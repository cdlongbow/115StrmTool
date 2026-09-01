import subprocess
import sys


def main():
    hidden = [
        "admin_api",
        "api_routes",
        "app_ver",
        "checkin_scheduler",
        "config_manager",
        "database",
        "exceptions",
        "external_players",
        "logger",
        "p115_client_wrapper",
        "proxy_app",
        "redirect_service",
        "strm_generator",
        "utils",
        "windows_tray",
        "httpx",
        "websockets",
        "p115client",
        "p115cipher",
        "p115pickcode",
        "pystray",
        "PIL",
        "qrcode",
        "full_strm_sync",
        "tkinter",
        "tkinter.filedialog",
        "urllib3_future",
    ]
    args = [
        "pyinstaller",
        "--name", "115网盘STRM生成与302工具",
        "--onefile",
        "--add-data", "web:web",
        "--collect-all", "fastapi",
        "--collect-all", "starlette",
        "--collect-all", "uvicorn",
        "--collect-submodules", "p115client",
        "--noconsole",
        "--noconfirm",
    ]
    for h in hidden:
        args.extend(["--hidden-import", h])
    args.append("main.py")
    print("Building ...")
    result = subprocess.run(args, capture_output=False)
    if result.returncode == 0:
        print("Build successful!")
    else:
        print("Build failed!")
        sys.exit(1)


if __name__ == "__main__":
    main()
