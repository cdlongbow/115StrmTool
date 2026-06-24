from typing import Any, Callable, Dict, List, Optional
from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from config_manager import config_manager
from logger import logger

router = APIRouter(prefix="/admin/api")

# 全局配置模型
class ConfigUpdateRequest(BaseModel):
    emby: Optional[Dict[str, Any]] = None
    p115: Optional[Dict[str, Any]] = None

_restart_emby_callback: Callable = None
_emby_status = {"running": False}


# ── 全局配置 ──


@router.get("/config")
async def get_config() -> Dict[str, Any]:
    return config_manager.get()


@router.post("/config")
async def update_config(req: ConfigUpdateRequest) -> Dict[str, Any]:
    updates = {}
    if req.emby is not None:
        updates["emby"] = {k: v for k, v in req.emby.items() if v is not None}
    if req.p115 is not None:
        updates["p115"] = {k: v for k, v in req.p115.items() if v is not None}
    if updates:
        config_manager.update(updates)
        logger.info("配置已更新: %s", updates)
    return config_manager.get()


def set_emby_restart_callback(cb: Callable):
    global _restart_emby_callback
    _restart_emby_callback = cb


def set_emby_status(running: bool):
    _emby_status["running"] = running


# ── Emby 配置 ──


class EmbyConfigRequest(BaseModel):
    enabled: Optional[bool] = None
    emby_host: Optional[str] = None
    proxy_host: Optional[str] = None
    proxy_port: Optional[int] = None
    pin_rules: Optional[str] = None
    external_player_url: Optional[bool] = None
    external_player_list: Optional[List[str]] = None


@router.get("/emby/config")
async def get_emby_config() -> Dict[str, Any]:
    return config_manager.get().get("emby", {})


@router.post("/emby/config")
async def update_emby_config(req: EmbyConfigRequest) -> Dict[str, Any]:
    updates = {k: v for k, v in req.dict(exclude_unset=True).items() if v is not None}
    if updates:
        config_manager.update({"emby": updates})
        logger.info("Emby 配置已更新: %s", updates)
    return config_manager.get().get("emby", {})


@router.post("/emby/restart")
async def restart_emby() -> Dict:
    if _restart_emby_callback:
        _restart_emby_callback()
        return {"status": "ok", "message": "Emby 代理已重启"}
    return {"status": "error", "message": "重启回调未注册"}


# ── P115 状态 ──

_p115_client_ref = {"instance": None}
_p115_status = {"running": False}


def set_p115_client_ref(client):
    _p115_client_ref["instance"] = client


def set_p115_status(running: bool):
    _p115_status["running"] = running


@router.get("/p115/status")
async def get_p115_status() -> Dict[str, Any]:
    client = _p115_client_ref["instance"]
    client_ready = client is not None and hasattr(client, "is_ready") and client.is_ready()
    stats = {}
    user_info = None
    storage = None
    if client_ready:
        try:
            from database import db
            stats = db.get_stats()
            user_info = client.get_user_info()
            storage = client.get_storage_info()
        except Exception:
            pass
    return {
        "running": _p115_status["running"],
        "client_ready": client_ready,
        "stats": stats,
        "user_info": user_info,
        "storage": storage,
    }


# ── 统一状态 ──


@router.get("/status")
async def get_combined_status() -> Dict[str, Any]:
    config = config_manager.get()
    return {
        "emby": {"running": _emby_status["running"], "enabled": config.get("emby", {}).get("enabled", False)},
        "p115": {"running": _p115_status["running"], "enabled": config.get("p115", {}).get("enabled", False)},
    }


# ── 日志 ──


@router.get("/logs")
async def get_logs(lines: int = 200) -> Dict:
    log_path = Path("logs/combined.log")
    if not log_path.exists():
        return {"logs": []}
    try:
        content = log_path.read_text(encoding="utf-8", errors="replace")
        log_lines = content.strip().split("\n")
        return {"logs": log_lines[-lines:]}
    except OSError as e:
        raise HTTPException(status_code=500, detail=f"读取日志失败: {e}")