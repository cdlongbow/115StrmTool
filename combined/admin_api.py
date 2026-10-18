import sys
from typing import Any, Callable, Dict, List, Optional

from datetime import datetime

from fastapi import APIRouter
from fastapi.responses import Response


from exceptions import ServiceError

from logger import LOG_DIR
from pydantic import BaseModel

from config_manager import config_manager, mask_config, COOKIE_MASK
from logger import logger

from app_metadata import APP_VERSION

router = APIRouter(prefix="/admin/api")

# 全局配置模型
class ConfigUpdateRequest(BaseModel):
    emby: Optional[Dict[str, Any]] = None
    p115: Optional[Dict[str, Any]] = None


_restart_emby_callback: Callable = None
_emby_status = {"running": False}
_restart_p115_callback: Callable = None


@router.get("/config")
def get_config() -> Dict[str, Any]:
    return mask_config(config_manager.get())


@router.get("/config/export")
def export_config() -> Response:
    data = config_manager.export_disk_text()
    if data is None:
        raise ServiceError("配置文件不存在，无法导出")
    filename = f"115strmtool-config-{datetime.now():%Y%m%d-%H%M%S}.json"
    return Response(
        content=data,
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


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


@router.get("/status")
def get_combined_status() -> Dict[str, Any]:
    """
    返回各服务运行状态、启用开关与程序版本号，供概览页一次拉取

    :return Dict: 含 emby/p115 状态与 version 的字典
    """
    config = config_manager.get()
    return {
        "emby": {"running": _emby_status["running"], "enabled": config.get("emby", {}).get("enabled", False)},
        "p115": {"running": _p115_status["running"], "enabled": config.get("p115", {}).get("enabled", False)},
        "version": APP_VERSION,
    }


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


if sys.platform == "win32":
    import winreg as _winreg

    _AUTOSTART_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
    _AUTOSTART_NAME = "115StrmTool"


def _autostart_supported() -> bool:
    """
    是否支持开机自启配置

    :return bool: 仅打包后的 Windows 环境支持
    """
    return sys.platform == "win32" and bool(getattr(sys, "frozen", False))


def  _get_autostart() -> bool:
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
    """
    写入或删除 HKCU Run 开机自启项

    :param enable (bool): 写入或删除自启项

    :raises ServiceError: 不支持的环境或注册表操作失败
    """
    if not _autostart_supported():
        raise ServiceError("当前环境不支持开机自启", status_code=501)
    key = None
    try:
        key = _winreg.OpenKey(
            _winreg.HKEY_CURRENT_USER, _AUTOSTART_KEY, 0, _winreg.KEY_SET_VALUE
        )
        if enable:
            _winreg.SetValueEx(
                key, _AUTOSTART_NAME, 0, _winreg.REG_SZ, sys.executable
            )
        else:
            try:
                _winreg.DeleteValue(key, _AUTOSTART_NAME)
            except FileNotFoundError:
                pass
    except OSError as e:
        logger.error("开机自启设置失败: %s", e, exc_info=True)
        raise ServiceError("开机自启设置失败", status_code=501)
    finally:
        if key is not None:
            _winreg.CloseKey(key)


@router.get("/autostart")
def  get_autostart() -> Dict:
    """
    读取开机自启状态与支持性

    :return Dict: 自启状态与支持性标志
    """
    return {"enabled": _get_autostart(), "supported": _autostart_supported()}


class AutostartRequest(BaseModel):
    enabled: bool


@router.post("/autostart")
def  set_autostart(req: AutostartRequest) -> Dict:
    """
    设置开机自启，不支持的环境返回 501

    :param req (AutostartRequest): 启用开关

    :return Dict: 含 supported 的自启状态
    """
    _set_autostart(req.enabled)
    return {"enabled": _get_autostart(), "supported": _autostart_supported()}


class WebdavBackupConfigRequest(BaseModel):
    """WebDAV 备份设置保存请求，password 传掩码串表示保留已存密码"""

    enabled: Optional[bool] = None
    url: Optional[str] = None
    username: Optional[str] = None
    password: Optional[str] = None
    remote_subdir: Optional[str] = None
    keep_count: Optional[int] = None


class RestoreRequest(BaseModel):
    """恢复请求，档案名需通过白名单校验"""

    name: str


@router.get("/backup/webdav/config")
def get_backup_webdav_config() -> Dict[str, Any]:
    """
    读取 WebDAV 备份配置（password 掩码展示）

    :return Dict: masked 形态的 webdav 配置节
    """
    return mask_config(config_manager.get()).get("webdav", {})


@router.post("/backup/webdav/config")
def save_backup_webdav_config(req: WebdavBackupConfigRequest) -> Dict[str, Any]:
    """
    保存 WebDAV 备份配置，密码字段传掩码值时保留已存密码

    :param req (WebdavBackupConfigRequest): 配置字段

    :return Dict: 保存后的脱敏配置
    """
    update = {k: v for k, v in req.model_dump(exclude_none=True).items()}
    if update.get("password") == COOKIE_MASK:
        update.pop("password")
    config_manager.update({"webdav": update})
    masked_view = mask_config(config_manager.get()).get("webdav", {})
    return masked_view


@router.post("/backup/webdav/test")
def test_backup_webdav() -> Dict[str, Any]:
    """连接测试：建目录、写探针、删探针、读列表

    :return Dict: 含 ok 与 message

    :raises ServiceError: 未启用或未配置地址（参数类问题）
    """
    cfg = config_manager.get().get("webdav", {})
    if not cfg.get("enabled"):
        raise ServiceError("远端备份未启用，请先开启并保存配置", 400)
    url = (cfg.get("url") or "").strip()
    if not url:
        raise ServiceError("未配置 WebDAV 根地址", 400)
    from webdav_backup import WebdavClient

    client = WebdavClient(
        url,
        username=cfg.get("username") or "",
        password=cfg.get("password") or "",
    )
    try:
        ok, msg = client.test_connection((cfg.get("remote_subdir") or "").strip("/"))
        return {"ok": ok, "message": msg}
    finally:
        client.close()


@router.get("/backup/status")
def get_backup_status() -> Dict[str, Any]:
    """
    返回备份开关状态与最近一次任务结果摘要，供面板结果卡展示

    :return Dict: 含 enabled 与 last 字段
    """
    from webdav_backup import backup_service

    enabled = bool(config_manager.get().get("webdav", {}).get("enabled"))
    return {"enabled": enabled, "last": backup_service.last_result()}


@router.post("/backup/run")
def run_config_backup() -> Dict[str, Any]:
    """
    执行一次 WebDAV 手动备份

    :return Dict: 备份结果摘要（name/size/pruned）
    """
    from webdav_backup import backup_service

    return backup_service.run_backup()


@router.get("/backup/remote")
def list_remote_backups() -> Dict[str, Any]:
    """
    查看远端备份档案列表

    :return Dict: 含 archives 列表
    """
    from webdav_backup import backup_service

    return {"archives": backup_service.list_remote()}


@router.get("/backup/remote/download")
def download_remote_archive(name: str) -> Response:
    """
    下载单个远端档案为 zip 附件

    :param name (str): query 参数档案名，白名单校验在 service 内完成

    :return Response: 附件响应字节流

    :raises BadRequestError: 档案名非法
    :raises NotFoundError: 档案不存在
    """
    from webdav_backup import backup_service

    archive_name, data = backup_service.download_remote(name)
    return Response(
        content=data,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{archive_name}"'},
    )


@router.post("/backup/restore")
def restore_from_backup(req: RestoreRequest) -> Dict[str, Any]:
    """
    从远端档案恢复配置，成功后触发 p115/emby 服务重建使新配置立即生效

    :param req (RestoreRequest): 档案名请求体，白名单校验在服务层

    :return Dict: 恢复结果摘要（含 guard 快照编号）
    """
    from webdav_backup import backup_service

    result = backup_service.restore_backup(req.name)
    if result.get("ok"):
        if _restart_p115_callback:
            _restart_p115_callback()
        if _restart_emby_callback:
            _restart_emby_callback()
    return result
