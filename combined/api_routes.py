from typing import Any, Dict, List
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

import asyncio

from logger import logger
from config_manager import config_manager
from database import db
from p115_client_wrapper import P115ClientWrapper

router = APIRouter(prefix="/api")

_client: P115ClientWrapper = None


def set_client(client: P115ClientWrapper):
    global _client
    _client = client


def get_client() -> P115ClientWrapper:
    if _client is None:
        raise HTTPException(status_code=503, detail="115 客户端未初始化，请先配置 Cookie")
    if not _client.is_ready():
        raise HTTPException(status_code=503, detail="115 客户端未就绪，请检查 Cookie 配置")
    return _client


# ── 浏览目录 ──


@router.get("/select-directory")
async def select_directory() -> Dict:
    try:
        path = await asyncio.to_thread(_select_directory_sync)
        return {"path": (path or "").replace("\\", "/")}
    except Exception as e:
        return {"path": ""}


def _select_directory_sync() -> str:
    import tkinter
    from tkinter import filedialog
    root = tkinter.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    path = filedialog.askdirectory(title="选择 STRM 输出目录")
    root.destroy()
    return path or ""


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
        from app_ver import get_real_app_ver
        resp = client._client.fs_files({"cid": pid, "limit": 1000, "app_ver": get_real_app_ver()})
        check_response(resp)
        items = []
        data = resp.get("data") or resp.get("Data") or []
        for item in data:
            if "fid" not in item:
                items.append({
                    "id": str(item.get("cid", "")),
                    "name": item.get("n", ""),
                    "is_dir": True,
                })
        if not items:
            logger.info("浏览目录 pid=%s 返回空: resp=%s", pid, resp)
        return {"items": items, "path": path or "/"}
    except Exception as e:
        logger.error("浏览目录失败 pid=%s: %s", pid, e, exc_info=True)
        raise HTTPException(status_code=500, detail=f"浏览目录失败: {e}")


# ── 同步 ──


_sync_in_progress = False


@router.post("/sync/start")
async def start_full_sync() -> Dict[str, Any]:
    global _sync_in_progress
    if _sync_in_progress:
        return {"status": "error", "message": "同步正在进行中"}
    _sync_in_progress = True
    try:
        config = config_manager.get()
        p115_cfg = config.get("p115", {})
        from strm_generator import get_strm_generator
        gen = get_strm_generator(_client, p115_cfg.get("strm_url_prefix", ""))
        gen.set_config(
            rmt_mediaext=p115_cfg.get("rmt_mediaext", ""),
            download_mediaext=p115_cfg.get("download_mediaext", ""),
            auto_download_mediainfo=p115_cfg.get("auto_download_mediainfo", False),
            overwrite_mode=p115_cfg.get("overwrite_mode", "never"),
        )
        result = gen.full_sync(p115_cfg.get("paths", []))
        return {"status": "completed", **result}
    except Exception as e:
        logger.error("同步异常: %s", e, exc_info=True)
        return {"status": "error", "message": str(e)}
    finally:
        _sync_in_progress = False


@router.post("/sync/incremental")
async def start_incremental_sync() -> Dict[str, Any]:
    global _sync_in_progress
    if _sync_in_progress:
        return {"status": "error", "message": "同步正在进行中"}
    _sync_in_progress = True
    try:
        config = config_manager.get()
        p115_cfg = config.get("p115", {})
        from strm_generator import get_strm_generator
        gen = get_strm_generator(_client, p115_cfg.get("strm_url_prefix", ""))
        gen.set_config(
            rmt_mediaext=p115_cfg.get("rmt_mediaext", ""),
            download_mediaext=p115_cfg.get("download_mediaext", ""),
            auto_download_mediainfo=p115_cfg.get("auto_download_mediainfo", False),
            overwrite_mode=p115_cfg.get("overwrite_mode", "never"),
        )
        result = gen.incremental_sync(p115_cfg.get("paths", []))
        return {"status": "completed", **result}
    except Exception as e:
        logger.error("增量同步异常: %s", e, exc_info=True)
        return {"status": "error", "message": str(e)}
    finally:
        _sync_in_progress = False


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


@router.post("/strm/clear")
async def clear_strm_files() -> Dict[str, Any]:
    return {"success": True}  # deprecated: 前端已改为纯 UI 操作


# ── 分享转存 ──


class ShareTransferRequest(BaseModel):
    share_url: str
    target_path: str = "/"


@router.post("/share/transfer")
async def share_transfer(req: ShareTransferRequest) -> Dict:
    client = get_client()
    try:
        db.add_share_transfer(req.share_url, req.target_path)
        logger.info("分享转存请求已记录: %s -> %s", req.share_url, req.target_path)
        return {"status": "added", "share_url": req.share_url, "target_path": req.target_path}
    except Exception as e:
        logger.error("添加分享转存失败: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


# ── 离线下载 ──


class OfflineTaskRequest(BaseModel):
    url: str
    name: str = ""
    save_path: str = "/"


@router.get("/offline/list")
async def get_offline_tasks() -> List[Dict]:
    cursor = db.conn.execute(
        "SELECT * FROM offline_tasks ORDER BY id DESC LIMIT 50"
    )
    return [dict(r) for r in cursor.fetchall()]


@router.post("/offline/add")
async def add_offline_task(req: OfflineTaskRequest) -> Dict:
    try:
        db.add_offline_task(req.url, req.name, req.save_path)
        logger.info("离线任务已添加: %s (%s)", req.name or req.url, req.save_path)
        return {"status": "added", "url": req.url}
    except Exception as e:
        logger.error("添加离线任务失败: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


# ── 签到 ──


from checkin_scheduler import checkin_scheduler


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
        raise HTTPException(status_code=500, detail="获取二维码失败")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


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


# ── 二维码登录 ──
