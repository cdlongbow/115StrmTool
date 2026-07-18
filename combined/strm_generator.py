import threading
from os import name as os_name
from pathlib import Path
from time import monotonic, sleep
from typing import Any, Callable, Dict, List, Optional

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


def _iter_files_115(client_wrapper: P115ClientWrapper, cid: int, path_cache: Dict[int, str] = None):
    """
    递归遍历 115 目录下的所有文件（非目录）
    使用 fs_files API 分页获取，递归子目录

    :param client_wrapper: P115ClientWrapper 实例
    :param cid: 起始目录 ID
    :param path_cache: 可选，用于追踪目录 CID 到路径的映射，在遍历过程中填充
    """
    from app_ver import get_real_app_ver
    app_ver = get_real_app_ver()
    http_client = client_wrapper.client
    if http_client is None:
        return
    if path_cache is None:
        path_cache = {}
    path_cache[cid] = ""
    stack = [(cid, "")]
    _fs_files_cooldown = 0.5
    _last_call = 0.0
    while stack:
        current_cid, current_rel = stack.pop()
        offset = 0
        limit = 1000
        while True:
            try:
                now = monotonic()
                remaining = _fs_files_cooldown - (now - _last_call)
                if remaining > 0:
                    sleep(remaining)
                resp = http_client.fs_files({
                    "cid": current_cid,
                    "limit": limit,
                    "offset": offset,
                    "app_ver": app_ver,
                    "cur": 1,
                    "fc_mix": 1,
                })
                _last_call = monotonic()
            except Exception as e:
                logger.warning("遍历目录失败 cid=%s: %s", current_cid, e)
                break
            if not isinstance(resp, dict):
                logger.warning("遍历目录响应不是 dict cid=%s: %s", current_cid, type(resp))
                break
            items = resp.get("data") or resp.get("Data") or []
            if not items:
                break
            for item in items:
                is_dir = False
                if "fid" not in item:
                    is_dir = True
                child_cid = item.get("cid")
                if is_dir:
                    if not child_cid or str(child_cid) == "0":
                        logger.warning("跳过无效子目录 cid=%s name=%s", child_cid, item.get("n", ""))
                        continue
                    dir_name = item.get("n", "")
                    child_rel = f"{current_rel}/{dir_name}" if current_rel else dir_name
                    path_cache[child_cid] = child_rel
                    stack.append((child_cid, child_rel))
                else:
                    attr = {
                        "name": item.get("n") or item.get("name", ""),
                        "is_dir": is_dir,
                        "size": item.get("s") or item.get("size", 0),
                        "pickcode": item.get("pc") or item.get("pickcode") or "",
                        "pick_code": item.get("pc") or item.get("pickcode") or "",
                        "sha1": item.get("sha") or item.get("sha", ""),
                        "path": "",
                        "id": child_cid if is_dir else item.get("fid", 0),
                        "parent_id": str(current_cid),
                    }
                    yield attr
            if len(items) < limit:
                break
            offset += limit


