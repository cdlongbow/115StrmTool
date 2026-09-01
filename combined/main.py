import argparse
import os
import signal
import socket
import sys
import threading
import time
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from uvicorn import Config, Server

from config_manager import config_manager
from logger import logger
from admin_api import (
    set_emby_status,
    set_emby_restart_callback,
    set_p115_client_ref,
    set_p115_restart_callback,
    set_p115_status,
)

EMBY_SERVER = None
EMBY_THREAD = None
P115_REDIRECT_SERVER = None
P115_REDIRECT_THREAD = None
ADMIN_SERVER = None
P115_CLIENT_WRAPPER = None
_shutdown_event = threading.Event()


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


def signal_handler(sig, frame):
    logger.info("收到退出信号，正在关闭服务...")
    _shutdown_event.set()
    _stop_emby()
    _stop_p115_redirect()
    if ADMIN_SERVER:
        ADMIN_SERVER.should_exit = True
    # 给线程最多 3 秒退出，然后强制退出
    for _ in range(30):
        if not (EMBY_THREAD and EMBY_THREAD.is_alive()) and not (P115_REDIRECT_THREAD and P115_REDIRECT_THREAD.is_alive()):
            break
        time.sleep(0.1)
    sys.exit(0)


def _stop_emby():
    global EMBY_SERVER, EMBY_THREAD
    if EMBY_SERVER is not None:
        try:
            EMBY_SERVER.should_exit = True
            if EMBY_THREAD and EMBY_THREAD.is_alive():
                EMBY_THREAD.join(timeout=5.0)
                if EMBY_THREAD.is_alive():
                    logger.warning("Emby 服务线程在 5s 超时内未能退出")
        except Exception as e:
            logger.warning("停止 Emby 服务时出现异常: %s", e, exc_info=True)
        EMBY_SERVER = None
        EMBY_THREAD = None
        set_emby_status(False)


def _stop_p115_redirect():
    global P115_REDIRECT_SERVER, P115_REDIRECT_THREAD, P115_CLIENT_WRAPPER
    if P115_REDIRECT_SERVER is not None:
        try:
            P115_REDIRECT_SERVER.should_exit = True
            if P115_REDIRECT_THREAD and P115_REDIRECT_THREAD.is_alive():
                P115_REDIRECT_THREAD.join(timeout=5.0)
                if P115_REDIRECT_THREAD.is_alive():
                    logger.warning("P115 Redirect 服务线程在 5s 超时内未能退出")
        except Exception as e:
            logger.warning("停止 P115 跳转服务时出现异常: %s", e, exc_info=True)
        P115_REDIRECT_SERVER = None
        P115_REDIRECT_THREAD = None
        set_p115_status(False)
    # 关闭并释放旧客户端，避免热重载 Cookie 时连接池泄漏
    if P115_CLIENT_WRAPPER is not None:
        try:
            P115_CLIENT_WRAPPER.close()
        except Exception as e:
            logger.warning("关闭 P115 客户端时出现异常: %s", e, exc_info=True)
        P115_CLIENT_WRAPPER = None


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


# ── Emby 代理 ──


def _start_emby():
    global EMBY_SERVER, EMBY_THREAD
    config = config_manager.get().get("emby", {})
    if not config.get("enabled") or not config.get("emby_host"):
        logger.warning("Emby 代理未启用或地址未配置，跳过")
        return

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
        redirect_mode=config.get("redirect_mode", False),
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
        EMBY_SERVER = Server(uv_config)
        EMBY_THREAD = threading.Thread(target=EMBY_SERVER.run, daemon=True)
        EMBY_THREAD.start()
        set_emby_status(True)
        logger.info("Emby 代理已启动: %s:%s -> %s", config.get("proxy_host"), config.get("proxy_port"), emby_host)
    except Exception as e:
        logger.error("Emby 代理启动失败: %s", e, exc_info=True)
        set_emby_status(False)


def _restart_emby():
    logger.info("正在重启 Emby 代理...")
    _stop_emby()
    config_manager.load()
    _start_emby()


# ── P115 STRM ──


def _start_p115():
    global P115_REDIRECT_SERVER, P115_REDIRECT_THREAD, P115_CLIENT_WRAPPER
    config = config_manager.get().get("p115", {})
    if not config.get("enabled"):
        logger.warning("P115 STRM 助手未启用，跳过")
        return

    cookie = config.get("cookie", "")
    if not cookie:
        logger.warning("P115 Cookie 未配置，跳过 302 跳转服务")
        return

    from p115_client_wrapper import P115ClientWrapper
    from redirect_service import RedirectService
    from api_routes import set_client
    from app_ver import apply_app_ver_patch

    apply_app_ver_patch()
    client = P115ClientWrapper(cookie)
    P115_CLIENT_WRAPPER = client
    set_client(client)
    set_p115_client_ref(client)

    # 启动签到调度器
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
        P115_REDIRECT_SERVER = Server(uv_config)
        P115_REDIRECT_THREAD = threading.Thread(target=P115_REDIRECT_SERVER.run, daemon=True)
        P115_REDIRECT_THREAD.start()
        set_p115_status(True)
        logger.info("P115 302 跳转服务已启动: %s:%s", config.get("redirect_host"), config.get("redirect_port"))
    except Exception as e:
        logger.error("P115 302 跳转服务启动失败: %s", e, exc_info=True)
        set_p115_status(False)


def _restart_p115():
    logger.info("正在重启 P115 服务...")
    _stop_p115_redirect()
    config_manager.load()
    _start_p115()


# ── 管理服务 ──


def create_admin_app() -> FastAPI:
    app = FastAPI(title="115网盘STRM生成与302工具")

    from fastapi.responses import JSONResponse
    from exceptions import ServiceError

    @app.exception_handler(ServiceError)
    async def service_error_handler(request, exc: ServiceError):
        logger.warning("ServiceError: %s", exc.message)
        return JSONResponse(
            status_code=exc.status_code,
            content={"error": exc.message},
        )

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
        host=config.get("admin_host", "0.0.0.0"),
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
    logger.info("  115网盘STRM生成与302工具")
    logger.info("=" * 56)

    config = config_manager.get()
    set_emby_restart_callback(_restart_emby)
    set_p115_restart_callback(_restart_p115)

    logger.info("管理界面: http://%s:%s/", config.get("admin_host"), config.get("admin_port"))

    # 启动 Emby 代理
    _start_emby()

    # 启动 P115 服务
    _start_p115()

    from windows_tray import run_tray, should_use_tray

    use_tray = should_use_tray() and not args.no_tray

    if use_tray:
        admin_thread = threading.Thread(target=_run_admin, daemon=True)
        admin_thread.start()
        run_tray(
            app_name="115网盘STRM生成与302工具",
            admin_url=f"http://127.0.0.1:{config.get('admin_port', 8100)}/",
            admin_port=int(config.get("admin_port", 8100)),
            on_exit=_shutdown_for_tray_exit,
        )
        # 正常退出路径已在 _shutdown_for_tray_exit 中强制终止进程，
        # 走到这里说明托盘异常退出，同样强制退出避免进程残留
        logger.warning("托盘已退出但进程仍在运行，强制退出")
        os._exit(0)
    else:
        _run_admin()


if __name__ == "__main__":
    main()