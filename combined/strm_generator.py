import os
import threading
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from p115client.tool.iterdir import iter_files_with_path_skim

from logger import logger
from p115_client_wrapper import P115ClientWrapper
from database import db


class StrmGenerator:
    def __init__(self, client: P115ClientWrapper, url_prefix: str = ""):
        self._client = client
        self._url_prefix = url_prefix.rstrip("/")
        self._cancel_flag = threading.Event()
        self._progress_callback: Optional[Callable] = None

    def set_progress_callback(self, cb: Callable):
        self._progress_callback = cb

    def cancel(self):
        self._cancel_flag.set()
        logger.info("同步已请求取消")

    def reset_cancel(self):
        self._cancel_flag.clear()

    def full_sync(self, path_mappings: List[Dict[str, str]]) -> Dict[str, Any]:
        self.reset_cancel()
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
                pan_path = mapping["from"]
                local_path = mapping["to"]
                logger.info("开始同步目录: %s -> %s", pan_path, local_path)

                try:
                    # 通过路径获取目录 CID
                    resp = self._client._client.fs_dir_getid(pan_path)
                    if not isinstance(resp, dict):
                        logger.warning("获取目录ID失败: %s 返回非 dict", pan_path)
                        total_failed += 1
                        continue
                    cid = resp.get("id", -1)
                    if cid <= 0:
                        logger.warning("目录不存在: %s (cid=%s)", pan_path, cid)
                        total_failed += 1
                        continue

                    # 遍历目录下所有文件
                    for attr in iter_files_with_path_skim(self._client._client, cid):
                        if self._cancel_flag.is_set():
                            break
                        if attr.get("is_dir"):
                            continue
                        name = attr.get("name", "")
                        if not self._is_media_file(name):
                            continue
                        pickcode = attr.get("pickcode", "")
                        if not pickcode:
                            continue
                        pan_full_path = attr.get("path", f"{pan_path}/{name}")
                        local_strm_path = self._to_local_path(
                            pan_full_path, pan_path, local_path
                        )
                        self._ensure_strm_file(local_strm_path, pickcode)
                        all_files.append({
                            "pickcode": pickcode,
                            "file_name": name,
                            "file_size": attr.get("size", 0),
                            "file_type": Path(name).suffix.lower(),
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

    def _is_media_file(self, name: str) -> bool:
        ext = Path(name).suffix.lower()
        return ext in (
            ".mp4", ".mkv", ".avi", ".mov", ".wmv", ".flv", ".webm",
            ".ts", ".mts", ".m2ts", ".iso", ".bdmv",
            ".mpg", ".mpeg", ".rmvb", ".3gp", ".ogm",
            ".m4v", ".asf", ".divx",
            ".srt", ".ass", ".ssa", ".sub", ".idx",
            ".sup", ".pgs", ".mks",
            ".mp3", ".flac", ".wav", ".aac", ".ogg", ".wma",
            ".dts", ".ac3", ".eac3", ".truehd",
        )

    def _to_local_path(self, pan_full_path: str, base_pan_path: str, local_strm_dir: str) -> Path:
        rel_path = pan_full_path[len(base_pan_path):].lstrip("/")
        return Path(local_strm_dir) / rel_path

    def _ensure_strm_file(self, strm_path: Path, pickcode: str):
        strm_path.parent.mkdir(parents=True, exist_ok=True)
        strm_url = f"{self._url_prefix}/api/v1/plugin/P115StrmHelper/redirect_url?pickcode={pickcode}"
        strm_path.write_text(strm_url, encoding="utf-8")


strm_generator: Optional[StrmGenerator] = None


def get_strm_generator(client: P115ClientWrapper, url_prefix: str = "") -> StrmGenerator:
    global strm_generator
    if strm_generator is None:
        strm_generator = StrmGenerator(client, url_prefix)
    return strm_generator
