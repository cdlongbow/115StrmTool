import queue
import threading
from os import name as os_name
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Dict, Iterator, List, Optional, Tuple
from urllib.parse import parse_qs, urlsplit

from logger import logger
from p115_client_wrapper import P115ClientWrapper
from database import db
from fast_scan import FastScanUnavailable, iter_files_fast

# 115 目录遍历单次无活动超时（秒）：若 SDK 遍历在该时长内无新文件产出，
# 判定被阻塞并停止遍历，避免拖垮整个同步任务
SYNC_TRAVERSE_IDLE_TIMEOUT_SECONDS = 60.0


class TraversalError(RuntimeError):
    """
    115 目录遍历失败

    生产线程异常或超时时抛出，调用方不得将当前根视为扫描成功
    """


class TraversalTimeoutError(TraversalError):
    """
    115 目录遍历无活动超时
    """


def _notes_suffix(notes: List[str]) -> str:
    """
    将同步过程备注拼接为完成消息后缀

    :param notes (List): 备注列表

    :return str: 圆括号包裹的备注串，无备注时为空字符串
    """
    return f"（{'；'.join(notes)}）" if notes else ""


def sanitize_path_parts(rel_path: Path) -> Path:
    if os_name != "nt":
        return rel_path
    illegal_chars = '<>"|?*'
    parts = list(rel_path.parts)
    if not parts:
        return rel_path
    sanitized = []
    for i, part in enumerate(parts):
        if i == 0 and part.endswith("\\"):
            sanitized.append(part)
            continue
        part = part.replace(":", "：")
        for char in illegal_chars:
            part = part.replace(char, "_")
        sanitized.append(part)
    result = Path(sanitized[0])
    for part in sanitized[1:]:
        result = result / part
    return result


def _iter_with_timeout(iterator: Iterator[Any], timeout: float) -> Iterator[Any]:
    """
    为同步迭代器加无活动超时：后台线程消费迭代器并入队，
    主线程带超时出队；超时或生产者异常时抛出专用异常

    :param iterator (Iterator): 同步迭代器
    :param timeout (float): 单次出队超时（秒）

    :return Iterator: 产出原迭代器项的迭代器

    :raises TraversalTimeoutError: 无活动超时
    :raises TraversalError: 生产者异常
    """
    out_queue: "queue.Queue[Optional[Any]]" = queue.Queue(maxsize=1)
    sentinel = object()
    stop_event = threading.Event()

    def _put(item: Any) -> bool:
        while not stop_event.is_set():
            try:
                out_queue.put(item, timeout=0.1)
                return True
            except queue.Full:
                continue
        return False

    def _producer() -> None:
        try:
            for item in iterator:
                if stop_event.is_set() or not _put(item):
                    return
        except BaseException as e:
            if not stop_event.is_set():
                _put(e)
        finally:
            if not stop_event.is_set():
                _put(sentinel)

    producer = threading.Thread(
        target=_producer, daemon=True, name="115-traverse-timeout"
    )
    producer.start()

    try:
        while True:
            try:
                item = out_queue.get(timeout=timeout)
            except queue.Empty:
                logger.warning(
                    "115 目录遍历 %s 秒无新文件产出，停止遍历（可能被 SDK 阻塞）", timeout
                )
                raise TraversalTimeoutError(
                    f"115 目录遍历 {timeout} 秒无新文件产出"
                )
            if item is sentinel:
                return
            if isinstance(item, BaseException):
                logger.warning("115 目录遍历异常: %s", item, exc_info=True)
                raise TraversalError("115 目录遍历异常") from item
            yield item
    finally:
        stop_event.set()
        producer.join(timeout=min(max(timeout, 0.1), 1.0))


def _download_headers_of(download_url: Any) -> Dict[str, str]:
    """
    提取 SDK 下载地址中与直链绑定的请求头

    P115URL 除 URL 外还携带 headers（如 user-agent），115 CDN 会按直链的
    f 参数校验请求头，下载时必须一并转发

    :param download_url (Any): SDK download_url 返回值

    :return Dict: 小写头名到值的映射，无可用头时返回空字典
    """
    getter = getattr(download_url, "get", None)
    if not callable(getter):
        return {}
    try:
        raw = getter("headers")
    except Exception:
        return {}
    if not isinstance(raw, dict):
        return {}
    return {
        str(key).lower(): str(value)
        for key, value in raw.items()
        if key and value
    }


def _is_strictly_under(path: Path, parent: Path) -> bool:
    """
    判断路径是否为父路径的严格子路径

    :param path (Path): 待判断路径
    :param parent (Path): 父路径

    :return bool: path 位于 parent 之下且不相等时为 True
    """
    if path == parent:
        return False
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def _is_path_within(path: Path, root: Path) -> bool:
    """
    判断解析后的路径是否严格位于根目录内

    :param path (Path): 待判断路径
    :param root (Path): 根目录

    :return bool: path 位于 root 内且不相等时为 True
    """
    try:
        resolved_path = path.expanduser().resolve(strict=False)
        resolved_root = root.expanduser().resolve(strict=False)
        resolved_path.relative_to(resolved_root)
        return resolved_path != resolved_root
    except (OSError, RuntimeError, ValueError):
        return False


def _local_path_key(path: Path) -> str:
    """
    生成本地路径碰撞检查键，统一分隔符并忽略大小写

    :param path (Path): 本地路径

    :return str: 规范化碰撞键
    """
    return str(path).replace("\\", "/").rstrip("/").casefold()


def _strm_has_pickcode(strm_path: Path, pickcode: str) -> bool:
    """
    校验既有 STRM 是否确实指向指定 pickcode

    :param strm_path (Path): STRM 文件路径
    :param pickcode (str): 期望 pickcode

    :return bool: 文件内容包含匹配的 pickcode 参数时为 True
    """
    try:
        content = strm_path.read_text(encoding="utf-8").strip()
        query = parse_qs(urlsplit(content).query)
    except (OSError, UnicodeError, ValueError):
        return False
    return pickcode in query.get("pickcode", [])


def _rename_root(old_dir: Path, new_dir: Path) -> Tuple[Path, Path]:
    """
    去掉新旧目录共同的结尾层级，返回实际发生改名的目录对

    :param old_dir (Path): 旧本地目录
    :param new_dir (Path): 新本地目录

    :return Tuple: (旧目录根, 新目录根)
    """
    old_parts = old_dir.parts
    new_parts = new_dir.parts
    common = 0
    while (
        common < len(old_parts)
        and common < len(new_parts)
        and old_parts[-1 - common] == new_parts[-1 - common]
    ):
        common += 1
    if not common:
        return old_dir, new_dir
    old_keep = len(old_parts) - common
    new_keep = len(new_parts) - common
    if old_keep <= 0 or new_keep <= 0:
        return old_dir, new_dir
    return Path(*old_parts[:old_keep]), Path(*new_parts[:new_keep])


