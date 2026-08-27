from typing import Any, Dict, List
from fastapi import APIRouter

import asyncio
import threading

from checkin_scheduler import checkin_scheduler
from logger import logger
from config_manager import config_manager
from database import db
from exceptions import ClientNotReadyError, ServiceError
from p115_client_wrapper import P115ClientWrapper

router = APIRouter(prefix="/api")

_client: P115ClientWrapper = None


def set_client(client: P115ClientWrapper):
    global _client
    _client = client


def get_client() -> P115ClientWrapper:
    if _client is None:
        raise ClientNotReadyError("115 客户端未初始化，请先配置 Cookie")
    if not _client.is_ready():
        raise ClientNotReadyError()
    return _client


# ── 目录选择与状态 ──


@router.get("/select-directory")
async def select_directory() -> Dict:
    try:
        path = await asyncio.to_thread(_select_directory_sync)
        return {"path": (path or "").replace("\\", "/")}
    except Exception:
        return {"path": ""}


# 目录选择对话框超时（秒）。tkinter 在部分 Windows 环境下可能不弹窗或
# 事件循环不转而永久阻塞，超时后返回空串，由前端回退到手动输入路径
SELECT_DIRECTORY_TIMEOUT = 180.0


def _select_directory_sync() -> str:
    """
    在专用临时线程中打开目录选择对话框，带超时保护

    tkinter 的 Tk 解释器须在单一固定线程内创建与销毁，且不放入默认
    线程池以免卡死的对话框占用公共工作线程；超时或异常时返回空串，
    前端会回退到手动输入

    :return str: 所选目录路径，取消 / 超时 / 异常时返回空串
    """
    holder: List[str] = []

    def _run() -> None:
        try:
            import tkinter
            from tkinter import filedialog
            root = tkinter.Tk()
            root.withdraw()
            root.attributes("-topmost", True)
            try:
                path = filedialog.askdirectory(
                    title="选择 STRM 输出目录", parent=root
                )
                holder.append(path or "")
            finally:
                root.destroy()
        except Exception as e:
            logger.warning("目录选择对话框异常: %s", e, exc_info=True)
            holder.append("")

    worker = threading.Thread(
        target=_run, daemon=True, name="select-directory"
    )
    worker.start()
    worker.join(timeout=SELECT_DIRECTORY_TIMEOUT)
    if worker.is_alive():
        logger.warning(
            "目录选择对话框 %ss 内无响应，回退手动输入", SELECT_DIRECTORY_TIMEOUT
        )
        return ""
    return holder[0] if holder else ""


@router.get("/status")
async def get_status() -> Dict[str, Any]:
    stats = db.get_stats()
    config = config_manager.get()
    client_ready = _client is not None and _client.is_ready()
    user_info = None
    storage = None
    if client_ready:
        try:
            user_info = _client.get_user_info()
            storage = _client.get_storage_info()
        except Exception as e:
            logger.warning("获取 115 用户信息失败: %s", e, exc_info=True)
    return {
        "client_ready": client_ready,
        "stats": stats,
        "user_info": user_info,
        "storage": storage,
        "config": config,
    }


# ── 浏览目录 ──


@router.get("/browse")
async def browse_directory(pid: str = "0", path: str = ""):
    client = get_client()
    try:
        from p115client import check_response
        from p115client.tool.attr import normalize_attr
        resp = client._client.fs_files_app({"cid": pid, "limit": 1000})
        check_response(resp)
        items = []
        data = resp.get("data") or resp.get("Data") or []
        for raw_item in data:
            item = normalize_attr(raw_item)
            if item["is_dir"]:
                items.append({
                    "id": str(item["id"]),
                    "name": item["name"],
                    "is_dir": True,
                })
        if not items:
            logger.info("浏览目录 pid=%s 返回空: resp=%s", pid, resp)
        return {"items": items, "path": path or "/"}
    except Exception as e:
        logger.error("浏览目录失败 pid=%s: %s", pid, e, exc_info=True)
        raise ServiceError(f"浏览目录失败: {e}")


# ── 同步 ──


