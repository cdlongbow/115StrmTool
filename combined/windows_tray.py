import os
import subprocess
import sys
import threading
from pathlib import Path
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
    from PIL import Image, ImageDraw
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.ellipse([4, 4, size - 4, size - 4], fill="#1a73e8")
    bbox = draw.textbbox((0, 0), "M", font=None)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    x = (size - tw) / 2
    y = (size - th) / 2 - 2
    draw.text((x, y), "M", fill="white", font=None)
    return img


def _post_api(path: str) -> str:
    try:
        req = Request(f"http://127.0.0.1:8100{path}", method="POST")
        with urlopen(req, timeout=5) as r:
            return r.read().decode("utf-8")
    except Exception as e:
        return str(e)


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
    icon_char: str = "M",
    on_exit: callable = None,
):
    if not _HAS_PYSTRAY:
        logger.warning("pystray 未安装，运行在控制台模式。pip install pystray Pillow")
        import time
        while True:
            time.sleep(3600)
        return

    def _open_admin(icon, item):
        _open_browser(admin_url)

    def _sync(icon, item):
        result = _post_api("/api/sync/start")
        logger.info("托盘菜单 - 全量同步: %s", result)
        icon.notify("全量同步结果\n" + result[:80], app_name)

    def _incr_sync(icon, item):
        result = _post_api("/api/sync/incremental")
        logger.info("托盘菜单 - 增量同步: %s", result)
        icon.notify("增量同步结果\n" + result[:80], app_name)

    def _checkin(icon, item):
        result = _post_api("/api/checkin/run")
        logger.info("托盘菜单 - 立即签到: %s", result)
        icon.notify("签到结果\n" + result[:80], app_name)

    def _open_logs(icon, item):
        _open_logs_dir()

    def _quit(icon, item):
        icon.stop()
        if on_exit:
            on_exit()
        os._exit(0)

    icon_image = _create_icon()
    menu = (
        pystray.MenuItem(f"打开管理界面", _open_admin, default=True),
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