import subprocess
import sys


def main():
    args = [
        "pyinstaller",
        "--name", "MediaServiceHub",
        "--onefile",
        "--add-data", "web:web",
        "--add-data", "admin_api.py:.",
        "--add-data", "api_routes.py:.",
        "--add-data", "config_manager.py:.",
        "--add-data", "database.py:.",
        "--add-data", "external_players.py:.",
        "--add-data", "logger.py:.",
        "--add-data", "p115_client_wrapper.py:.",
        "--add-data", "proxy_app.py:.",
        "--add-data", "redirect_service.py:.",
        "--add-data", "strm_generator.py:.",
        "--add-data", "windows_tray.py:.",
        "--hidden-import", "uvicorn.logging",
        "--hidden-import", "uvicorn.loops.auto",
        "--hidden-import", "uvicorn.protocols.http.auto",
        "--hidden-import", "httpx",
        "--hidden-import", "websockets",
        "--hidden-import", "p115client",
        "--hidden-import", "p115rsacipher",
        "--hidden-import", "pystray",
        "--hidden-import", "PIL",
        "--collect-all", "fastapi",
        "--collect-all", "starlette",
        "--noconfirm",
        "main.py",
    ]
    print("Building MediaServiceHub ...")
    result = subprocess.run(args, capture_output=False)
    if result.returncode == 0:
        print("Build successful! Output: dist/MediaServiceHub.exe")
    else:
        print("Build failed!")
        sys.exit(1)


if __name__ == "__main__":
    main()