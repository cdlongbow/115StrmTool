import asyncio
import queue
import threading
import time
from typing import Any, Dict, List, Optional

from fastapi import APIRouter

from checkin_scheduler import checkin_scheduler
from config_manager import config_manager, mask_config
from database import db
from exceptions import ClientNotReadyError, ServiceError
from logger import logger
from p115_client_wrapper import P115ClientWrapper

router = APIRouter(prefix="/api")

_client: P115ClientWrapper = None
_client_cookie: Optional[str] = None
_sync_lock = threading.RLock()
_sync_generator = None
_sync_pending = False
_sync_generation = 0
_sync_cancel_requested = False


def set_client(client: P115ClientWrapper):
    global _client, _client_cookie
    with _sync_lock:
        _client = client
        _client_cookie = getattr(client, "_cookie", None) if client is not None else None


def get_client() -> P115ClientWrapper:
    p115_cfg = config_manager.get().get("p115", {})
    configured_cookie = str(p115_cfg.get("cookie") or "").strip()
    if not p115_cfg.get("enabled") or not configured_cookie:
        raise ClientNotReadyError("P115 未启用或 Cookie 为空，请先完成配置")
    if _client is None:
        raise ClientNotReadyError("115 客户端未初始化，请先配置 Cookie")
    if _client_cookie is not None and _client_cookie != configured_cookie:
        raise ClientNotReadyError("115 客户端与当前 Cookie 配置不一致，请重启 P115 服务")
    if not _client.is_ready():
        raise ClientNotReadyError()
    return _client


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
_directory_requests: "queue.Queue[tuple[queue.Queue, threading.Event]]" = queue.Queue()
_directory_worker_lock = threading.Lock()
_directory_worker_thread: Optional[threading.Thread] = None


def _directory_worker() -> None:
    """
    在单一常驻线程中创建、使用和销毁 Tk 解释器

    :return None: 工作线程退出时返回
    """
    while True:
        result_queue, cancel_event = _directory_requests.get()
        try:
            import tkinter
            from tkinter import filedialog

            root = tkinter.Tk()
            root.withdraw()
            root.attributes("-topmost", True)

            def _cancel_if_requested() -> None:
                if cancel_event.is_set():
                    try:
                        root.destroy()
                    except Exception:
                        pass
                    return
                root.after(50, _cancel_if_requested)

            root.after(50, _cancel_if_requested)
            try:
                path = filedialog.askdirectory(
                    title="选择 STRM 输出目录", parent=root
                )
                result_queue.put(path or "")
            finally:
                try:
                    root.destroy()
                except Exception:
                    pass
        except Exception as e:
            logger.warning("目录选择对话框异常: %s", e, exc_info=True)
            result_queue.put("")
        finally:
            _directory_requests.task_done()


def _ensure_directory_worker() -> None:
    """
    按需启动唯一的目录选择工作线程

    :return None: 无返回值
    """
    global _directory_worker_thread
    with _directory_worker_lock:
        if _directory_worker_thread is None or not _directory_worker_thread.is_alive():
            _directory_worker_thread = threading.Thread(
                target=_directory_worker, daemon=True, name="select-directory"
            )
            _directory_worker_thread.start()


def _select_directory_sync() -> str:
    """
    在常驻 Tk 工作线程中打开目录选择对话框，带超时保护

    :return str: 所选目录路径，取消 / 超时 / 异常时返回空串
    """
    result_queue: "queue.Queue[str]" = queue.Queue(maxsize=1)
    cancel_event = threading.Event()
    _ensure_directory_worker()
    _directory_requests.put((result_queue, cancel_event))
    try:
        return result_queue.get(timeout=SELECT_DIRECTORY_TIMEOUT)
    except queue.Empty:
        cancel_event.set()
        logger.warning(
            "目录选择对话框 %ss 内无响应，回退手动输入", SELECT_DIRECTORY_TIMEOUT
        )
        return ""


_STATUS_REMOTE_TTL = 30.0
_status_remote_cache: Dict[str, Any] = {"ts": 0.0, "user_info": None, "storage": None}
_status_remote_lock = threading.Lock()


