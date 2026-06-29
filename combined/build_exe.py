import subprocess
import sys


def main():
    py_files = [
        "admin_api.py", "api_routes.py", "checkin_scheduler.py",
        "config_manager.py", "database.py", "external_players.py",
        "logger.py", "p115_client_wrapper.py", "proxy_app.py",
        "redirect_service.py", "strm_generator.py", "windows_tray.py",
    ]
    args = [
        "pyinstaller",
        "--name", "115网盘STRM生成与302工具",
        "--onefile",
        "--add-data", "web:web",
    ]
    for f in py_files:
        args.extend(["--add-data", f"{f}:."])
    hidden = [
        "uvicorn.logging", "uvicorn.loops.auto", "uvicorn.protocols.http.auto",
        "httpx", "websockets", "p115client", "p115cipher", "pystray", "PIL",
    ]
    for h in hidden:
        args.extend(["--hidden-import", h])
    args.extend([
        "--collect-all", "fastapi",
        "--collect-all", "starlette",
        "--noconsole",
        "--noconfirm",
        "main.py",
    ])
    print("Building ...")
    result = subprocess.run(args, capture_output=False)
    if result.returncode == 0:
        print("Build successful!")
    else:
        print("Build failed!")
        sys.exit(1)


if __name__ == "__main__":
    main()