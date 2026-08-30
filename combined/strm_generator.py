import threading
from os import name as os_name
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from logger import logger
from p115_client_wrapper import P115ClientWrapper
from database import db


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


def _iter_files_115(client_wrapper: P115ClientWrapper, cid: int):
    """
    递归遍历 115 目录下的所有文件（非目录）

    使用 iter_files_with_path（Android API, proapi.115.com）替代 fs_files（webapi.115.com），
    避免 webapi 域名被风控返回 405 的问题

    :param client_wrapper: P115ClientWrapper 实例
    :param cid: 起始目录 ID
    """
    from p115client.tool.iterdir import iter_files_with_path
    from p115client.tool.attr import normalize_attr

    client = client_wrapper.client
    if client is None:
        return
    for attr in iter_files_with_path(
        client, cid, type=99, cur=0, app="android",
        cooldown=0.5, escape=None,
    ):
        yield normalize_attr(attr)


class StrmGenerator:
    def __init__(self, client: P115ClientWrapper, url_prefix: str = ""):
        self._client = client
        self._url_prefix = url_prefix.rstrip("/")
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
        self._rust_processor = None
        self._sync_lock = threading.RLock()

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

    def set_use_rust(self, enabled: bool):
        self._use_rust = enabled

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
                self._ensure_strm_file(strm_path, pickcode, force=force)
            except OSError as e:
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
        try:
            self.reset_cancel()
            if kwargs.get("rmt_mediaext") is not None:
                self.set_config(
                    rmt_mediaext=kwargs.get("rmt_mediaext", ""),
                    download_mediaext=kwargs.get("download_mediaext", ""),
                    auto_download_mediainfo=kwargs.get("auto_download_mediainfo", False),
                )
            history_id = db.add_sync_history("full")
            self._set_progress("scanning", message="准备开始全量同步...")
            total_new = 0
            total_deleted = 0
            total_failed = 0
            total_count = 0

            try:
                all_files = []
                rust_items = []
                for mapping in path_mappings:
                    if self._cancel_flag.is_set():
                        self._set_progress("cancelled", message="全量同步已取消")
                        break
                    if mapping.get("enabled") is False:
                        continue
                    pan_path = mapping["from"]
                    local_path = mapping["to"]
                    self._set_progress("scanning", message=f"正在扫描目录: {pan_path}")
                    logger.info("开始同步目录: %s -> %s", pan_path, local_path)

                    try:
                        cid = self._resolve_pan_path(pan_path)

                        for attr in _iter_files_115(self._client, cid):
                            if self._cancel_flag.is_set():
                                break
                            if attr.get("is_dir"):
                                continue
                            name = attr.get("name", "")
                            ext = Path(name).suffix.lower()
                            pickcode = attr.get("pickcode", "")
                            pan_full_path = attr.get("path", "")

                            if self._auto_download_mediainfo and ext in self._download_mediaext:
                                local_file_path = self._to_local_path(
                                    pan_full_path, pan_path, local_path
                                )
                                if local_file_path is not None and not local_file_path.exists():
                                    self._download_aux_file(pickcode, name, local_file_path)

                            if ext in self._rmt_mediaext:
                                if not pickcode:
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
                                local_strm_path_orig = self._to_local_path(
                                    pan_full_path, pan_path, local_path
                                )
                                if local_strm_path_orig is None:
                                    continue
                                local_strm_path = local_strm_path_orig.with_suffix(".strm")
                                # Rust 模式跳过 Python 写 STRM，避免双写，
                                # 批处理失败时由 _process_rust_batch 统一回填
                                if not self._use_rust:
                                    self._ensure_strm_file(local_strm_path, pickcode)
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
                                self._set_progress("scanning", current=len(all_files), message=f"已扫描 {len(all_files)} 个文件")
                    except Exception as e:
                        logger.error("同步目录失败 %s: %s", pan_path, e, exc_info=True)
                        total_failed += 1

                if self._use_rust and rust_items and not self._cancel_flag.is_set():
                    files_by_pan_path = {
                        f["pan_path"]: (Path(f["local_strm_path"]), f["pickcode"], False)
                        for f in all_files
                    }
                    self._process_rust_batch(rust_items, files_by_pan_path)

                if not self._cancel_flag.is_set() and all_files:
                    db.batch_add_files(all_files)
                    total_new = len(all_files)
                    total_count = total_new

                    if self._cleanup_deleted:
                        seen_pickcodes = {f["pickcode"] for f in all_files}
                        for mapping in path_mappings:
                            if mapping.get("enabled") is False:
                                continue
                            pan_path = mapping["from"]
                            for f in db.get_active_files_by_parent(pan_path):
                                if f["pickcode"] not in seen_pickcodes:
                                    db.mark_file_deleted(f["pickcode"])
                                    strm_path = Path(f["local_strm_path"])
                                    if strm_path.exists():
                                        try:
                                            strm_path.unlink()
                                            logger.info("已删除残留 STRM: %s", strm_path)
                                        except OSError:
                                            pass
                                    total_deleted += 1

                self._set_progress("processing", current=total_count, total=total_count, message="同步完成，正在写入数据库...")
                db.finish_sync_history(
                    history_id, total_count, total_new, total_deleted, total_failed
                )

                self._set_progress("completed", current=total_count, total=total_count, message=f"全量同步完成: 新增 {total_new}，删除 {total_deleted}，失败 {total_failed}")
                logger.info(
                    "全量同步完成: 新增=%d, 删除=%d, 失败=%d",
                    total_new, total_deleted, total_failed,
                )
            except Exception as e:
                self._set_progress("error", message=f"全量同步异常: {e}")
                logger.error("全量同步异常: %s", e, exc_info=True)
                db.finish_sync_history(
                    history_id, total_count, total_new, total_deleted, total_failed, str(e)
                )

            return {
                "history_id": history_id,
                "total": total_count,
                "new": total_new,
                "deleted": total_deleted,
                "failed": total_failed,
                "cancelled": self._cancel_flag.is_set(),
            }
        finally:
            self._sync_lock.release()

    def incremental_sync(self, path_mappings: List[Dict[str, str]], **kwargs) -> Dict[str, Any]:
        if not self._sync_lock.acquire(blocking=False):
            logger.warning("增量同步已在进行中，忽略重复请求")
            return {"error": "sync_already_running", "message": "增量同步已在进行中"}
        try:
            self.reset_cancel()
            if kwargs.get("rmt_mediaext") is not None:
                self.set_config(
                    rmt_mediaext=kwargs.get("rmt_mediaext", ""),
                    download_mediaext=kwargs.get("download_mediaext", ""),
                    auto_download_mediainfo=kwargs.get("auto_download_mediainfo", False),
                )
            if db.count_active_files() == 0:
                logger.info("数据库中无同步记录，首次增量同步自动切换为全量同步")
                return self.full_sync(path_mappings, **kwargs)

            history_id = db.add_sync_history("incremental")
            self._set_progress("scanning", message="准备开始增量同步...")
            total_new = 0
            total_changed = 0
            total_deleted = 0
            total_failed = 0
            total_unchanged = 0

            try:
                all_new_files = []
                seen_pickcodes = set()
                rust_items = []
                files_by_pan_path: Dict[str, Tuple[Path, str, bool]] = {}
                # 所有映射的存量记录汇总后再统一判定删除，
                # 避免前序映射扫描时把尚未轮到的兄弟目录文件误判为已删除
                all_existing: Dict[str, Dict] = {}

                for mapping in path_mappings:
                    if self._cancel_flag.is_set():
                        self._set_progress("cancelled", message="增量同步已取消")
                        break
                    if mapping.get("enabled") is False:
                        continue
                    pan_path = mapping["from"]
                    local_path = mapping["to"]
                    self._set_progress("scanning", message=f"正在扫描目录: {pan_path}")
                    logger.info("开始增量同步目录: %s -> %s", pan_path, local_path)

                    for f in db.get_active_files_by_parent(pan_path):
                        all_existing[f["pickcode"]] = {
                            "pickcode": f["pickcode"],
                            "sha1": f["sha1"],
                            "pan_path": f["pan_path"],
                            "local_strm_path": f["local_strm_path"],
                        }

                    try:
                        cid = self._resolve_pan_path(pan_path)

                        for attr in _iter_files_115(self._client, cid):
                            if self._cancel_flag.is_set():
                                break
                            if attr.get("is_dir"):
                                continue
                            name = attr.get("name", "")
                            ext = Path(name).suffix.lower()
                            pickcode = attr.get("pickcode", "")
                            pan_full_path = attr.get("path", "")

                            if self._auto_download_mediainfo and ext in self._download_mediaext:
                                local_file_path = self._to_local_path(
                                    pan_full_path, pan_path, local_path
                                )
                                if local_file_path is not None and not local_file_path.exists():
                                    self._download_aux_file(pickcode, name, local_file_path)

                            if ext not in self._rmt_mediaext:
                                continue
                            if not pickcode:
                                continue

                            seen_pickcodes.add(pickcode)
                            sha1 = attr.get("sha1", "")

                            if pickcode not in all_existing:
                                local_strm_path_orig = self._to_local_path(
                                    pan_full_path, pan_path, local_path
                                )
                                if local_strm_path_orig is None:
                                    continue
                                local_strm_path = local_strm_path_orig.with_suffix(".strm")
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
                                        local_strm_path, pickcode, False
                                    )
                                else:
                                    self._ensure_strm_file(local_strm_path, pickcode)
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
                            elif all_existing[pickcode]["sha1"] != sha1:
                                local_strm_path_orig = self._to_local_path(
                                    pan_full_path, pan_path, local_path
                                )
                                if local_strm_path_orig is None:
                                    continue
                                local_strm_path = local_strm_path_orig.with_suffix(".strm")
                                # 文件同时被移动且内容变化时，旧路径 STRM 需清理，
                                # 否则媒体库会出现指向同一文件的重复条目
                                old_strm_path = Path(all_existing[pickcode]["local_strm_path"])
                                if old_strm_path != local_strm_path and old_strm_path.exists():
                                    try:
                                        old_strm_path.unlink()
                                        logger.info(
                                            "已删除路径变更文件的旧 STRM: %s", old_strm_path
                                        )
                                    except OSError as e:
                                        logger.warning(
                                            "删除旧路径 STRM 失败 %s: %s", old_strm_path, e
                                        )
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
                                        local_strm_path, pickcode, True
                                    )
                                else:
                                    self._ensure_strm_file(local_strm_path, pickcode, force=True)
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
                            elif all_existing[pickcode]["pan_path"] != pan_full_path:
                                # 115 中文件被移动或重命名：pickcode 与 sha1 未变但路径已变
                                local_strm_path_orig = self._to_local_path(
                                    pan_full_path, pan_path, local_path
                                )
                                if local_strm_path_orig is None:
                                    continue
                                local_strm_path = local_strm_path_orig.with_suffix(".strm")
                                self._migrate_strm_file(
                                    all_existing[pickcode], local_strm_path
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
                            self._set_progress("scanning", current=processed, message=f"已扫描 {processed} 个文件")

                    except Exception as e:
                        logger.error("增量同步目录失败 %s: %s", pan_path, e, exc_info=True)
                        total_failed += 1

                if not self._cancel_flag.is_set():
                    deleted_pickcodes = [
                        pc for pc in all_existing if pc not in seen_pickcodes
                    ]
                    for pc in deleted_pickcodes:
                        entry = all_existing[pc]
                        strm_path = Path(entry["local_strm_path"])
                        if strm_path.exists():
                            try:
                                strm_path.unlink()
                                logger.info("已删除残留 STRM: %s", strm_path)
                            except OSError as e:
                                logger.warning("删除 STRM 文件失败 %s: %s", strm_path, e)
                        db.mark_file_deleted(pc)
                    total_deleted += len(deleted_pickcodes)

                if self._use_rust and rust_items and not self._cancel_flag.is_set():
                    self._process_rust_batch(rust_items, files_by_pan_path)

                self._set_progress("processing", message="正在写入数据库...")
                if not self._cancel_flag.is_set() and all_new_files:
                    db.batch_add_files(all_new_files)

                total_count = total_new + total_changed + total_unchanged + total_deleted
                db.finish_sync_history(
                    history_id, total_count, total_new + total_changed, total_deleted, total_failed
                )

                self._set_progress("completed", current=total_count, total=total_count, message=f"增量同步完成: 新增 {total_new}，变更 {total_changed}，删除 {total_deleted}，失败 {total_failed}")
                logger.info(
                    "增量同步完成: 新增=%d, 变更=%d, 未变=%d, 删除=%d, 失败=%d",
                    total_new, total_changed, total_unchanged, total_deleted, total_failed,
                )
            except Exception as e:
                self._set_progress("error", message=f"增量同步异常: {e}")
                logger.error("增量同步异常: %s", e, exc_info=True)
                db.finish_sync_history(
                    history_id, 0, 0, 0, total_failed, str(e)
                )

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
        finally:
            self._sync_lock.release()

    def _to_local_path(self, pan_full_path: str, base_pan_path: str, local_strm_dir: str) -> Optional[Path]:
        if not pan_full_path.startswith(base_pan_path):
            # 前缀不匹配说明路径不在同步目录下，避免拼接出越界路径
            logger.warning("路径不在同步目录内，跳过: %s (基目录 %s)", pan_full_path, base_pan_path)
            return None
        rel_path = pan_full_path[len(base_pan_path):].lstrip("/")
        return sanitize_path_parts(Path(local_strm_dir) / rel_path)

    def _download_aux_file(self, pickcode: str, name: str, local_file_path: Path) -> None:
        """
        下载媒体附属文件（nfo、海报等）到本地 STRM 目录

        :param pickcode (str): 文件 pickcode
        :param name (str): 原始文件名，用于日志
        :param local_file_path (Path): 本地保存路径
        """
        local_file_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            file_url = self._client.download_url(pickcode)
            if file_url:
                import httpx
                file_resp = httpx.get(file_url, follow_redirects=True, timeout=30)
                local_file_path.write_bytes(file_resp.content)
                logger.info("已下载附属文件: %s", name)
        except Exception as e:
            logger.warning("下载附属文件失败 %s: %s", name, e)

    def _migrate_strm_file(self, old_entry: Dict[str, str], new_strm_path: Path):
        """
        迁移本地 STRM 文件到新路径（115 中文件被移动或重命名时）

        优先移动原 STRM 文件以保留内容；移动失败时按现有内容规则重新生成

        :param old_entry (Dict): 数据库中旧文件记录
        :param new_strm_path (Path): 新 STRM 文件路径
        """
        old_strm_path = Path(old_entry["local_strm_path"])
        if old_strm_path != new_strm_path:
            new_strm_path.parent.mkdir(parents=True, exist_ok=True)
            try:
                if old_strm_path.exists():
                    if new_strm_path.exists():
                        new_strm_path.unlink()
                    old_strm_path.replace(new_strm_path)
                    logger.info(
                        "已迁移 STRM: %s -> %s", old_strm_path, new_strm_path
                    )
                    return
            except OSError as e:
                logger.warning(
                    "迁移 STRM 失败 %s -> %s: %s", old_strm_path, new_strm_path, e
                )
        self._ensure_strm_file(new_strm_path, old_entry.get("pickcode", ""))

    def _ensure_strm_file(self, strm_path: Path, pickcode: str, force: bool = False):
        if not force and self._overwrite_mode == "never" and strm_path.exists():
            return
        strm_path.parent.mkdir(parents=True, exist_ok=True)
        strm_url = f"{self._url_prefix}/api/v1/plugin/P115StrmHelper/redirect_url?pickcode={pickcode}"
        strm_path.write_text(strm_url, encoding="utf-8")


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