def build_p115_status(
    client: Optional[P115ClientWrapper], include_remote: bool = False
) -> Dict[str, Any]:
    """
    组装 115 状态快照

    默认只读取本地统计并保留 user/storage 空字段，避免概览页常规刷新触达
    115 远程接口；只有 include_remote 为 true 时才拉取用户/存储信息，
    远程结果进程内缓存 30 秒

    :param client (P115ClientWrapper): 115 客户端实例，可能为 None
    :param include_remote (bool): 是否请求 115 用户/存储远程接口

    :return Dict: 含 client_ready/stats/user_info/storage 的字典
    """
    client_ready = (
        client is not None and hasattr(client, "is_ready") and client.is_ready()
    )
    stats: Dict[str, Any] = {}
    user_info: Any = None
    storage: Any = None
    if client_ready:
        try:
            stats = db.get_stats()
        except Exception as e:
            logger.warning("获取 P115 统计失败: %s", e, exc_info=True)
        if include_remote:
            now = time.monotonic()
            with _status_remote_lock:
                if now - _status_remote_cache["ts"] < _STATUS_REMOTE_TTL:
                    user_info = _status_remote_cache["user_info"]
                    storage = _status_remote_cache["storage"]
                else:
                    try:
                        fetched_user_info = client.get_user_info()
                        fetched_storage = client.get_storage_info()
                        user_info = fetched_user_info
                        storage = fetched_storage
                        _status_remote_cache.update(
                            {"ts": now, "user_info": user_info, "storage": storage}
                        )
                    except Exception as e:
                        logger.warning(
                            "获取 115 用户/存储信息失败: %s", e, exc_info=True
                        )
    return {
        "client_ready": client_ready,
        "stats": stats,
        "user_info": user_info,
        "storage": storage,
    }


@router.get("/status")
def get_status(include_remote: bool = False) -> Dict[str, Any]:
    """
    返回 115 状态快照与脱敏配置

    默认只读取本地统计，远程用户/存储字段留空；需要查看 115 用户信息或
    网盘容量时传 include_remote=true

    :param include_remote (bool): 是否请求 115 用户/存储远程接口

    :return Dict: 含 client_ready/stats/user_info/storage/config 的状态字段
    """
    config = config_manager.get()
    result = build_p115_status(_client, include_remote=include_remote)
    result["config"] = mask_config(config)
    return result


@router.get("/browse")
def browse_directory(pid: str = "0", path: str = ""):
    client = get_client()
    try:
        from p115client.tool.attr import normalize_attr

        items = []
        page_size = 1000
        offset = 0
        max_pages = 20
        for _ in range(max_pages):
            resp = client.fs_files_app({
                "cid": int(pid) if str(pid).isdigit() else 0,
                "limit": page_size,
                "offset": offset,
                "show_dir": 1,
            })
            if not resp:
                break
            from p115client import check_response

            check_response(resp)
            data = resp.get("data") or resp.get("Data") or []
            for raw_item in data:
                item = normalize_attr(raw_item)
                if item["is_dir"]:
                    items.append({
                        "id": str(item["id"]),
                        "name": item["name"],
                        "is_dir": True,
                    })
            if len(data) < page_size:
                break
            offset += len(data)
        if not items:
            logger.info("浏览目录 pid=%s 返回空", pid)
        return {"items": items, "path": path or "/"}
    except Exception as e:
        logger.error("浏览目录失败 pid=%s: %s", pid, e, exc_info=True)
        raise ServiceError(f"浏览目录失败: {e}")


def _set_sync_progress(generator, phase: str, message: str) -> None:
    """
    在生成器支持时写入同步终态

    :param generator: STRM 生成器
    :param phase (str): 进度阶段
    :param message (str): 进度说明
    """
    setter = getattr(generator, "_set_progress", None)
    if callable(setter):
        setter(phase, message=message)


