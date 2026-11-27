import argparse
import os
import signal
import socket
import sys
import threading
import time
from contextlib import asynccontextmanager
from hmac import compare_digest
from pathlib import Path
from typing import AsyncIterator

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse
from uvicorn import Config, Server

from admin_api import (
    set_emby_status,
    set_emby_restart_callback,
    set_p115_client_ref,
    set_p115_restart_callback,
    set_p115_status,
)
from app_metadata import APP_VERSION
from config_manager import config_manager
from logger import logger

EMBY_SERVER = None
EMBY_THREAD = None
P115_REDIRECT_SERVER = None
P115_REDIRECT_THREAD = None
ADMIN_SERVER = None
P115_CLIENT_WRAPPER = None
_shutdown_event = threading.Event()
_service_lock = threading.RLock()
_SERVER_START_TIMEOUT = 10.0
_SERVER_STOP_TIMEOUT = 5.0


def _port_in_use(host: str, port: int) -> bool:
    """
    探测端口是否已被占用，避免 uvicorn 线程内静默绑定失败导致状态误报

    :param host (str): 监听地址，0.0.0.0 按本机回环探测
    :param port (int): 目标端口

    :return bool: 端口已被占用时返回 True
    """
    probe_host = "127.0.0.1" if host in ("0.0.0.0", "", "::") else host
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.5)
            return s.connect_ex((probe_host, port)) == 0
    except OSError:
        return False