def _iter_files_115(client_wrapper: P115ClientWrapper, cid: int):
    """
    递归遍历 115 目录下的所有文件（非目录）

    使用 iter_files（Android API, proapi.115.com）替代 fs_files（webapi.115.com），
    避免 webapi 域名被风控返回 405 的问题
    返回的 attr 已由 SDK 内部默认的 normalize_attr 完成字段规范化，直接产出即可，
    再包一层 normalize_attr 会因格式无法识别被当作 web 数据二次解析而 KeyError

    :param client_wrapper: P115ClientWrapper 实例
    :param cid: 起始目录 ID
    """
    client = client_wrapper.client
    if client is None:
        raise TraversalError("115 客户端未初始化或已关闭，无法执行目录扫描")
    from p115client.tool.iterdir import iter_files

    yield from _iter_with_timeout(
        iter_files(
            client, cid, type=99, cur=0, app="android",
            cooldown=0.5, escape=None, with_path=True,
        ),
        timeout=SYNC_TRAVERSE_IDLE_TIMEOUT_SECONDS,
    )


class StrmGenerator:
    def __init__(self, client: P115ClientWrapper, url_prefix: str = ""):
        self._client = client
        self._url_prefix = (url_prefix or "").strip().rstrip("/")
        self._cancel_flag = threading.Event()
        self._progress_callback: Optional[Callable] = None
        self._progress: Dict[str, Any] = {"phase": "idle", "current": 0, "total": 0, "message": ""}
        self._rmt_mediaext: set = {
            ".mp4", ".mkv", ".ts", ".iso", ".rmvb", ".avi", ".mov", ".mpeg", ".mpg",
            ".wmv", ".3gp", ".asf", ".m4v", ".flv", ".m2ts", ".tp", ".f4v", ".webm",
        }
        self._download_mediaext: set = {
            ".srt", ".ssa", ".ass", ".sup", ".pgs", ".sub", ".idx",
        }
        self._auto_download_mediainfo = False
        self._overwrite_mode = "never"
        self._cleanup_deleted = False
        self._use_rust = False
        self._fast_scan = True
        self._sync_notes: List[str] = []
        self._rust_processor = None
        self._sync_lock = threading.RLock()
        self._active_local_roots: List[Path] = []

    def set_progress_callback(self, cb: Callable):
        self._progress_callback = cb

    def get_progress(self) -> Dict[str, Any]:
        return dict(self._progress)

    def _set_progress(self, phase: str, current: int = 0, total: int = 0, message: str = ""):
        self._progress.update({"phase": phase, "current": current, "total": total, "message": message})

    def set_config(
        self,
        rmt_mediaext: str = "",
        download_mediaext: str = "",
        auto_download_mediainfo: bool = False,
        overwrite_mode: str = "never",
        cleanup_deleted: bool = False,
        use_rust: Optional[bool] = None,
        fast_scan: Optional[bool] = None,
    ):
        if rmt_mediaext:
            new_ext = {f".{e.strip().lower()}" for e in rmt_mediaext.replace("，", ",").split(",") if e.strip()}
            if new_ext != self._rmt_mediaext:
                # 扩展名变化使 Rust 处理器内嵌的旧配置过期，必须重建
                self._rust_processor = None
                self._rmt_mediaext = new_ext
        if download_mediaext:
            self._download_mediaext = {f".{e.strip().lower()}" for e in download_mediaext.replace("，", ",").split(",") if e.strip()}
        self._auto_download_mediainfo = auto_download_mediainfo
        self._overwrite_mode = overwrite_mode
        self._cleanup_deleted = cleanup_deleted
        if use_rust is not None:
            self._use_rust = use_rust
        if fast_scan is not None:
            self._fast_scan = fast_scan

    def set_use_rust(self, enabled: bool):
        self._use_rust = enabled

    def _assert_local_containment(self, path: Path) -> None:
        """
        确认写盘路径仍位于本轮同步的本地根目录内

        :param path (Path): 待写入路径

        :raises ValueError: 路径越界
        """
        if not self._active_local_roots:
            return
        if not any(_is_path_within(path, root) for root in self._active_local_roots):
            raise ValueError(f"本地路径越界，拒绝写入: {path}")

    def _claim_local_path(
        self,
        path: Path,
        pickcode: str,
        path_owners: Dict[str, set],
        conflicting_paths: Optional[set] = None,
    ) -> bool:
        """
        占用规范化本地路径，拒绝不同 pickcode 共享同一 STRM

        :param path (Path): 本地 STRM 路径
        :param pickcode (str): 当前文件 pickcode
        :param path_owners (Dict): 规范化路径到 pickcode 集合的映射
        :param conflicting_paths (set): 冲突路径键集合，用于阻止清理旧记录

        :return bool: 成功占用或同一 pickcode 复用时为 True
        """
        key = _local_path_key(path)
        owners = path_owners.setdefault(key, set())
        if owners and pickcode not in owners:
            if conflicting_paths is not None:
                conflicting_paths.add(key)
            logger.warning(
                "本地 STRM 路径冲突，跳过: %s (pickcode=%s, 已占用=%s)",
                path,
                pickcode,
                ",".join(sorted(owners)),
            )
            return False
        owners.add(pickcode)
        return True

    def _collect_path_owners(self, path_mappings: List[Dict[str, str]]) -> Dict[str, set]:
        """
        汇总当前映射下已有数据库记录路径占用

        :param path_mappings (List): 同步路径映射

        :return Dict: 规范化本地路径到 pickcode 集合的映射
        """
        owners: Dict[str, set] = {}
        for mapping in path_mappings:
            if mapping.get("enabled") is False:
                continue
            pan_path = mapping.get("from", "")
            if not pan_path:
                continue
            for record in db.get_active_files_by_parent(pan_path):
                key = _local_path_key(Path(record["local_strm_path"]))
                owners.setdefault(key, set()).add(record["pickcode"])
        return owners

    def _delete_strm_file(self, strm_path: Path) -> bool:
        """
        删除 STRM 文件并报告本地删除是否成功

        :param strm_path (Path): 待删除文件路径

        :return bool: 文件不存在或删除成功时为 True
        """
        try:
            strm_path.unlink()
        except FileNotFoundError:
            return True
        except OSError as e:
            logger.warning("删除 STRM 文件失败 %s: %s", strm_path, e)
            return False
        logger.info("已删除残留 STRM: %s", strm_path)
        return True

    def _all_enabled_roots_scanned(
        self, path_mappings: List[Dict[str, str]], scanned_roots: set
    ) -> bool:
        """
        判断所有启用映射是否都完成了整根扫描

        :param path_mappings (List): 同步路径映射
        :param scanned_roots (set): 已成功扫描的网盘根集合

        :return bool: 所有启用根均已扫描时为 True
        """
        return all(
            mapping.get("from") in scanned_roots
            for mapping in path_mappings
            if mapping.get("enabled") is not False
        )

    def _iter_root_files(self, cid: int, pan_path: str):
        """
        按配置选择遍历方式产出同步根的文件属性流，快速扫描不可用时逐根降级

        :param cid (int): 同步根目录 cid
        :param pan_path (str): 同步根网盘路径

        :return: 文件属性字典迭代器
        """
        if not self._fast_scan:
            return _iter_files_115(self._client, cid)
        try:
            attrs = iter_files_fast(
                self._client,
                cid,
                root_pan_path=pan_path,
                cancellation=self._cancel_flag,
            )
            return iter(attrs)
        except FastScanUnavailable as e:
            logger.warning("快速扫描降级为逐目录遍历 [%s]: %s", pan_path, e)
            note = f"{pan_path} 快速扫描降级"
            if note not in self._sync_notes:
                self._sync_notes.append(note)
            return _iter_files_115(self._client, cid)

    def _get_rust_processor(self):
        if self._rust_processor is not None:
            return self._rust_processor
        try:
            from full_strm_sync import Processor

            import json
            config_json = json.dumps({
                "media_extensions": list(self._rmt_mediaext)
            })
            self._rust_processor = Processor(config_json)
            from full_strm_sync import __version__ as rust_core_version
            logger.info("Rust STRM 加速核心已初始化 v%s", rust_core_version)
            return self._rust_processor
        except ImportError:
            logger.warning("full_strm_sync 不可用，回退到纯 Python 模式")
            self._use_rust = False
            return None
        except Exception as e:
            logger.error("初始化 Rust 处理器失败: %s", e)
            self._use_rust = False
            return None

    def _process_rust_batch(
        self,
        rust_items: List[Dict[str, Any]],
        files_by_pan_path: Dict[str, Tuple[Path, str, bool]],
    ) -> None:
        """
        提交 Rust 批处理生成 STRM，失败部分用 Python 逐文件回填

        Rust 批处理与 Python 写 STRM 互斥执行，防止双写；处理器不可用、
        批处理抛异常或单文件被报失败时，统一走 _ensure_strm_file 回填，
        避免数据库记录指向不存在的 STRM 文件

        :param rust_items: 提交给 Rust 处理器的文件项列表
        :param files_by_pan_path: 网盘路径 -> (本地 STRM 路径, pickcode, 是否强制覆盖) 映射
        """
        if not rust_items:
            return
        for strm_path, _, _ in files_by_pan_path.values():
            self._assert_local_containment(strm_path)
        processor = self._get_rust_processor()
        if processor is None:
            self._backfill_strm_files(files_by_pan_path)
            return
        failed_pan_paths: List[str] = []
        try:
            import json
            results = processor.process_batch(json.dumps(rust_items))
            rust_strm_count = getattr(results, "strm_results_count", 0) or 0
            fail_results = getattr(results, "fail_results", []) or []
            logger.info(
                "Rust 加速处理完成: STRM=%d, 失败=%d",
                rust_strm_count, len(fail_results)
            )
            for fail_info in fail_results:
                pan_path = getattr(fail_info, "path_in_pan", "")
                if pan_path:
                    failed_pan_paths.append(pan_path)
                logger.warning(
                    "Rust STRM 生成失败: path=%s reason=%s",
                    pan_path or "?",
                    getattr(fail_info, "reason", "?"),
                )
        except Exception as e:
            logger.error("Rust 批处理失败，回退 Python 逐文件生成: %s", e, exc_info=True)
            self._backfill_strm_files(files_by_pan_path)
            return
        if failed_pan_paths:
            self._backfill_strm_files(
                {
                    p: files_by_pan_path[p]
                    for p in failed_pan_paths
                    if p in files_by_pan_path
                }
            )

    def _backfill_strm_files(
        self, files_by_pan_path: Dict[str, Tuple[Path, str, bool]]
    ) -> None:
        """
        用 Python 逐文件回填 STRM

        :param files_by_pan_path: 网盘路径 -> (本地 STRM 路径, pickcode, 是否强制覆盖) 映射
        """
        for strm_path, pickcode, force in files_by_pan_path.values():
            try:
                if not self._ensure_strm_file(strm_path, pickcode, force=force):
                    logger.warning("回填 STRM 目标冲突，未登记: %s", strm_path)
            except (OSError, ValueError) as e:
                logger.warning("回填 STRM 失败 %s: %s", strm_path, e)

    def cancel(self):
        self._cancel_flag.set()
        self._set_progress("cancelling", message="正在取消同步...")
        logger.info("同步已请求取消")

    def is_syncing(self) -> bool:
        """
        判断当前是否有同步任务正在执行

        :return bool: 同步锁被占用时返回 True
        """
        acquired = self._sync_lock.acquire(blocking=False)
        if acquired:
            self._sync_lock.release()
            return False
        return True

    def reset_cancel(self):
        self._cancel_flag.clear()

    def _resolve_pan_path(self, pan_path: str) -> int:
        http_client = self._client.client
        if http_client is None:
            raise RuntimeError("115 客户端未初始化")
        resp = http_client.fs_dir_getid(pan_path)
        if not isinstance(resp, dict):
            raise RuntimeError(f"获取目录ID失败: {pan_path}")
        from p115client import check_response as _check_response
        _check_response(resp)
        cid = int(resp.get("id", -1))
        if cid <= 0:
            raise RuntimeError(f"目录不存在: {pan_path}")
        return cid

    def full_sync(self, path_mappings: List[Dict[str, str]], **kwargs) -> Dict[str, Any]:
        if not self._sync_lock.acquire(blocking=False):
            logger.warning("全量同步已在进行中，忽略重复请求")
            return {"error": "sync_already_running", "message": "全量同步已在进行中"}
        history_id: Optional[int] = None
        total_new = 0
        total_deleted = 0
        total_failed = 0
        total_count = 0
        self._active_local_roots = []
        try:
            self.reset_cancel()
            self._active_local_roots = [
                Path(mapping["to"])
                for mapping in path_mappings
                if mapping.get("enabled") is not False and mapping.get("to")
            ]
            if kwargs.get("rmt_mediaext") is not None:
                self.set_config(
                    rmt_mediaext=kwargs.get("rmt_mediaext", ""),
                    download_mediaext=kwargs.get("download_mediaext", ""),
                    auto_download_mediainfo=kwargs.get("auto_download_mediainfo", False),
                )
            self._sync_notes = []
            history_id = db.add_sync_history("full")
            self._set_progress("scanning", message="准备开始全量同步...")

            all_files = []
            rust_items = []
            seen_pickcodes: set = set()
            scanned_roots: set = set()
            conflicting_paths: set = set()
            path_owners = self._collect_path_owners(path_mappings)

            for mapping in path_mappings:
                if self._cancel_flag.is_set():
                    break
                if mapping.get("enabled") is False:
                    continue
                pan_path = mapping["from"]
                local_path = mapping["to"]
                self._set_progress("scanning", message=f"正在扫描目录: {pan_path}")
                logger.info("开始同步目录: %s -> %s", pan_path, local_path)

                try:
                    cid = self._resolve_pan_path(pan_path)

                    for attr in self._iter_root_files(cid, pan_path):
                        if self._cancel_flag.is_set():
                            break
                        if attr.get("is_dir"):
                            continue
                        name = attr.get("name", "")
                        ext = Path(name).suffix.lower()
                        pickcode = attr.get("pickcode", "")
                        pan_full_path = attr.get("path", "")

                        self._try_download_aux(
                            ext, pickcode, name, pan_full_path, pan_path, local_path
                        )

                        if ext not in self._rmt_mediaext or not pickcode:
                            continue
                        seen_pickcodes.add(pickcode)
                        local_strm_path_orig = self._to_local_path(
                            pan_full_path, pan_path, local_path
                        )
                        if local_strm_path_orig is None:
                            total_failed += 1
                            continue
                        local_strm_path = local_strm_path_orig.with_suffix(".strm")
                        self._assert_local_containment(local_strm_path)
                        if not self._claim_local_path(
                            local_strm_path,
                            pickcode,
                            path_owners,
                            conflicting_paths,
                        ):
                            total_failed += 1
                            note = "检测到本地 STRM 路径冲突，已跳过冲突项"
                            if note not in self._sync_notes:
                                self._sync_notes.append(note)
                            continue
                        if self._use_rust:
                            rust_items.append({
                                "name": name,
                                "path": pan_full_path,
                                "is_dir": False,
                                "size": attr.get("size", 0),
                                "pickcode": pickcode,
                                "sha1": attr.get("sha1", ""),
                            })
                        else:
                            if not self._ensure_strm_file(local_strm_path, pickcode):
                                total_failed += 1
                                continue
                        all_files.append({
                            "pickcode": pickcode,
                            "file_name": name,
                            "file_size": attr.get("size", 0),
                            "file_type": ext,
                            "pan_path": pan_full_path,
                            "local_strm_path": str(local_strm_path),
                            "sha1": attr.get("sha1", ""),
                            "parent_id": pan_path,
                        })
                        self._set_progress(
                            "scanning",
                            current=len(all_files),
                            message=f"已扫描 {len(all_files)} 个文件",
                        )
                except Exception as e:
                    logger.error("同步目录失败 %s: %s", pan_path, e, exc_info=True)
                    total_failed += 1
                    note = f"{pan_path} 扫描失败，已跳过该目录残留清理"
                    if note not in self._sync_notes:
                        self._sync_notes.append(note)
                else:
                    if not self._cancel_flag.is_set():
                        scanned_roots.add(pan_path)

            if self._use_rust and rust_items:
                files_by_pan_path = {
                    f["pan_path"]: (Path(f["local_strm_path"]), f["pickcode"], False)
                    for f in all_files
                }
                self._process_rust_batch(rust_items, files_by_pan_path)

            if all_files:
                db.batch_add_files(all_files)
                total_new = len(all_files)
            total_count = total_new

            if self._cleanup_deleted and not self._cancel_flag.is_set():
                cleanup_roots = scanned_roots
                if not all_files and (
                    total_failed or not self._all_enabled_roots_scanned(path_mappings, scanned_roots)
                ):
                    cleanup_roots = set()
                for mapping in path_mappings:
                    if mapping.get("enabled") is False:
                        continue
                    pan_path = mapping["from"]
                    if pan_path not in cleanup_roots:
                        continue
                    for record in db.get_active_files_by_parent(pan_path):
                        if record["pickcode"] in seen_pickcodes:
                            continue
                        if _local_path_key(Path(record["local_strm_path"])) in conflicting_paths:
                            continue
                        if self._delete_strm_file(Path(record["local_strm_path"])):
                            db.mark_file_deleted(record["pickcode"])
                            total_deleted += 1
                        else:
                            total_failed += 1

            if self._cancel_flag.is_set():
                db.finish_sync_history(
                    history_id,
                    total_count,
                    total_new,
                    total_deleted,
                    total_failed,
                    status="cancelled",
                )
                self._set_progress(
                    "cancelled",
                    current=total_count,
                    total=total_count,
                    message=f"全量同步已取消: 新增 {total_new}，删除 {total_deleted}，失败 {total_failed}",
                )
            else:
                db.finish_sync_history(
                    history_id, total_count, total_new, total_deleted, total_failed
                )
                self._set_progress(
                    "completed",
                    current=total_count,
                    total=total_count,
                    message=(
                        f"全量同步完成: 新增 {total_new}，删除 {total_deleted}，"
                        f"失败 {total_failed}{_notes_suffix(self._sync_notes)}"
                    ),
                )
                logger.info(
                    "全量同步完成: 新增=%d, 删除=%d, 失败=%d",
                    total_new,
                    total_deleted,
                    total_failed,
                )
        except Exception as e:
            self._set_progress("error", message=f"全量同步异常: {e}")
            logger.error("全量同步异常: %s", e, exc_info=True)
            if history_id is not None:
                try:
                    db.finish_sync_history(
                        history_id,
                        total_count,
                        total_new,
                        total_deleted,
                        total_failed,
                        str(e),
                    )
                except Exception as history_error:
                    logger.error("写入全量同步失败历史异常: %s", history_error, exc_info=True)
            return {
                "history_id": history_id,
                "total": total_count,
                "new": total_new,
                "deleted": total_deleted,
                "failed": total_failed,
                "cancelled": self._cancel_flag.is_set(),
                "error": str(e),
            }
        finally:
            self._active_local_roots = []
            self._sync_lock.release()

        return {
            "history_id": history_id,
            "total": total_count,
            "new": total_new,
            "deleted": total_deleted,
            "failed": total_failed,
            "cancelled": self._cancel_flag.is_set(),
        }

    def incremental_sync(self, path_mappings: List[Dict[str, str]], **kwargs) -> Dict[str, Any]:
        if not self._sync_lock.acquire(blocking=False):
            logger.warning("增量同步已在进行中，忽略重复请求")
            return {"error": "sync_already_running", "message": "增量同步已在进行中"}
        history_id: Optional[int] = None
        total_new = 0
        total_changed = 0
        total_deleted = 0
        total_failed = 0
        total_unchanged = 0
        self._active_local_roots = []
        try:
            self.reset_cancel()
            self._active_local_roots = [
                Path(mapping["to"])
                for mapping in path_mappings
                if mapping.get("enabled") is not False and mapping.get("to")
            ]
            if kwargs.get("rmt_mediaext") is not None:
                self.set_config(
                    rmt_mediaext=kwargs.get("rmt_mediaext", ""),
                    download_mediaext=kwargs.get("download_mediaext", ""),
                    auto_download_mediainfo=kwargs.get("auto_download_mediainfo", False),
                )
            if db.count_active_files() == 0:
                logger.info("数据库中无同步记录，首次增量同步自动切换为全量同步")
                return self.full_sync(path_mappings, **kwargs)

            self._sync_notes = []
            history_id = db.add_sync_history("incremental")
            self._set_progress("scanning", message="准备开始增量同步...")

            all_new_files = []
            seen_pickcodes: set = set()
            rust_items = []
            files_by_pan_path: Dict[str, Tuple[Path, str, bool]] = {}
            renames: List[Tuple[Dict[str, Any], Path, Path]] = []
            pending_aux: List[Tuple[str, str, str, str, str, str]] = []
            all_existing: Dict[str, Dict] = {}
            path_owners: Dict[str, set] = {}
            conflicting_paths: set = set()
            scanned_roots: set = set()

            for mapping in path_mappings:
                if mapping.get("enabled") is False:
                    continue
                pan_path = mapping.get("from", "")
                if not pan_path:
                    continue
                for record in db.get_active_files_by_parent(pan_path):
                    all_existing[record["pickcode"]] = {
                        "pickcode": record["pickcode"],
                        "sha1": record["sha1"],
                        "pan_path": record["pan_path"],
                        "local_strm_path": record["local_strm_path"],
                        "root": pan_path,
                    }
                    key = _local_path_key(Path(record["local_strm_path"]))
                    path_owners.setdefault(key, set()).add(record["pickcode"])

            for mapping in path_mappings:
                if self._cancel_flag.is_set():
                    break
                if mapping.get("enabled") is False:
                    continue
                pan_path = mapping["from"]
                local_path = mapping["to"]
                self._set_progress("scanning", message=f"正在扫描目录: {pan_path}")
                logger.info("开始增量同步目录: %s -> %s", pan_path, local_path)

                try:
                    cid = self._resolve_pan_path(pan_path)

                    for attr in self._iter_root_files(cid, pan_path):
                        if self._cancel_flag.is_set():
                            break
                        if attr.get("is_dir"):
                            continue
                        name = attr.get("name", "")
                        ext = Path(name).suffix.lower()
                        pickcode = attr.get("pickcode", "")
                        pan_full_path = attr.get("path", "")

                        if self._auto_download_mediainfo and ext in self._download_mediaext:
                            pending_aux.append(
                                (ext, pickcode, name, pan_full_path, pan_path, local_path)
                            )

                        if ext not in self._rmt_mediaext or not pickcode:
                            continue
                        seen_pickcodes.add(pickcode)
                        sha1 = attr.get("sha1", "")
                        local_strm_path_orig = self._to_local_path(
                            pan_full_path, pan_path, local_path
                        )
                        if local_strm_path_orig is None:
                            total_failed += 1
                            continue
                        local_strm_path = local_strm_path_orig.with_suffix(".strm")
                        self._assert_local_containment(local_strm_path)

                        existing = all_existing.get(pickcode)
                        if existing is None:
                            if not self._claim_local_path(
                                local_strm_path, pickcode, path_owners, conflicting_paths
                            ):
                                total_failed += 1
                                note = "检测到本地 STRM 路径冲突，已跳过冲突项"
                                if note not in self._sync_notes:
                                    self._sync_notes.append(note)
                                continue
                            if self._use_rust:
                                rust_items.append({
                                    "name": name,
                                    "path": pan_full_path,
                                    "is_dir": False,
                                    "size": attr.get("size", 0),
                                    "pickcode": pickcode,
                                    "sha1": sha1,
                                })
                                files_by_pan_path[pan_full_path] = (
                                    local_strm_path,
                                    pickcode,
                                    False,
                                )
                            else:
                                if not self._ensure_strm_file(local_strm_path, pickcode):
                                    total_failed += 1
                                    continue
                            all_new_files.append({
                                "pickcode": pickcode,
                                "file_name": name,
                                "file_size": attr.get("size", 0),
                                "file_type": ext,
                                "pan_path": pan_full_path,
                                "local_strm_path": str(local_strm_path),
                                "sha1": sha1,
                                "parent_id": pan_path,
                            })
                            total_new += 1
                        else:
                            old_strm_path = Path(existing["local_strm_path"])
                            path_changed = old_strm_path != local_strm_path
                            pan_changed = existing["pan_path"] != pan_full_path
                            sha_changed = existing["sha1"] != sha1
                            if sha_changed and not path_changed and not pan_changed:
                                if not self._claim_local_path(
                                    local_strm_path, pickcode, path_owners, conflicting_paths
                                ):
                                    total_failed += 1
                                    continue
                                if self._use_rust:
                                    rust_items.append({
                                        "name": name,
                                        "path": pan_full_path,
                                        "is_dir": False,
                                        "size": attr.get("size", 0),
                                        "pickcode": pickcode,
                                        "sha1": sha1,
                                    })
                                    files_by_pan_path[pan_full_path] = (
                                        local_strm_path,
                                        pickcode,
                                        True,
                                    )
                                else:
                                    if not self._ensure_strm_file(
                                        local_strm_path, pickcode, force=True
                                    ):
                                        total_failed += 1
                                        continue
                                all_new_files.append({
                                    "pickcode": pickcode,
                                    "file_name": name,
                                    "file_size": attr.get("size", 0),
                                    "file_type": ext,
                                    "pan_path": pan_full_path,
                                    "local_strm_path": str(local_strm_path),
                                    "sha1": sha1,
                                    "parent_id": pan_path,
                                })
                                total_changed += 1
                            elif path_changed or pan_changed:
                                if not self._claim_local_path(
                                    local_strm_path, pickcode, path_owners, conflicting_paths
                                ):
                                    total_failed += 1
                                    note = "检测到本地 STRM 路径冲突，已跳过冲突项"
                                    if note not in self._sync_notes:
                                        self._sync_notes.append(note)
                                    continue
                                renames.append(
                                    (existing, local_strm_path, Path(local_path))
                                )
                                all_new_files.append({
                                    "pickcode": pickcode,
                                    "file_name": name,
                                    "file_size": attr.get("size", 0),
                                    "file_type": ext,
                                    "pan_path": pan_full_path,
                                    "local_strm_path": str(local_strm_path),
                                    "sha1": sha1,
                                    "parent_id": pan_path,
                                })
                                total_changed += 1
                            else:
                                total_unchanged += 1
                        processed = total_new + total_changed + total_unchanged
                        self._set_progress(
                            "scanning",
                            current=processed,
                            message=f"已扫描 {processed} 个文件",
                        )
                except Exception as e:
                    logger.error("增量同步目录失败 %s: %s", pan_path, e, exc_info=True)
                    total_failed += 1
                    note = f"{pan_path} 扫描失败，已跳过该目录残留清理"
                    if note not in self._sync_notes:
                        self._sync_notes.append(note)
                else:
                    if not self._cancel_flag.is_set():
                        scanned_roots.add(pan_path)

            if not self._cancel_flag.is_set():
                deleted_pickcodes = [
                    pc
                    for pc in all_existing
                    if pc not in seen_pickcodes
                    and all_existing[pc]["root"] in scanned_roots
                    and _local_path_key(Path(all_existing[pc]["local_strm_path"]))
                    not in conflicting_paths
                ]
                if deleted_pickcodes and not self._cleanup_deleted:
                    note = (
                        f"检测到 {len(deleted_pickcodes)} 个残留 STRM，"
                        "未开启「同步后清理残留」，已保留"
                    )
                    logger.info(note)
                    if note not in self._sync_notes:
                        self._sync_notes.append(note)
                else:
                    for pc in deleted_pickcodes:
                        entry = all_existing[pc]
                        if self._delete_strm_file(Path(entry["local_strm_path"])):
                            db.mark_file_deleted(pc)
                            total_deleted += 1
                        else:
                            total_failed += 1

            failed_renames = self._apply_renames(
                renames, all_existing, all_new_files, scanned_roots
            )
            if failed_renames:
                all_new_files = [
                    record
                    for record in all_new_files
                    if record["pickcode"] not in failed_renames
                ]
                total_changed -= len(failed_renames)
                total_failed += len(failed_renames)
            if not self._cancel_flag.is_set():
                for aux_args in pending_aux:
                    if self._cancel_flag.is_set():
                        break
                    self._try_download_aux(*aux_args)

            if self._use_rust and rust_items:
                self._process_rust_batch(rust_items, files_by_pan_path)

            self._set_progress("processing", message="正在写入数据库...")
            if all_new_files:
                db.batch_add_files(all_new_files)

            total_count = total_new + total_changed + total_unchanged + total_deleted
            if self._cancel_flag.is_set():
                db.finish_sync_history(
                    history_id,
                    total_count,
                    total_new + total_changed,
                    total_deleted,
                    total_failed,
                    status="cancelled",
                )
                self._set_progress(
                    "cancelled",
                    current=total_count,
                    total=total_count,
                    message=(
                        f"增量同步已取消: 新增 {total_new}，变更 {total_changed}，"
                        f"删除 {total_deleted}，失败 {total_failed}"
                    ),
                )
            else:
                db.finish_sync_history(
                    history_id,
                    total_count,
                    total_new + total_changed,
                    total_deleted,
                    total_failed,
                )
                self._set_progress(
                    "completed",
                    current=total_count,
                    total=total_count,
                    message=(
                        f"增量同步完成: 新增 {total_new}，变更 {total_changed}，"
                        f"删除 {total_deleted}，失败 {total_failed}"
                        f"{_notes_suffix(self._sync_notes)}"
                    ),
                )
                logger.info(
                    "增量同步完成: 新增=%d, 变更=%d, 未变=%d, 删除=%d, 失败=%d",
                    total_new,
                    total_changed,
                    total_unchanged,
                    total_deleted,
                    total_failed,
                )
        except Exception as e:
            self._set_progress("error", message=f"增量同步异常: {e}")
            logger.error("增量同步异常: %s", e, exc_info=True)
            if history_id is not None:
                try:
                    db.finish_sync_history(
                        history_id,
                        total_new + total_changed + total_unchanged + total_deleted,
                        total_new + total_changed,
                        total_deleted,
                        total_failed,
                        str(e),
                    )
                except Exception as history_error:
                    logger.error("写入增量同步失败历史异常: %s", history_error, exc_info=True)
            return {
                "history_id": history_id,
                "total": total_new + total_changed + total_unchanged + total_deleted,
                "new": total_new,
                "changed": total_changed,
                "unchanged": total_unchanged,
                "deleted": total_deleted,
                "failed": total_failed,
                "cancelled": self._cancel_flag.is_set(),
                "error": str(e),
            }
        finally:
            self._active_local_roots = []
            self._sync_lock.release()

        return {
            "history_id": history_id,
            "total": total_new + total_changed + total_unchanged + total_deleted,
            "new": total_new,
            "changed": total_changed,
            "unchanged": total_unchanged,
            "deleted": total_deleted,
            "failed": total_failed,
            "cancelled": self._cancel_flag.is_set(),
        }

    def _to_local_path(
        self,
        pan_full_path: str,
        base_pan_path: str,
        local_strm_dir: str,
    ) -> Optional[Path]:
        """
        将网盘文件路径安全映射到本地同步根

        :param pan_full_path (str): 网盘文件完整路径
        :param base_pan_path (str): 网盘同步根路径
        :param local_strm_dir (str): 本地同步根目录

        :return Path: 本地文件路径，路径越界或非法时返回 None
        """
        if not pan_full_path or not base_pan_path or not local_strm_dir:
            return None
        pan_path = PurePosixPath(pan_full_path)
        base_path = PurePosixPath(base_pan_path)
        if not pan_path.is_absolute() or not base_path.is_absolute():
            logger.warning("网盘路径必须为绝对路径，跳过: %s", pan_full_path)
            return None
        try:
            rel_path = pan_path.relative_to(base_path)
        except ValueError:
            logger.warning(
                "路径不在同步目录内，跳过: %s (基目录 %s)",
                pan_full_path,
                base_pan_path,
            )
            return None
        if not rel_path.parts or rel_path.is_absolute():
            return None
        if any(part in {"", ".", ".."} for part in rel_path.parts):
            logger.warning("网盘路径包含非法片段，跳过: %s", pan_full_path)
            return None
        local_root = Path(local_strm_dir)
        candidate = local_root.joinpath(*rel_path.parts)
        candidate = sanitize_path_parts(candidate)
        if not _is_path_within(candidate, local_root):
            logger.warning("本地路径越界，跳过: %s", pan_full_path)
            return None
        return candidate

    def _try_download_aux(
        self,
        ext: str,
        pickcode: str,
        name: str,
        pan_full_path: str,
        base_pan_path: str,
        local_strm_dir: str,
    ) -> None:
        """
        按扩展名与开关判定并触发附属文件下载

        :param ext (str): 文件扩展名（含点，小写）
        :param pickcode (str): 文件 pickcode
        :param name (str): 文件名
        :param pan_full_path (str): 网盘完整路径
        :param base_pan_path (str): 同步根目录
        :param local_strm_dir (str): 本地映射目录
        """
        if not (self._auto_download_mediainfo and ext in self._download_mediaext):
            return
        local_file_path = self._to_local_path(pan_full_path, base_pan_path, local_strm_dir)
        if local_file_path is not None and not local_file_path.exists():
            self._download_aux_file(pickcode, name, local_file_path)

    def _download_aux_file(self, pickcode: str, name: str, local_file_path: Path) -> None:
        """
        下载媒体附属文件（字幕、歌词等）到本地 STRM 目录

        SDK 的 download_url 返回含 URL 字段的原始响应（P115URL 可能带与直链绑定的
        headers），先提取直链与请求头再流式下载，先写 .part 临时文件成功后原子替换，
        避免中断留下半截文件

        :param pickcode (str): 文件 pickcode
        :param name (str): 原始文件名，用于日志
        :param local_file_path (Path): 本地保存路径
        """
        result = self._client.download_url(pickcode)
        request_headers = _download_headers_of(result)
        if isinstance(result, dict):
            file_url = result.get("url") or result.get("file_url")
        elif isinstance(result, str):
            file_url = result
        else:
            file_url = None
        if not file_url:
            logger.warning("附属文件无下载地址，跳过: %s", name)
            return
        self._assert_local_containment(local_file_path)
        local_file_path.parent.mkdir(parents=True, exist_ok=True)
        self._assert_local_containment(local_file_path)
        tmp_path = local_file_path.with_name(local_file_path.name + ".part")
        try:
            import httpx

            with httpx.stream(
                "GET",
                file_url,
                headers=request_headers or None,
                follow_redirects=True,
                timeout=30,
            ) as resp:
                resp.raise_for_status()
                with open(tmp_path, "wb") as f:
                    for chunk in resp.iter_bytes():
                        f.write(chunk)
            tmp_path.replace(local_file_path)
            logger.info("已下载附属文件: %s", name)
        except Exception as e:
            try:
                tmp_path.unlink(missing_ok=True)
            except OSError:
                pass
            logger.warning("下载附属文件失败 %s: %s", name, e)

    def _plan_dir_moves(
        self,
        renames: List[Tuple[Dict[str, Any], Path, Path]],
        all_existing: Dict[str, Dict],
        all_new_files: List[Dict[str, Any]],
    ) -> List[Tuple[Path, Path]]:
        """
        从文件级改名候选推导可整体搬移的目录对

        只接受新旧目录都在同一本地根内、目标不是同步根、旧目录下所有数据库记录
        都参与改名、且本轮没有新文件写在该旧目录下的场景；一个旧目录对应多个新目录、
        多个旧目录并入同一新目录、存在未改名或新增文件、父子改名目标不一致时
        退回逐文件迁移，避免把未参与改名的文件一起搬走

        :param renames (List): (旧记录, 新 STRM 路径, 本地根) 三元组列表
        :param all_existing (Dict): pickcode 到旧记录的映射
        :param all_new_files (List): 本轮待写入数据库的文件记录

        :return List: 可安全整体搬移的 (旧目录, 新目录) 列表
        """
        to_map: Dict[Path, Path] = {}
        bad: set = set()
        candidate_old = {str(entry["local_strm_path"]) for entry, _, _ in renames}

        for entry, new_strm_path, local_root in renames:
            old_strm_path = Path(entry["local_strm_path"])
            old_dir = old_strm_path.parent
            new_dir = new_strm_path.parent
            if old_dir == new_dir:
                continue
            if new_strm_path.exists() or new_strm_path.is_symlink():
                continue
            old_root, new_root = _rename_root(old_dir, new_dir)
            if old_root == new_root or old_root == local_root:
                continue
            if not _is_strictly_under(old_root, local_root):
                continue
            if not _is_strictly_under(new_root, local_root):
                continue
            if _is_strictly_under(new_root, old_root) or _is_strictly_under(
                old_root, new_root
            ):
                continue
            previous = to_map.get(old_root)
            if previous is not None and previous != new_root:
                bad.add(old_root)
            to_map[old_root] = new_root

        dest_count: Dict[Path, int] = {}
        for old_root, new_root in to_map.items():
            if old_root not in bad:
                dest_count[new_root] = dest_count.get(new_root, 0) + 1

        moves = [
            (old_root, new_root)
            for old_root, new_root in to_map.items()
            if old_root not in bad and dest_count[new_root] == 1
        ]
        moves.sort(key=lambda item: (len(item[0].parts), str(item[0])))

        conflicting: set = set()
        for old_root, new_root in moves:
            for other_old, other_new in moves:
                if old_root == other_old:
                    continue
                if not _is_strictly_under(other_old, old_root):
                    continue
                expected = new_root / other_old.relative_to(old_root)
                if expected != other_new:
                    conflicting.add(old_root)
        if conflicting:
            moves = [move for move in moves if move[0] not in conflicting]

        db_paths = [Path(entry["local_strm_path"]) for entry in all_existing.values()]
        new_paths = [
            Path(record["local_strm_path"]) for record in all_new_files
        ]
        kept: List[Tuple[Path, Path]] = []
        for old_root, new_root in moves:
            if any(
                _is_strictly_under(old_root, kept_old)
                or _is_strictly_under(new_root, kept_new)
                for kept_old, kept_new in kept
            ):
                continue
            if any(
                _is_strictly_under(db_path, old_root)
                and str(db_path) not in candidate_old
                for db_path in db_paths
            ):
                logger.info(
                    "目录 %s 仍含未改名文件，退回逐文件迁移", old_root
                )
                continue
            if any(_is_strictly_under(path, old_root) for path in new_paths):
                logger.info(
                    "目录 %s 含本轮新增文件，退回逐文件迁移", old_root
                )
                continue
            kept.append((old_root, new_root))
        return kept

    def _apply_renames(
        self,
        renames: List[Tuple[Dict[str, Any], Path, Path]],
        all_existing: Dict[str, Dict],
        all_new_files: List[Dict[str, Any]],
        scanned_roots: set,
    ) -> set:
        """
        应用改名迁移：优先整目录搬移，失败或条件不足时逐文件迁移

        整目录搬移只对整根扫描成功的映射生效；扫描失败的根仍按文件迁移已确认
        的改名，避免数据库记录与新 STRM 路径脱节

        :param renames (List): (旧记录, 新 STRM 路径, 本地根) 三元组列表
        :param all_existing (Dict): pickcode 到旧记录的映射
        :param all_new_files (List): 本轮待写入数据库的文件记录
        :param scanned_roots (set): 整根扫描成功的网盘根路径

        :return set: 迁移失败、需保留旧数据库记录的 pickcode 集合
        """
        failed: set = set()
        if not renames:
            return failed
        movable = [
            rename
            for rename in renames
            if rename[0].get("root") in scanned_roots
        ]
        moved_roots: List[Tuple[Path, Path]] = []
        for old_root, new_root in self._plan_dir_moves(
            movable, all_existing, all_new_files
        ):
            if not old_root.is_dir() or old_root.is_symlink() or new_root.exists():
                continue
            try:
                self._assert_local_containment(new_root)
                new_root.parent.mkdir(parents=True, exist_ok=True)
                old_root.rename(new_root)
            except (OSError, ValueError) as e:
                logger.warning(
                    "目录级迁移失败 %s -> %s: %s", old_root, new_root, e
                )
                continue
            moved_roots.append((old_root, new_root))
            logger.info("已迁移目录: %s -> %s", old_root, new_root)

        prune_starts: List[Tuple[Path, Path]] = []
        moved_pickcodes: set = set()
        for moved_old, moved_new in moved_roots:
            group = [
                rename
                for rename in renames
                if _is_strictly_under(
                    Path(rename[0]["local_strm_path"]), moved_old
                )
            ]
            created: List[Path] = []
            group_failed: set = set()
            for entry, new_strm_path, _ in group:
                pickcode = entry.get("pickcode", "")
                moved_pickcodes.add(pickcode)
                old_strm_path = Path(entry["local_strm_path"])
                relocated = moved_new / old_strm_path.relative_to(moved_old)
                if relocated == new_strm_path:
                    continue
                if new_strm_path.exists() or new_strm_path.is_symlink():
                    logger.warning(
                        "改名目标已存在，保留源和目标: %s -> %s",
                        relocated,
                        new_strm_path,
                    )
                    group_failed.add(pickcode)
                    continue
                try:
                    new_strm_path.parent.mkdir(parents=True, exist_ok=True)
                    with relocated.open("rb") as source, new_strm_path.open("xb") as target:
                        target.write(source.read())
                except FileExistsError:
                    logger.warning(
                        "改名目标已存在，保留源和目标: %s -> %s",
                        relocated,
                        new_strm_path,
                    )
                    group_failed.add(pickcode)
                except OSError as e:
                    try:
                        new_strm_path.unlink()
                    except FileNotFoundError:
                        pass
                    except OSError as cleanup_error:
                        logger.warning(
                            "清理失败的新 STRM 异常 %s: %s",
                            new_strm_path,
                            cleanup_error,
                        )
                    logger.warning(
                        "改名后写入新 STRM 失败 %s: %s", new_strm_path, e
                    )
                    group_failed.add(pickcode)
                else:
                    created.append(new_strm_path)
            if group_failed:
                for created_path in created:
                    try:
                        created_path.unlink()
                    except FileNotFoundError:
                        pass
                    except OSError as e:
                        logger.warning(
                            "回滚新增 STRM 失败 %s: %s", created_path, e
                        )
                try:
                    moved_new.rename(moved_old)
                except OSError as e:
                    logger.error(
                        "目录迁移回滚失败 %s -> %s: %s",
                        moved_new,
                        moved_old,
                        e,
                        exc_info=True,
                    )
                failed.update(group_failed)
                continue
            for entry, new_strm_path, _ in group:
                old_strm_path = Path(entry["local_strm_path"])
                relocated = moved_new / old_strm_path.relative_to(moved_old)
                if relocated == new_strm_path:
                    continue
                try:
                    relocated.unlink()
                    logger.info("已删除改名后残留 STRM: %s", relocated)
                except FileNotFoundError:
                    pass
                except OSError as e:
                    logger.warning(
                        "删除改名后残留 STRM 失败 %s: %s", relocated, e
                    )

        for entry, new_strm_path, local_root in renames:
            pickcode = entry.get("pickcode", "")
            if pickcode in moved_pickcodes:
                continue
            old_strm_path = Path(entry["local_strm_path"])
            if not self._migrate_strm_file(entry, new_strm_path):
                failed.add(pickcode)
                continue
            prune_starts.append((old_strm_path.parent, local_root))
        for start, root in sorted(
            prune_starts, key=lambda item: len(item[0].parts), reverse=True
        ):
            self._prune_empty_dirs(start, root)
        return failed

    def _prune_empty_dirs(self, start: Path, root: Path) -> None:
        """
        自下而上清理迁移后留下的空目录，止于本地同步根

        :param start (Path): 起始目录
        :param root (Path): 本地同步根，不会被删除
        """
        current = start
        while _is_strictly_under(current, root):
            try:
                next(current.iterdir())
                return
            except StopIteration:
                pass
            except OSError:
                return
            try:
                current.rmdir()
            except OSError:
                return
            logger.info("已清理空目录: %s", current)
            current = current.parent

    def _migrate_strm_file(
        self, old_entry: Dict[str, str], new_strm_path: Path
    ) -> bool:
        """
        安全迁移本地 STRM 文件到新路径

        目标已存在时保留源和目标并返回失败；移动失败时仅在目标仍不存在的情况下
        按现有内容规则重新生成

        :param old_entry (Dict): 数据库中旧文件记录
        :param new_strm_path (Path): 新 STRM 文件路径

        :return bool: 新位置已就绪时为 True
        """
        old_strm_path = Path(old_entry["local_strm_path"])
        if old_strm_path == new_strm_path:
            return True
        self._assert_local_containment(new_strm_path)
        if new_strm_path.exists() or new_strm_path.is_symlink():
            logger.warning(
                "改名目标已存在，保留源和目标: %s -> %s",
                old_strm_path,
                new_strm_path,
            )
            return False
        new_strm_path.parent.mkdir(parents=True, exist_ok=True)
        self._assert_local_containment(new_strm_path)
        if old_strm_path.exists():
            try:
                old_strm_path.replace(new_strm_path)
            except OSError as e:
                logger.warning(
                    "迁移 STRM 失败 %s -> %s: %s", old_strm_path, new_strm_path, e
                )
                if new_strm_path.exists():
                    return False
            else:
                logger.info("已迁移 STRM: %s -> %s", old_strm_path, new_strm_path)
                return True
        try:
            if not self._ensure_strm_file(
                new_strm_path,
                old_entry.get("pickcode", ""),
                force=self._overwrite_mode == "always",
            ):
                return False
        except (OSError, ValueError) as e:
            logger.warning("迁移后生成 STRM 失败 %s: %s", new_strm_path, e)
            return False
        if not new_strm_path.exists():
            return False
        if old_strm_path.exists():
            try:
                old_strm_path.unlink()
            except FileNotFoundError:
                pass
            except OSError as e:
                logger.warning("迁移后清理旧 STRM 失败 %s: %s", old_strm_path, e)
        return True

    def _ensure_strm_file(
        self, strm_path: Path, pickcode: str, force: bool = False
    ) -> bool:
        """
        写入单个 STRM 文件，写盘前再次确认本地根 containment 与既有内容归属

        :param strm_path (Path): STRM 目标路径
        :param pickcode (str): 115 pickcode
        :param force (bool): 是否强制覆盖同一 pickcode 的既有文件

        :return bool: 目标已确认指向当前 pickcode 时为 True

        :raises ValueError: 未配置跳转地址或路径越界
        :raises OSError: 文件写入失败
        """
        self._assert_local_containment(strm_path)
        target_exists = strm_path.exists() or strm_path.is_symlink()
        if target_exists and self._overwrite_mode == "never":
            if _strm_has_pickcode(strm_path, pickcode):
                if not force:
                    return True
            elif not force:
                logger.warning(
                    "STRM 目标已存在且不属于当前 pickcode，跳过: %s (pickcode=%s)",
                    strm_path,
                    pickcode,
                )
                return False
        if not self._url_prefix:
            raise ValueError("未配置 STRM 跳转地址，请先在 P115 配置中填写服务地址")
        strm_path.parent.mkdir(parents=True, exist_ok=True)
        self._assert_local_containment(strm_path)
        strm_url = f"{self._url_prefix}/api/v1/plugin/P115StrmHelper/redirect_url?pickcode={pickcode}"
        strm_path.write_text(strm_url, encoding="utf-8")
        return _strm_has_pickcode(strm_path, pickcode)


strm_generator: Optional[StrmGenerator] = None


def get_strm_generator(client: P115ClientWrapper, url_prefix: str = "") -> StrmGenerator:
    """
    获取 STRM 生成器单例并刷新其依赖引用

    单例在首次调用时创建；后续每次调用都用最新的 client 与 URL 前缀
    刷新实例引用，避免 Cookie 热重载或前缀修改后仍使用旧引用

    :param client (P115ClientWrapper): 最新的 115 客户端包装
    :param url_prefix (str): STRM 跳转服务 URL 前缀

    :return StrmGenerator: 单例生成器
    """
    global strm_generator
    if strm_generator is None:
        strm_generator = StrmGenerator(client, url_prefix)
        return strm_generator
    if client is not None:
        strm_generator._client = client
    if url_prefix:
        strm_generator._url_prefix = url_prefix.rstrip("/")
    return strm_generator