def _launch_sync(sync_type: str) -> Dict[str, Any]:
    """
    启动同步任务：读取配置并在后台线程执行，立即返回，避免阻塞事件循环

    同步为长耗时操作，若在请求线程内同步执行会冻结整个管理 UI，
    进度轮询接口也无法响应。改为后台线程执行后，
    前端通过 /api/sync/progress 轮询获取实时进度

    :param sync_type (str): full 全量或 incremental 增量

    :return Dict: 启动结果，status 为 started 或 error
    """
    global _sync_generator, _sync_pending, _sync_generation, _sync_cancel_requested
    try:
        client = get_client()
    except ClientNotReadyError as e:
        return {"status": "error", "message": e.message}

    config = config_manager.get()
    p115_cfg = config.get("p115", {})
    if not (p115_cfg.get("strm_url_prefix") or "").strip():
        return {"status": "error", "message": "STRM 跳转地址未配置，请先在 P115 配置中填写服务地址"}
    from strm_generator import get_strm_generator

    with _sync_lock:
        if _sync_pending:
            return {"status": "error", "message": "同步任务正在启动"}
        if _sync_generator is not None and _sync_generator.is_syncing():
            return {"status": "error", "message": "同步正在进行中"}
        gen = get_strm_generator(client, p115_cfg.get("strm_url_prefix", ""))
        gen.set_config(
            rmt_mediaext=p115_cfg.get("rmt_mediaext", ""),
            download_mediaext=p115_cfg.get("download_mediaext", ""),
            auto_download_mediainfo=p115_cfg.get("auto_download_mediainfo", False),
            overwrite_mode=p115_cfg.get("overwrite_mode", "never"),
            cleanup_deleted=p115_cfg.get("cleanup_deleted_strm", False),
            use_rust=False,
            fast_scan=p115_cfg.get("fast_scan", True),
        )
        sync_fn = gen.full_sync if sync_type == "full" else gen.incremental_sync
        mappings = p115_cfg.get("paths", [])
        _sync_generator = gen
        _sync_pending = True
        _sync_cancel_requested = False
        _sync_generation += 1
        generation = _sync_generation

        def _runner():
            global _sync_pending, _sync_cancel_requested
            reset_cancel = getattr(gen, "reset_cancel", None)
            had_instance_reset = "reset_cancel" in getattr(gen, "__dict__", {})
            if reset_cancel is not None:
                def _guarded_reset_cancel():
                    reset_cancel()
                    with _sync_lock:
                        if generation == _sync_generation and _sync_cancel_requested:
                            gen.cancel()

                gen.reset_cancel = _guarded_reset_cancel
            try:
                with _sync_lock:
                    if generation != _sync_generation or _sync_cancel_requested:
                        return
                sync_fn(mappings)
            except Exception as e:
                logger.error("同步线程异常: %s", e, exc_info=True)
            finally:
                if reset_cancel is not None:
                    try:
                        if had_instance_reset:
                            gen.reset_cancel = reset_cancel
                        else:
                            delattr(gen, "reset_cancel")
                    except Exception:
                        logger.warning("恢复同步取消方法失败", exc_info=True)
                with _sync_lock:
                    if generation == _sync_generation:
                        if _sync_cancel_requested:
                            progress = gen.get_progress()
                            if progress.get("phase") == "cancelling":
                                _set_sync_progress(gen, "cancelled", "同步已取消")
                        _sync_pending = False
                        _sync_cancel_requested = False

        try:
            threading.Thread(
                target=_runner, daemon=True, name=f"sync-{sync_type}"
            ).start()
        except Exception as exc:
            _sync_pending = False
            _sync_cancel_requested = False
            logger.error("同步线程启动失败: %s", exc, exc_info=True)
            return {"status": "error", "message": "同步任务启动失败"}
    return {"status": "started", "message": "同步任务已启动"}


@router.get("/sync/progress")
def get_sync_progress() -> Dict[str, Any]:
    with _sync_lock:
        gen = _sync_generator
    if gen is None:
        return {"phase": "idle", "current": 0, "total": 0, "message": ""}
    return gen.get_progress()