def _wait_for_server_started(server, thread, timeout: float = _SERVER_START_TIMEOUT) -> bool:
    """
    等待服务线程完成真实绑定或提前退出

    :param server: uvicorn Server 实例
    :param thread (threading.Thread): 服务线程
    :param timeout (float): 最大等待秒数

    :return bool: Server.started 为真时返回 True
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if getattr(server, "started", False):
            return True
        if not thread.is_alive():
            return False
        time.sleep(0.05)
    return bool(getattr(server, "started", False))


def _stop_server(server, thread, service_name: str) -> bool:
    """
    停止服务线程，超时或异常时保留句柄

    :param server: uvicorn Server 实例
    :param thread (threading.Thread): 服务线程
    :param service_name (str): 服务名称

    :return bool: 确认停止时返回 True
    """
    if server is None and (thread is None or not thread.is_alive()):
        return True
    try:
        if server is not None:
            server.should_exit = True
        if thread is not None and thread.is_alive():
            thread.join(timeout=_SERVER_STOP_TIMEOUT)
            if thread.is_alive():
                logger.warning("%s 服务线程在 %ss 超时内未能退出", service_name, _SERVER_STOP_TIMEOUT)
                return False
    except Exception as exc:
        logger.warning("停止 %s 服务时出现异常: %s", service_name, exc, exc_info=True)
        return False
    return True


def signal_handler(sig, frame):
    logger.info("收到退出信号，正在关闭服务...")
    _shutdown_event.set()
    _stop_emby()
    _stop_p115_redirect()
    try:
        from checkin_scheduler import checkin_scheduler
        checkin_scheduler.stop()
    except Exception as e:
        logger.warning("停止签到调度器时出现异常: %s", e, exc_info=True)
    if ADMIN_SERVER:
        try:
            ADMIN_SERVER.should_exit = True
        except Exception as e:
            logger.warning("停止管理界面服务时出现异常: %s", e, exc_info=True)
    # 给线程最多 5 秒退出，然后强制退出
    for _ in range(50):
        if not (EMBY_THREAD and EMBY_THREAD.is_alive()) and not (P115_REDIRECT_THREAD and P115_REDIRECT_THREAD.is_alive()):
            break
        time.sleep(0.1)
    sys.exit(0)


def _stop_emby() -> bool:
    """
    停止 Emby 代理，停止失败时保留服务句柄

    :return bool: 确认停止时返回 True
    """
    global EMBY_SERVER, EMBY_THREAD
    with _service_lock:
        if not _stop_server(EMBY_SERVER, EMBY_THREAD, "Emby"):
            return False
        EMBY_SERVER = None
        EMBY_THREAD = None
        set_emby_status(False)
        return True


def _teardown_p115_client() -> None:
    """
    停止签到并清空所有 P115 客户端引用，使旧包装器失效
    """
    global P115_CLIENT_WRAPPER
    from checkin_scheduler import checkin_scheduler
    from api_routes import set_client

    checkin_scheduler.stop()
    set_client(None)
    set_p115_client_ref(None)
    wrapper = P115_CLIENT_WRAPPER
    P115_CLIENT_WRAPPER = None
    if wrapper is not None:
        try:
            wrapper.close()
        except Exception as exc:
            logger.warning("关闭 P115 客户端时出现异常: %s", exc, exc_info=True)
        finally:
            try:
                wrapper._client = None
            except Exception as exc:
                logger.warning("失效旧 P115 客户端时出现异常: %s", exc, exc_info=True)


def _stop_p115_redirect() -> bool:
    """
    停止 P115 跳转服务并统一清理客户端，停止失败时保留句柄

    :return bool: 确认停止时返回 True
    """
    global P115_REDIRECT_SERVER, P115_REDIRECT_THREAD
    with _service_lock:
        if not _stop_server(P115_REDIRECT_SERVER, P115_REDIRECT_THREAD, "P115 Redirect"):
            return False
        P115_REDIRECT_SERVER = None
        P115_REDIRECT_THREAD = None
        _teardown_p115_client()
        set_p115_status(False)
        return True


def _shutdown_for_tray_exit():
    """
    托盘退出时的完整关闭流程

    依次停止 Emby 代理、P115 服务、签到调度器和管理界面服务后，
    强制终止进程。打包为单文件 exe 时解释器关闭阶段可能因残留的
    daemon 线程（uvicorn 事件循环、网络库后台线程等）挂起，导致
    进程不退出、exe 文件被自身锁定无法删除，因此最后调用 os._exit

    :return None: 无返回值，进程被强制终止
    """
    logger.info("托盘退出：正在停止所有服务...")
    _stop_emby()
    _stop_p115_redirect()
    try:
        from checkin_scheduler import checkin_scheduler

        checkin_scheduler.stop()
    except Exception as e:
        logger.warning("停止签到调度器时出现异常: %s", e, exc_info=True)
    if ADMIN_SERVER is not None:
        try:
            ADMIN_SERVER.should_exit = True
        except Exception as e:
            logger.warning("停止管理界面服务时出现异常: %s", e, exc_info=True)
    logger.info("所有服务已停止，进程即将退出")
    os._exit(0)


def _start_emby() -> bool:
    """
    启动 Emby 代理并在真实绑定成功后发布运行状态

    :return bool: 启动成功或未启用时返回 True
    """
    global EMBY_SERVER, EMBY_THREAD
    with _service_lock:
        if EMBY_SERVER is not None or (EMBY_THREAD and EMBY_THREAD.is_alive()):
            return True
        config = config_manager.get().get("emby", {})
        if not config.get("enabled") or not config.get("emby_host"):
            logger.warning("Emby 代理未启用或地址未配置，跳过")
            set_emby_status(False)
            return True

        emby_host = config["emby_host"]
        if not emby_host.startswith(("http://", "https://")):
            emby_host = "http://" + emby_host

        from proxy_app import create_app
        from config_manager import config_manager as cm

        pin_rules = cm.parse_pin_rules(config.get("pin_rules", ""))
        app = create_app(
            emby_host=emby_host,
            pin_rules=pin_rules,
            external_player_url=config.get("external_player_url", False),
            external_player_list=config.get("external_player_list", []),
            playback_mode=config.get("playback_mode", "disabled"),
        )
        try:
            if _port_in_use(config.get("proxy_host", "0.0.0.0"), int(config.get("proxy_port", 8097))):
                raise OSError(f"端口 {config.get('proxy_port', 8097)} 已被占用")
            uv_config = Config(
                app=app,
                host=config.get("proxy_host", "0.0.0.0"),
                port=int(config.get("proxy_port", 8097)),
                log_config=None,
            )
            server = Server(uv_config)
            thread = threading.Thread(target=server.run, daemon=True)
            EMBY_SERVER = server
            EMBY_THREAD = thread
            thread.start()
            if not _wait_for_server_started(server, thread):
                _stop_server(server, thread, "Emby")
                EMBY_SERVER = None
                EMBY_THREAD = None
                set_emby_status(False)
                logger.error("Emby 代理启动失败: 服务线程未完成绑定")
                return False
            set_emby_status(True)
            logger.info(
                "Emby 代理已启动: %s:%s -> %s",
                config.get("proxy_host"),
                config.get("proxy_port"),
                emby_host,
            )
            return True
        except Exception as e:
            logger.error("Emby 代理启动失败: %s", e, exc_info=True)
            if EMBY_SERVER is not None:
                _stop_server(EMBY_SERVER, EMBY_THREAD, "Emby")
            EMBY_SERVER = None
            EMBY_THREAD = None
            set_emby_status(False)
            return False


def _restart_emby() -> bool:
    """
    重启 Emby 代理，停止失败时不再启动新实例

    :return bool: 重启成功时返回 True
    """
    with _service_lock:
        logger.info("正在重启 Emby 代理...")
        if not _stop_emby():
            return False
        config_manager.load()
        return _start_emby()


def _start_p115() -> bool:
    """
    启动 P115 跳转服务并在真实绑定成功后发布运行状态

    :return bool: 启动成功、未启用或未配置 Cookie 时返回 True
    """
    global P115_REDIRECT_SERVER, P115_REDIRECT_THREAD, P115_CLIENT_WRAPPER
    with _service_lock:
        if P115_REDIRECT_SERVER is not None or (P115_REDIRECT_THREAD and P115_REDIRECT_THREAD.is_alive()):
            return True
        config = config_manager.get().get("p115", {})
        cookie = str(config.get("cookie") or "").strip()
        if not config.get("enabled") or not cookie:
            logger.warning("P115 STRM 助手未启用或 Cookie 未配置，跳过 302 跳转服务")
            _teardown_p115_client()
            set_p115_status(False)
            return True

        from p115_client_wrapper import P115ClientWrapper
        from redirect_service import RedirectService
        from api_routes import set_client
        from app_ver import apply_app_ver_patch

        apply_app_ver_patch()
        client = P115ClientWrapper(cookie)
        P115_CLIENT_WRAPPER = client
        set_client(client)
        set_p115_client_ref(client)

        from checkin_scheduler import checkin_scheduler
        checkin_scheduler.set_client(client)
        checkin_scheduler.start()

        svc = RedirectService(client)
        redirect_app = svc.create_app()
        try:
            if _port_in_use(config.get("redirect_host", "0.0.0.0"), int(config.get("redirect_port", 3333))):
                raise OSError(f"端口 {config.get('redirect_port', 3333)} 已被占用")
            uv_config = Config(
                app=redirect_app,
                host=config.get("redirect_host", "0.0.0.0"),
                port=int(config.get("redirect_port", 3333)),
                log_config=None,
            )
            server = Server(uv_config)
            thread = threading.Thread(target=server.run, daemon=True)
            P115_REDIRECT_SERVER = server
            P115_REDIRECT_THREAD = thread
            thread.start()
            if not _wait_for_server_started(server, thread):
                _stop_server(server, thread, "P115 Redirect")
                P115_REDIRECT_SERVER = None
                P115_REDIRECT_THREAD = None
                _teardown_p115_client()
                set_p115_status(False)
                logger.error("P115 302 跳转服务启动失败: 服务线程未完成绑定")
                return False
            set_p115_status(True)
            logger.info(
                "P115 302 跳转服务已启动: %s:%s",
                config.get("redirect_host"),
                config.get("redirect_port"),
            )
            return True
        except Exception as e:
            logger.error("P115 302 跳转服务启动失败: %s", e, exc_info=True)
            if P115_REDIRECT_SERVER is not None:
                _stop_server(P115_REDIRECT_SERVER, P115_REDIRECT_THREAD, "P115 Redirect")
            P115_REDIRECT_SERVER = None
            P115_REDIRECT_THREAD = None
            _teardown_p115_client()
            set_p115_status(False)
            return False


def _restart_p115() -> bool:
    """
    重启 P115 服务，停止失败时不再启动新实例

    :return bool: 重启成功时返回 True
    """
    with _service_lock:
        logger.info("正在重启 P115 服务...")
        if not _stop_p115_redirect():
            _teardown_p115_client()
            return False
        config_manager.load()
        return _start_p115()


def create_admin_app() -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        """
        管理应用启动与关闭期间维护就绪状态

        :param app (FastAPI): 当前管理应用

        :return AsyncIterator: 生命周期上下文
        """
        app.state.ready = False
        try:
            app.state.ready = True
            yield
        finally:
            app.state.ready = False

    # 自动接口文档不在令牌中间件保护范围内，直接关闭避免接口结构外泄
    app = FastAPI(
        title="115网盘STRM生成与302工具",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )

    from exceptions import ServiceError

    @app.exception_handler(ServiceError)
    async def service_error_handler(request, exc: ServiceError):
        logger.warning("ServiceError: %s", exc.message)
        return JSONResponse(
            status_code=exc.status_code,
            content={"error": exc.message},
        )

    @app.get("/health/live")
    async def health_live() -> JSONResponse:
        """
        返回进程存活状态

        :return JSONResponse: 仅包含固定状态字段的响应
        """
        return JSONResponse(content={"status": "ok"})

    @app.get("/health/ready")
    async def health_ready() -> JSONResponse:
        """
        返回管理应用初始化状态

        :return JSONResponse: 初始化完成返回 200 未完成返回 503
        """
        if not getattr(app.state, "ready", False):
            return JSONResponse(status_code=503, content={"status": "not_ready"})
        return JSONResponse(content={"status": "ready"})

    @app.middleware("http")
    async def admin_token_gate(request, call_next):
        """
        API 端点访问令牌校验：配置令牌为空时跳过（未初始化/开发模式）

        :param request: 进入的 HTTP 请求
        :param call_next: 下游处理器
        """
        path = request.url.path
        api_paths = ("/api/", "/admin/api/")
        if not (path.startswith(api_paths) or path in ("/api", "/admin/api")):
            return await call_next(request)
        token = config_manager.get().get("admin_token") or ""
        if not token:
            # 令牌未初始化时只允许本机回环访问，避免局域网/公网直接裸奔；
            # 启动流程会自动生成令牌，本机仍能进入管理界面完成初始化
            peer = request.client.host if request.client else ""
            if peer not in ("127.0.0.1", "::1", "localhost"):
                return JSONResponse(
                    status_code=401,
                    content={"error": "管理令牌未初始化，仅本机可访问"},
                )
            return await call_next(request)
        supplied = request.headers.get("x-admin-token") or ""
        if not compare_digest(supplied, token):
            return JSONResponse(
                status_code=401,
                content={"error": "缺少或错误的访问令牌（X-Admin-Token）"},
            )
        return await call_next(request)

    # 挂载 P115 API 路由
    from api_routes import router as p115_router

    app.include_router(p115_router)

    # 挂载管理 API
    from admin_api import router as admin_router

    app.include_router(admin_router)

    if getattr(sys, "frozen", False):
        web_dir = Path(getattr(sys, "_MEIPASS", Path(__file__).parent)) / "web"
    else:
        web_dir = Path(__file__).parent / "web"
    if web_dir.exists():
        @app.get("/")
        @app.get("/admin")
        @app.get("/admin/")
        @app.get("/admin/{path:path}")
        async def admin_spa():
            index_path = web_dir / "index.html"
            if index_path.exists():
                content = index_path.read_text(encoding="utf-8")
                return HTMLResponse(content=content)
            return HTMLResponse(content="<h1>115网盘STRM生成与302工具</h1><p>Web UI 未构建</p>")

    return app


def _run_admin():
    global ADMIN_SERVER
    config = config_manager.get()
    app = create_admin_app()
    admin_config = Config(
        app=app,
        host=config.get("admin_host", "127.0.0.1"),
        port=int(config.get("admin_port", 8100)),
        log_config=None,
    )
    ADMIN_SERVER = Server(admin_config)
    ADMIN_SERVER.run()


def main():
    parser = argparse.ArgumentParser(description="115网盘STRM生成与302工具")
    parser.add_argument("--no-tray", action="store_true", help="以控制台模式运行（不启动托盘）")
    args = parser.parse_args()

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    logger.info("=" * 56)
    logger.info("  115网盘STRM生成与302工具  v%s", APP_VERSION)
    logger.info("=" * 56)

    config = config_manager.get()
    admin_token = config.get("admin_token") or ""
    if not admin_token:
        from secrets import token_urlsafe

        admin_token = token_urlsafe(24)
        if not config_manager.update({"admin_token": admin_token}):
            raise RuntimeError("管理令牌保存失败，请检查数据目录权限或磁盘空间")
    # 日志可通过管理接口读取，完整令牌不得落盘
    logger.info(
        "管理界面访问令牌已就绪（前 4 位 %s...），完整值见 config.json；"
        "托盘「打开管理界面」会自动注入",
        admin_token[:4] if admin_token else "(空)",
    )
    set_emby_restart_callback(_restart_emby)
    set_p115_restart_callback(_restart_p115)

    logger.info("管理界面: http://%s:%s/", config.get("admin_host"), config.get("admin_port"))

    # 启动 Emby 代理
    _start_emby()

    # 启动 P115 服务
    _start_p115()

    from windows_tray import run_tray, should_use_tray

    tray_available = should_use_tray()
    use_tray = tray_available and not args.no_tray
    if not args.no_tray and not tray_available:
        logger.warning("系统托盘不可用，已回退管理服务模式；请确认 Windows 平台已安装 pystray")

    if use_tray:
        admin_thread = threading.Thread(target=_run_admin, daemon=True)
        admin_thread.start()
        run_tray(
            app_name="115网盘STRM生成与302工具",
            admin_url=f"http://127.0.0.1:{config.get('admin_port', 8100)}/?token={admin_token}",
            admin_port=int(config.get("admin_port", 8100)),
            on_exit=_shutdown_for_tray_exit,
            tooltip=f"115网盘STRM生成与302工具 v{APP_VERSION}",
        )
        # 正常退出路径已在 _shutdown_for_tray_exit 中强制终止进程，
        # 走到这里说明托盘异常退出，同样强制退出避免进程残留
        logger.warning("托盘已退出但进程仍在运行，强制退出")
        os._exit(0)
    else:
        _run_admin()


if __name__ == "__main__":
    main()