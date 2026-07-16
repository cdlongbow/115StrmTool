import argparse
import signal
import sys
import threading
from pathlib import Path

import collections.abc as _cabc
if not hasattr(_cabc, "_check_methods"):
    def _check_methods(C, *methods):
        mro = C.__mro__
        for method in methods:
            for B in mro:
                if method in B.__dict__:
                    if B.__dict__[method] is None:
                        return NotImplemented
                    break
            else:
                return NotImplemented
        return True
    _cabc._check_methods = _check_methods

from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from uvicorn import Config, Server

from config_manager import config_manager
from logger import logger
from admin_api import (
    set_emby_status,
    set_emby_restart_callback,
    set_p115_client_ref,
    set_p115_status,
)

EMBY_SERVER = None
EMBY_THREAD = None
P115_REDIRECT_SERVER = None
P115_REDIRECT_THREAD = None
ADMIN_SERVER = None


def signal_handler(sig, frame):
    logger.info("收到退出信号，正在关闭服务...")
    _stop_emby()
    _stop_p115_redirect()
    if ADMIN_SERVER:
        ADMIN_SERVER.should_exit = True
    sys.exit(0)


def _stop_emby():
    global EMBY_SERVER, EMBY_THREAD
    if EMBY_SERVER is not None:
        try:
            EMBY_SERVER.should_exit = True
            if EMBY_THREAD and EMBY_THREAD.is_alive():
                EMBY_THREAD.join(timeout=5.0)
        except Exception:
            pass
        EMBY_SERVER = None
        EMBY_THREAD = None
        set_emby_status(False)


def _stop_p115_redirect():
    global P115_REDIRECT_SERVER, P115_REDIRECT_THREAD
    if P115_REDIRECT_SERVER is not None:
        try:
            P115_REDIRECT_SERVER.should_exit = True
            if P115_REDIRECT_THREAD and P115_REDIRECT_THREAD.is_alive():
                P115_REDIRECT_THREAD.join(timeout=5.0)
        except Exception:
            pass
        P115_REDIRECT_SERVER = None
        P115_REDIRECT_THREAD = None
        set_p115_status(False)


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
    )
    try:
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
    global P115_REDIRECT_SERVER, P115_REDIRECT_THREAD
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
    from api_routes import router as p115_router, set_client
    from app_ver import apply_app_ver_patch

    apply_app_ver_patch()
    client = P115ClientWrapper(cookie)
    set_client(client)
    set_p115_client_ref(client)

    # 启动签到调度器
    from checkin_scheduler import checkin_scheduler
    checkin_scheduler.set_client(client)
    checkin_scheduler.start()

    svc = RedirectService(client)
    redirect_app = svc.create_app()
    try:
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


# ── 管理服务 ──


def create_admin_app() -> FastAPI:
    app = FastAPI(title="115网盘STRM生成与302工具")

    # 挂载 P115 API 路由
    from api_routes import router as p115_router

    app.include_router(p115_router)

    # 挂载管理 API
    from admin_api import router as admin_router

    app.include_router(admin_router)

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

    logger.info("管理界面: http://%s:%s/", config.get("admin_host"), config.get("admin_port"))

    # 启动 Emby 代理
    _start_emby()

    # 启动 P115 服务
    _start_p115()

    use_tray = not args.no_tray if sys.platform == "win32" else False

    if use_tray:
        admin_thread = threading.Thread(target=_run_admin, daemon=True)
        admin_thread.start()
        from windows_tray import run_tray
        run_tray(
            app_name="115网盘STRM生成与302工具",
            admin_url=f"http://127.0.0.1:{config.get('admin_port', 8100)}/",
            icon_char="M",
            on_exit=lambda: (_stop_emby(), _stop_p115_redirect()),
        )
    else:
        _run_admin()


if __name__ == "__main__":
    main()