class StrmGenerator:
    def __init__(self, client: P115ClientWrapper, url_prefix: str = ""):
        self._client = client
        self._url_prefix = url_prefix.rstrip("/")
        self._cancel_flag = threading.Event()
        self._progress_callback: Optional[Callable] = None
        self._rmt_mediaext: set = {
            ".mp4", ".mkv", ".ts", ".iso", ".rmvb", ".avi", ".mov", ".mpeg", ".mpg",
            ".wmv", ".3gp", ".asf", ".m4v", ".flv", ".m2ts", ".tp", ".f4v", ".webm",
        }
        self._download_mediaext: set = {
            ".srt", ".ssa", ".ass", ".sup", ".pgs", ".sub", ".idx",
        }
        self._auto_download_mediainfo = False
        self._overwrite_mode = "never"
        self._use_rust = False
        self._rust_processor = None

    def set_progress_callback(self, cb: Callable):
        self._progress_callback = cb

    def set_config(
        self,
        rmt_mediaext: str = "",
        download_mediaext: str = "",
        auto_download_mediainfo: bool = False,
        overwrite_mode: str = "never",
    ):
        if rmt_mediaext:
            self._rmt_mediaext = {f".{e.strip().lower()}" for e in rmt_mediaext.replace("，", ",").split(",") if e.strip()}
        if download_mediaext:
            self._download_mediaext = {f".{e.strip().lower()}" for e in download_mediaext.replace("，", ",").split(",") if e.strip()}
        self._auto_download_mediainfo = auto_download_mediainfo
        self._overwrite_mode = overwrite_mode

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

    def cancel(self):
        self._cancel_flag.set()
        logger.info("同步已请求取消")

    def reset_cancel(self):
        self._cancel_flag.clear()

    def _resolve_pan_path(self, pan_path: str) -> int:
        try:
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
        except Exception as e:
            raise

    def full_sync(self, path_mappings: List[Dict[str, str]], **kwargs) -> Dict[str, Any]:
        self.reset_cancel()
        if kwargs.get("rmt_mediaext") is not None:
            self.set_config(
                rmt_mediaext=kwargs.get("rmt_mediaext", ""),
                download_mediaext=kwargs.get("download_mediaext", ""),
                auto_download_mediainfo=kwargs.get("auto_download_mediainfo", False),
            )
        history_id = db.add_sync_history("full")
        total_new = 0
        total_deleted = 0
        total_failed = 0
        total_count = 0

        try:
            all_files = []
            rust_items = []
            for mapping in path_mappings:
                if self._cancel_flag.is_set():
                    break
                if mapping.get("enabled") is False:
                    continue
                pan_path = mapping["from"]
                local_path = mapping["to"]
                logger.info("开始同步目录: %s -> %s", pan_path, local_path)

                try:
                    cid = self._resolve_pan_path(pan_path)
                    path_cache: Dict[int, str] = {}

                    for attr in _iter_files_115(self._client, cid, path_cache):
                        if self._cancel_flag.is_set():
                            break
                        if attr.get("is_dir"):
                            continue
                        name = attr.get("name", "")
                        ext = Path(name).suffix.lower()
                        pickcode = attr.get("pickcode") or attr.get("pick_code") or ""
                        file_id = attr.get("id", 0)
                        parent_id = attr.get("parent_id", 0)
                        rel_path = path_cache.get(parent_id, "")
                        pan_full_path = f"{pan_path}/{rel_path}/{name}" if rel_path else f"{pan_path}/{name}"

                        if self._auto_download_mediainfo and ext in self._download_mediaext:
                            local_file_path = self._to_local_path(
                                pan_full_path, pan_path, local_path
                            )
                            if not local_file_path.exists():
                                local_file_path.parent.mkdir(parents=True, exist_ok=True)
                                try:
                                    dl_resp = self._client._client.download_url(pickcode)
                                    if isinstance(dl_resp, dict):
                                        from p115client import check_response
                                        dl_resp = check_response(dl_resp)
                                        file_url = dl_resp.get("url") or dl_resp.get("file_url", "")
                                        if file_url:
                                            import httpx
                                            file_resp = httpx.get(file_url, follow_redirects=True, timeout=30)
                                            local_file_path.write_bytes(file_resp.content)
                                            logger.info("已下载附属文件: %s", name)
                                except Exception as e:
                                    logger.warning("下载附属文件失败 %s: %s", name, e)

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
                            local_strm_path = local_strm_path_orig.with_suffix(".strm")
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
                except Exception as e:
                    logger.error("同步目录失败 %s: %s", pan_path, e, exc_info=True)
                    total_failed += 1

            if self._use_rust and rust_items and not self._cancel_flag.is_set():
                processor = self._get_rust_processor()
                if processor:
                    try:
                        import json
                        batch_json = json.dumps(rust_items)
                        results = processor.process_batch(batch_json)
                        rust_strm_count = getattr(results, "strm_results_count", 0) or 0
                        rust_fail_count = len(getattr(results, "fail_results", []) or [])
                        logger.info(
                            "Rust 加速处理完成: STRM=%d, 失败=%d",
                            rust_strm_count, rust_fail_count
                        )
                        for fail_info in (getattr(results, "fail_results", []) or []):
                            logger.warning(
                                "Rust STRM 生成失败: path=%s reason=%s",
                                getattr(fail_info, "path_in_pan", "?"),
                                getattr(fail_info, "reason", "?"),
                            )
                    except Exception as e:
                        logger.error("Rust 批处理失败: %s", e, exc_info=True)

            if not self._cancel_flag.is_set() and all_files:
                db.batch_add_files(all_files)
                total_new = len(all_files)
                total_count = total_new

            db.finish_sync_history(
                history_id, total_count, total_new, total_deleted, total_failed
            )

            logger.info(
                "全量同步完成: 新增=%d, 失败=%d",
                total_new,
                total_failed,
            )
        except Exception as e:
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

    def incremental_sync(self, path_mappings: List[Dict[str, str]], **kwargs) -> Dict[str, Any]:
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
        total_new = 0
        total_changed = 0
        total_deleted = 0
        total_failed = 0
        total_unchanged = 0

        try:
            all_new_files = []
            seen_pickcodes = set()
            rust_items = []

            for mapping in path_mappings:
                if self._cancel_flag.is_set():
                    break
                if mapping.get("enabled") is False:
                    continue
                pan_path = mapping["from"]
                local_path = mapping["to"]
                logger.info("开始增量同步目录: %s -> %s", pan_path, local_path)

                existing_map: Dict[str, Dict] = {}
                for f in db.get_active_files_by_parent(pan_path):
                    existing_map[f["pickcode"]] = {
                        "sha1": f["sha1"],
                        "pan_path": f["pan_path"],
                        "local_strm_path": f["local_strm_path"],
                    }

                try:
                    cid = self._resolve_pan_path(pan_path)
                    path_cache: Dict[int, str] = {}

                    for attr in _iter_files_115(self._client, cid, path_cache):
                        if self._cancel_flag.is_set():
                            break
                        if attr.get("is_dir"):
                            continue
                        name = attr.get("name", "")
                        ext = Path(name).suffix.lower()
                        pickcode = attr.get("pickcode") or attr.get("pick_code") or ""
                        file_id = attr.get("id", 0)
                        parent_id = attr.get("parent_id", 0)
                        rel_path = path_cache.get(parent_id, "")
                        pan_full_path = f"{pan_path}/{rel_path}/{name}" if rel_path else f"{pan_path}/{name}"

                        if self._auto_download_mediainfo and ext in self._download_mediaext:
                            local_file_path = self._to_local_path(
                                pan_full_path, pan_path, local_path
                            )
                            if not local_file_path.exists():
                                local_file_path.parent.mkdir(parents=True, exist_ok=True)
                                try:
                                    dl_resp = self._client._client.download_url(pickcode)
                                    if isinstance(dl_resp, dict):
                                        from p115client import check_response
                                        dl_resp = check_response(dl_resp)
                                        file_url = dl_resp.get("url") or dl_resp.get("file_url", "")
                                        if file_url:
                                            import httpx
                                            file_resp = httpx.get(file_url, follow_redirects=True, timeout=30)
                                            local_file_path.write_bytes(file_resp.content)
                                            logger.info("已下载附属文件: %s", name)
                                except Exception as e:
                                    logger.warning("下载附属文件失败 %s: %s", name, e)

                        if ext not in self._rmt_mediaext:
                            continue
                        if not pickcode:
                            continue

                        seen_pickcodes.add(pickcode)
                        sha1 = attr.get("sha1", "")

                        if pickcode not in existing_map:
                            local_strm_path_orig = self._to_local_path(
                                pan_full_path, pan_path, local_path
                            )
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
                        elif existing_map[pickcode]["sha1"] != sha1:
                            local_strm_path_orig = self._to_local_path(
                                pan_full_path, pan_path, local_path
                            )
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
                        else:
                            total_unchanged += 1

                    deleted_pickcodes = [
                        pc for pc in existing_map if pc not in seen_pickcodes
                    ]
                    for pc in deleted_pickcodes:
                        entry = existing_map[pc]
                        strm_path = Path(entry["local_strm_path"])
                        if strm_path.exists():
                            try:
                                strm_path.unlink()
                                logger.info("已删除残留 STRM: %s", strm_path)
                            except OSError as e:
                                logger.warning("删除 STRM 文件失败 %s: %s", strm_path, e)
                        db.mark_file_deleted(pc)
                    total_deleted += len(deleted_pickcodes)

                except Exception as e:
                    logger.error("增量同步目录失败 %s: %s", pan_path, e, exc_info=True)
                    total_failed += 1

            if self._use_rust and rust_items and not self._cancel_flag.is_set():
                processor = self._get_rust_processor()
                if processor:
                    try:
                        import json
                        batch_json = json.dumps(rust_items)
                        results = processor.process_batch(batch_json)
                        rust_strm_count = getattr(results, "strm_results_count", 0) or 0
                        rust_fail_count = len(getattr(results, "fail_results", []) or [])
                        logger.info(
                            "Rust 加速处理完成: STRM=%d, 失败=%d",
                            rust_strm_count, rust_fail_count
                        )
                        for fail_info in (getattr(results, "fail_results", []) or []):
                            logger.warning(
                                "Rust STRM 生成失败: path=%s reason=%s",
                                getattr(fail_info, "path_in_pan", "?"),
                                getattr(fail_info, "reason", "?"),
                            )
                    except Exception as e:
                        logger.error("Rust 批处理失败: %s", e, exc_info=True)

            if not self._cancel_flag.is_set() and all_new_files:
                db.batch_add_files(all_new_files)

            total_count = total_new + total_changed + total_unchanged + total_deleted
            db.finish_sync_history(
                history_id, total_count, total_new + total_changed, total_deleted, total_failed
            )

            logger.info(
                "增量同步完成: 新增=%d, 变更=%d, 未变=%d, 删除=%d, 失败=%d",
                total_new, total_changed, total_unchanged, total_deleted, total_failed,
            )
        except Exception as e:
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

    def _to_local_path(self, pan_full_path: str, base_pan_path: str, local_strm_dir: str) -> Path:
        rel_path = pan_full_path[len(base_pan_path):].lstrip("/")
        return sanitize_path_parts(Path(local_strm_dir) / rel_path)

    def _ensure_strm_file(self, strm_path: Path, pickcode: str, force: bool = False):
        if not force and self._overwrite_mode == "never" and strm_path.exists():
            return
        strm_path.parent.mkdir(parents=True, exist_ok=True)
        strm_url = f"{self._url_prefix}/api/v1/plugin/P115StrmHelper/redirect_url?pickcode={pickcode}"
        strm_path.write_text(strm_url, encoding="utf-8")


strm_generator: Optional[StrmGenerator] = None


def get_strm_generator(client: P115ClientWrapper, url_prefix: str = "") -> StrmGenerator:
    global strm_generator
    if strm_generator is None:
        strm_generator = StrmGenerator(client, url_prefix)
    return strm_generator