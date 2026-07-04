import threading
from os import name as os_name
from pathlib import Path
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
    for part in parts:
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
    while stack:
        current_cid, current_rel = stack.pop()
        offset = 0
        limit = 1000
        while True:
            try:
                resp = http_client.fs_files_app({
                    "cid": current_cid,
                    "limit": limit,
                    "offset": offset,
                    "app_ver": app_ver,
                    "cur": 1,
                    "fc_mix": 1,
                }, app="android")
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
                if "s" not in item:
                    is_dir = True
                child_cid = item.get("fid")
                if is_dir:
                    if not child_cid or str(child_cid) == "0":
                        logger.warning("跳过无效子目录 cid=%s name=%s", child_cid, item.get("fn", ""))
                        continue
                    dir_name = item.get("fn", "")
                    child_rel = f"{current_rel}/{dir_name}" if current_rel else dir_name
                    path_cache[child_cid] = child_rel
                    stack.append((child_cid, child_rel))
                else:
                    attr = {
                        "name": item.get("fn") or item.get("name", ""),
                        "is_dir": is_dir,
                        "size": item.get("s") or item.get("size", 0),
                        "pickcode": item.get("pc") or item.get("pickcode") or "",
                        "pick_code": item.get("pc") or item.get("pickcode") or "",
                        "sha1": item.get("sha") or item.get("sha", ""),
                        "path": "",
                        "id": child_cid if is_dir else item.get("fid", 0),
                        "parent_id": item.get("pid", 0),
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

    def _to_local_path(self, pan_full_path: str, base_pan_path: str, local_strm_dir: str) -> Path:
        rel_path = pan_full_path[len(base_pan_path):].lstrip("/")
        return sanitize_path_parts(Path(local_strm_dir) / rel_path)

    def _ensure_strm_file(self, strm_path: Path, pickcode: str):
        if self._overwrite_mode == "never" and strm_path.exists():
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