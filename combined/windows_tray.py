"""
Windows 系统托盘 + 原生窗口集成模块

为两个独立应用提供统一的 Windows 桌面体验：
- 启动时弹出原生窗口（WebView2）显示管理界面
- 关闭窗口时缩小到系统托盘
- 托盘图标右键菜单：打开窗口 / 退出
"""

import os
import sys
import threading

from logger import logger

_HAS_PYSTRAY = False
_HAS_PYWEBVIEW = False

try:
    import pystray
    from PIL import Image, ImageDraw

    _HAS_PYSTRAY = True
except ImportError:
    pass

try:
    import webview

    _HAS_PYWEBVIEW = True
except ImportError:
    pass


def _create_tray_icon() -> "Image":
    """生成一个简单的托盘图标（蓝色圆形 + 白色箭头）"""
    size = 64
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.ellipse([4, 4, size - 4, size - 4], fill="#1a73e8")
    # 白色箭头
    draw.polygon([(32, 14), (18, 40), (46, 40)], fill="white")
    return img


def _create_text_icon(text: str = "S") -> "Image":
    """生成带文字的托盘图标"""
    size = 64
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.ellipse([4, 4, size - 4, size - 4], fill="#1a73e8")
    # 使用 PIL 默认字体居中绘制文字
    bbox = draw.textbbox((0, 0), text, font=None)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    x = (size - tw) / 2
    y = (size - th) / 2 - 2
    draw.text((x, y), text, fill="white", font=None)
    return img


def run_tray(
    app_name: str = "App",
    admin_url: str = "http://localhost:8100",
    icon_char: str = "S",
    on_exit: callable = None,
):
    """
    以系统托盘模式运行应用

    :param app_name: 应用名称，显示在托盘菜单
    :param admin_url: 管理界面 URL
    :param icon_char: 托盘图标文字
    :param on_exit: 退出时的清理回调
    """
    if not _HAS_PYSTRAY:
        logger.warning("pystray 未安装，回退到控制台模式。pip install pystray Pillow")
        _block_forever()
        return

    if _HAS_PYWEBVIEW:
        _run_with_webview(app_name, admin_url, icon_char, on_exit)
    else:
        _run_with_browser(app_name, admin_url, icon_char, on_exit)


def _block_forever():
    """没有 pystray 时保持进程运行"""
    import time

    while True:
        time.sleep(3600)


def _open_browser(url: str):
    """在默认浏览器中打开 URL"""
    import webbrowser
    webbrowser.open(url)


def _run_with_webview(app_name: str, admin_url: str, icon_char: str, on_exit: callable):
    """使用 pywebview 原生窗口 + 系统托盘，关闭窗口不退出，托盘可重新打开"""
    reopen = threading.Event()
    quit_flag = threading.Event()

    class _Api:
        def selectDirectory(self) -> str:
            try:
                import tkinter
                from tkinter import filedialog
                root = tkinter.Tk()
                root.withdraw()
                path = filedialog.askdirectory(title="选择 STRM 输出目录")
                root.destroy()
                return path or ""
            except Exception:
                return ""

    def show_window(icon, item):
        reopen.set()

    def quit_app(icon, item):
        quit_flag.set()
        reopen.set()
        icon.stop()
        if on_exit:
            on_exit()

    icon_image = _create_text_icon(icon_char)
    menu = (
        pystray.MenuItem(f"打开 {app_name}", show_window, default=True),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("退出", quit_app),
    )

    icon = pystray.Icon(app_name, icon_image, app_name, menu)
    tray_thread = threading.Thread(target=icon.run, daemon=True)
    tray_thread.start()

    api = _Api()

    # 主线程循环：窗口关闭后等待重新打开信号
    while not quit_flag.is_set():
        w = webview.create_window(
            title=app_name,
            url=admin_url,
            width=1100,
            height=750,
            resizable=True,
            min_size=(800, 600),
            js_api=api,
        )
        webview.start(debug=False, private_mode=False)
        if quit_flag.is_set():
            break
        reopen.clear()
        reopen.wait()


def _run_with_browser(app_name: str, admin_url: str, icon_char: str, on_exit: callable):
    """使用浏览器打开管理界面 + 系统托盘"""
    exit_flag = threading.Event()

    def open_admin(icon, item):
        _open_browser(admin_url)

    def quit_app(icon, item):
        icon.stop()
        if on_exit:
            on_exit()
        os._exit(0)

    icon_image = _create_text_icon(icon_char)
    menu = (
        pystray.MenuItem(f"打开 {app_name}", open_admin, default=True),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("退出", quit_app),
    )

    icon = pystray.Icon(app_name, icon_image, app_name, menu)
    icon.run()


def should_use_tray() -> bool:
    """检测是否应该使用托盘模式（Windows + 有 pystray）"""
    return sys.platform == "win32" and _HAS_PYSTRAY