def _launch_sync(sync_type: str) -> Dict[str, Any]:
    """
    启动同步任务：读取配置并在后台线程执行，立即返回，避免阻塞事件循环

    同步为长耗时操作，若在请求线程内同步执行会冻结整个管理 UI，
    进度轮询接口也无法响应。改为后台线程执行后，
    前端通过 /api/sync/progress 轮询获取实时进度

    :param sync_type (str): full 全量或 incremental 增量

    :return Dict: 启动结果，status 为 started 或 error
    """
    try:
        client = get_client()
    except ClientNotReadyError as e:
        return {"status": "error", "message": e.message}

    config = config_manager.get()
    p115_cfg = config.get("p115", {})
    from strm_generator import get_strm_generator

    gen = get_strm_generator(client, p115_cfg.get("strm_url_prefix", ""))
    if gen.is_syncing():
        return {"status": "error", "message": "同步正在进行中"}
    gen.set_config(
        rmt_mediaext=p115_cfg.get("rmt_mediaext", ""),
        download_mediaext=p115_cfg.get("download_mediaext", ""),
        auto_download_mediainfo=p115_cfg.get("auto_download_mediainfo", False),
        overwrite_mode=p115_cfg.get("overwrite_mode", "never"),
        cleanup_deleted=p115_cfg.get("cleanup_deleted_strm", False),
        use_rust=p115_cfg.get("use_rust", False),
    )
    sync_fn = gen.full_sync if sync_type == "full" else gen.incremental_sync
    mappings = p115_cfg.get("paths", [])

    def _runner():
        try:
            sync_fn(mappings)
        except Exception as e:
            logger.error("同步线程异常: %s", e, exc_info=True)

    threading.Thread(
        target=_runner, daemon=True, name=f"sync-{sync_type}"
    ).start()
    return {"status": "started", "message": "同步任务已启动"}


@router.get("/sync/progress")
async def get_sync_progress() -> Dict[str, Any]:
    from strm_generator import get_strm_generator
    gen = get_strm_generator(_client, "")
    return gen.get_progress()


@router.post("/sync/start")
async def start_full_sync() -> Dict[str, Any]:
    return _launch_sync("full")


@router.post("/sync/incremental")
async def start_incremental_sync() -> Dict[str, Any]:
    return _launch_sync("incremental")


@router.post("/sync/cancel")
async def cancel_sync() -> Dict[str, Any]:
    from strm_generator import get_strm_generator
    gen = get_strm_generator(_client)
    gen.cancel()
    return {"status": "cancelled"}


@router.get("/sync/history")
async def get_sync_history(limit: int = 20) -> List[Dict]:
    return db.get_sync_history(limit)


@router.post("/sync/history/clear")
async def clear_sync_history() -> Dict[str, Any]:
    db.clear_sync_history()
    return {"success": True}


@router.post("/sync/reset-baseline")
async def reset_sync_baseline() -> Dict[str, Any]:
    db.clear_all_files()
    db.clear_sync_history()
    return {"success": True}


# ── STRM 管理 ──


@router.get("/strm/list")
async def list_strm_files(page: int = 1, page_size: int = 50) -> Dict:
    offset = (page - 1) * page_size
    cursor = db.conn.execute(
        "SELECT * FROM files WHERE status='active' ORDER BY id DESC LIMIT ? OFFSET ?",
        (page_size, offset),
    )
    rows = [dict(r) for r in cursor.fetchall()]
    total = db.count_active_files()
    return {"items": rows, "total": total, "page": page, "page_size": page_size}


@router.get("/strm/count")
async def count_strm_files() -> Dict:
    return {"total": db.count_active_files()}


# ── 签到 ──


@router.get("/checkin/status")
async def checkin_status() -> Dict:
    return checkin_scheduler.get_status()


@router.post("/checkin/run")
async def checkin_manual_exec() -> Dict:
    ok, detail = checkin_scheduler.manual_checkin()
    return {"status": "ok" if ok else "error", "message": detail}


@router.post("/checkin/config")
async def checkin_save_config(data: dict) -> Dict:
    config_manager.update({"checkin": {
        "enabled": bool(data.get("enabled", False)),
        "time_range": str(data.get("time_range", "06:00-09:00")),
    }})
    return {"status": "ok", "message": "签到配置已保存"}


# ── 二维码登录 ──


@router.get("/qrcode")
async def get_qrcode(app: str = "alipaymini") -> Dict:
    client = get_client()
    try:
        result = client.get_qrcode(app)
        if result:
            return result
        raise ServiceError("获取二维码失败")
    except Exception as e:
        raise ServiceError(str(e))


@router.post("/qrcode/check")
async def check_qrcode(payload: dict) -> Dict:
    client = get_client()
    try:
        result = client.check_qrcode(payload)
        if result:
            return result
        return {"status": "pending"}
    except Exception as e:
        return {"status": "error", "message": str(e)}

