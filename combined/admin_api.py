import sys
from typing import Any, Callable, Dict, List, Optional

from fastapi import APIRouter

from exceptions import ServiceError

from logger import LOG_DIR
from pydantic import BaseModel

from config_manager import config_manager, mask_config, COOKIE_MASK
from logger import logger

router = APIRouter(prefix="/admin/api")

# 全局配置模型
class ConfigUpdateRequest(BaseModel):
    emby: Optional[Dict[str, Any]] = None
    p115: Optional[Dict[str, Any]] = None

_restart_emby_callback: Callable = None
_emby_status = {"running": False}
_restart_p115_callback: Callable = None


# ── 全局配置 ──


@router.get("/config")
def get_config() -> Dict[str, Any]:
    return mask_config(config_manager.get())


@router.post("/config")
def update_config(req: ConfigUpdateRequest) -> Dict[str, Any]:
    updates = {}
    if req.emby is not None:
        emby_updates = {k: v for k, v in req.emby.items() if v is not None}
        if emby_updates:
            updates["emby"] = emby_updates
    if req.p115 is not None:
        p115_updates = {k: v for k, v in req.p115.items() if v is not None}
        # 前端回传掩码值表示 Cookie 未修改，不覆盖已保存的真实 Cookie
        if p115_updates.get("cookie") == COOKIE_MASK:
            p115_updates.pop("cookie")
        if p115_updates:
            updates["p115"] = p115_updates
    if updates:
        config_manager.update(updates)
        logger.info("配置已更新: %s", mask_config(updates))
    if "cookie" in updates.get("p115", {}) and _restart_p115_callback:
        _restart_p115_callback()
        logger.info("P115 Cookie 已更新，客户端已重建")
    return mask_config(config_manager.get())


def set_emby_restart_callback(cb: Callable):
    global _restart_emby_callback
    _restart_emby_callback = cb


def set_p115_restart_callback(cb: Callable):
    global _restart_p115_callback
    _restart_p115_callback = cb


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
    redirect_mode: Optional[bool] = None


@router.get("/emby/config")
def get_emby_config() -> Dict[str, Any]:
    return config_manager.get().get("emby", {})


@router.post("/emby/config")
def update_emby_config(req: EmbyConfigRequest) -> Dict[str, Any]:
    updates = {k: v for k, v in req.model_dump(exclude_unset=True).items() if v is not None}
    if updates:
        config_manager.update({"emby": updates})
        logger.info("Emby 配置已更新: %s", updates)
    return config_manager.get().get("emby", {})


@router.post("/emby/restart")
def restart_emby() -> Dict:
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
def get_p115_status() -> Dict[str, Any]:
    """
    与 /api/status 共用状态构建器，额外返回 running 服务状态

    :return Dict: 状态快照字段加 running
    """
    from api_routes import build_p115_status

    result = build_p115_status(_p115_client_ref["instance"])
    result["running"] = _p115_status["running"]
    return result


# ── 统一状态 ──


@router.get("/status")
def get_combined_status() -> Dict[str, Any]:
    config = config_manager.get()
    return {
        "emby": {"running": _emby_status["running"], "enabled": config.get("emby", {}).get("enabled", False)},
        "p115": {"running": _p115_status["running"], "enabled": config.get("p115", {}).get("enabled", False)},
    }


# ── 日志 ──


@router.get("/logs")
def get_logs(lines: int = 200) -> Dict:
    log_path = LOG_DIR / "combined.log"
    if not log_path.exists():
        return {"logs": []}
    try:
        content = log_path.read_text(encoding="utf-8", errors="replace")
        log_lines = content.strip().split("\n")
        return {"logs": log_lines[-lines:]}
    except OSError as e:
        raise ServiceError(f"读取日志失败: {e}")


@router.delete("/logs")
def clear_logs() -> Dict:
    """
    清空日志文件
    """
    try:
        # 主日志由 RotatingFileHandler 持有，直接重置流内容避免 fd 偏移产生 NUL 空洞
        from logging.handlers import RotatingFileHandler
        for h in logger.handlers:
            if isinstance(h, RotatingFileHandler):
                h.acquire()
                try:
                    if h.stream and not h.stream.closed:
                        h.stream.seek(0)
                        h.stream.truncate(0)
                finally:
                    h.release()
        # 清理轮转备份（主日志由 RotatingFileHandler 持有，不能 unlink）
        for f in LOG_DIR.glob("combined.log.*"):
            if f.is_file():
                f.unlink()
        logger.info("日志已清空")
        return {"status": "ok", "message": "日志已清空"}
    except OSError as e:
        raise ServiceError(f"清空日志失败: {e}")


# ── 系统自动启动 ──


if sys.platform == "win32":
    import winreg as _winreg

    _AUTOSTART_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
    _AUTOSTART_NAME = "115StrmTool"


def _get_autostart() -> bool:
    if sys.platform != "win32":
        return False
    try:
        key = _winreg.OpenKey(_winreg.HKEY_CURRENT_USER, _AUTOSTART_KEY, 0, _winreg.KEY_READ)
        try:
            _winreg.QueryValueEx(key, _AUTOSTART_NAME)
            return True
        except FileNotFoundError:
            return False
        finally:
            _winreg.CloseKey(key)
    except Exception:
        return False


def _set_autostart(enable: bool):
    if sys.platform != "win32":
        return
    try:
        key = _winreg.OpenKey(_winreg.HKEY_CURRENT_USER, _AUTOSTART_KEY, 0, _winreg.KEY_SET_VALUE)
        if enable:
            _winreg.SetValueEx(key, _AUTOSTART_NAME, 0, _winreg.REG_SZ, sys.executable)
        else:
            try:
                _winreg.DeleteValue(key, _AUTOSTART_NAME)
            except FileNotFoundError:
                pass
        _winreg.CloseKey(key)
    except Exception as e:
        logger.error("设置自动启动失败: %s", e)


@router.get("/autostart")
def get_autostart() -> Dict:
    return {"enabled": _get_autostart()}


class AutostartRequest(BaseModel):
    enabled: bool


@router.post("/autostart")
def set_autostart(req: AutostartRequest) -> Dict:
    _set_autostart(req.enabled)
    return {"enabled": _get_autostart()}