@router.post("/sync/start")
def start_full_sync() -> Dict[str, Any]:
    return _launch_sync("full")


@router.post("/sync/incremental")
def start_incremental_sync() -> Dict[str, Any]:
    return _launch_sync("incremental")


@router.post("/sync/cancel")
def cancel_sync() -> Dict[str, Any]:
    global _sync_cancel_requested
    with _sync_lock:
        gen = _sync_generator
        active = _sync_pending or (gen is not None and gen.is_syncing())
        if not active:
            return {"status": "idle", "message": "当前没有同步任务"}
        _sync_cancel_requested = True
        if gen is not None:
            gen.cancel()
    return {"status": "cancelled"}


def _ensure_sync_inactive() -> None:
    """
    确认当前没有启动中或运行中的同步任务

    :raises ServiceError: 同步任务活跃时返回 409
    """
    with _sync_lock:
        if _sync_pending or (_sync_generator is not None and _sync_generator.is_syncing()):
            raise ServiceError("同步任务正在进行中，请等待完成后再执行", 409)


@router.get("/sync/history")
def get_sync_history(limit: int = 20) -> List[Dict]:
    return db.get_sync_history(limit)


@router.post("/sync/history/clear")
def clear_sync_history() -> Dict[str, Any]:
    with _sync_lock:
        _ensure_sync_inactive()
        db.clear_sync_history()
    return {"success": True}


@router.post("/sync/reset-baseline")
def reset_sync_baseline() -> Dict[str, Any]:
    with _sync_lock:
        _ensure_sync_inactive()
        db.clear_all_files()
        db.clear_sync_history()
    return {"success": True}


@router.get("/strm/list")
def list_strm_files(page: int = 1, page_size: int = 50) -> Dict:
    page = max(1, page)
    page_size = min(500, max(1, page_size))
    offset = (page - 1) * page_size
    cursor = db.conn.execute(
        "SELECT * FROM files WHERE status='active' ORDER BY id DESC LIMIT ? OFFSET ?",
        (page_size, offset),
    )
    rows = [dict(r) for r in cursor.fetchall()]
    total = db.count_active_files()
    return {"items": rows, "total": total, "page": page, "page_size": page_size}


@router.get("/strm/count")
def count_strm_files() -> Dict:
    return {"total": db.count_active_files()}


@router.get("/checkin/status")
def checkin_status() -> Dict:
    return checkin_scheduler.get_status()


@router.post("/checkin/run")
def checkin_manual_exec() -> Dict:
    ok, detail = checkin_scheduler.manual_checkin()
    return {"status": "ok" if ok else "error", "message": detail}


@router.post("/checkin/config")
def checkin_save_config(data: dict) -> Dict:
    updates = {"checkin": {
        "enabled": bool(data.get("enabled", False)),
        "time_range": str(data.get("time_range", "06:00-09:00")),
    }}
    if not config_manager.update(updates):
        raise ServiceError("配置保存失败，请检查数据目录权限或磁盘空间")
    return {"status": "ok", "message": "签到配置已保存"}


QRCODE_APPS = frozenset({"alipaymini", "wechatmini"})


@router.get("/qrcode")
def get_qrcode(app: str = "alipaymini") -> Dict:
    app = app if app in QRCODE_APPS else "alipaymini"
    client = get_client()
    try:
        result = client.get_qrcode(app)
        if result:
            return result
        raise ServiceError("获取二维码失败")
    except Exception as e:
        raise ServiceError(str(e))


@router.post("/qrcode/check")
def check_qrcode(payload: dict) -> Dict:
    client = get_client()
    try:
        result = client.check_qrcode(payload)
        if result:
            if result.get("status") == "success":
                saved_cookie = config_manager.get().get("p115", {}).get("cookie") or ""
                if result.get("cookie") and saved_cookie != result.get("cookie"):
                    return {"status": "error", "message": "登录成功但配置保存失败，请重试"}
            return result
        return {"status": "pending"}
    except Exception as e:
        return {"status": "error", "message": str(e)}

