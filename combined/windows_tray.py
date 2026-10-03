import threading
from typing import Callable, Optional, Tuple
import os
import sys
from urllib.request import Request, urlopen

from logger import logger

_HAS_PYSTRAY = False
try:
    import pystray
    from PIL import Image, ImageDraw
    _HAS_PYSTRAY = True
except ImportError:
    pass


def _create_icon():
    size = 64
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.ellipse([4, 4, size - 4, size - 4], fill="#1a73e8")
    bbox = draw.textbbox((0, 0), "M", font=None)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    x = (size - tw) / 2
    y = (size - th) / 2 - 2
    draw.text((x, y), "M", fill="white", font=None)
    return img


def _post_api(path: str, port: int = 8100) -> Tuple[bool, str]:
    """
    向本地管理接口发起 POST 请求

    :param path (str): 接口路径
    :param port (int): 管理端口

    :return Tuple: (是否成功, 响应正文或错误描述)
    """
    try:
        req = Request(f"http://127.0.0.1:{port}{path}", method="POST")
        try:
            from config_manager import config_manager

            admin_token = config_manager.get().get("admin_token", "")
            if admin_token:
                req.add_header("X-Admin-Token", admin_token)
        except Exception:
            logger.debug("读取管理令牌失败，按无令牌请求", exc_info=True)
        with urlopen(req, timeout=5) as r:
            return True, r.read().decode("utf-8")
    except Exception as e:
        logger.exception("托盘请求管理接口失败: %s", path)
        return False, "请求失败: " + str(e)


def _open_browser(url: str):
    import webbrowser
    webbrowser.open(url)


def _open_logs_dir():
    from logger import LOG_DIR
    logs_path = LOG_DIR
    if logs_path.exists():
        os.startfile(str(logs_path.resolve()))  # Windows only


def run_tray(
    app_name: str = "App",
    admin_url: str = "http://localhost:8100",
    admin_port: int = 8100,
    on_exit: Optional[Callable[[], None]] = None,
) -> None:
    """
    运行系统托盘，菜单回调在工作线程执行

    :param app_name (str): 托盘显示名
    :param admin_url (str): 管理界面地址
    :param admin_port (int): 管理端口
    :param on_exit (Callable): 退出时的清理回调
    """
    if not _HAS_PYSTRAY:
        raise RuntimeError("pystray 未安装，无法运行托盘")

    def _run_async(fn):
        threading.Thread(target=fn, daemon=True).start()

    def _open_admin(icon, item):
        _run_async(lambda: _open_browser(admin_url))

    def _notify_task(icon, label, path):
        def _work():
            ok, result = _post_api(path, admin_port)
            logger.info("托盘菜单 - %s: %s", label, result)
            title = label if ok else label + " 失败"
            icon.notify(title + "\n" + result[:80], app_name)

        _run_async(_work)

    def _sync(icon, item):
        _notify_task(icon, "全量同步", "/api/sync/start")

    def _incr_sync(icon, item):
        _notify_task(icon, "增量同步", "/api/sync/incremental")

    def _checkin(icon, item):
        _notify_task(icon, "立即签到", "/api/checkin/run")

    def _open_logs(icon, item):
        _run_async(_open_logs_dir)

    def _quit(icon, item):
        icon.stop()
        if on_exit:
            on_exit()

    icon_image = _create_icon()
    menu = (
        pystray.MenuItem("打开管理界面", _open_admin, default=True),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("全量同步", _sync),
        pystray.MenuItem("增量同步", _incr_sync),
        pystray.MenuItem("立即签到", _checkin),
        pystray.MenuItem("打开日志目录", _open_logs),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("退出", _quit),
    )

    icon = pystray.Icon(app_name, icon_image, app_name, menu)
    icon.run()


def should_use_tray() -> bool:
    return sys.platform == "win32" and _HAS_PYSTRAY