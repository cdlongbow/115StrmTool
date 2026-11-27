"""
冻结 exe 冒烟脚本：启动无托盘实例并校验 Web、API、健康检查与版本后清理进程
"""
import argparse
import json
import os
import subprocess
import time
from pathlib import Path
from typing import Any, Dict
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


def _wait_for_config(config_path: Path, timeout: float) -> Dict[str, Any]:
    """
    等待应用生成配置文件

    :param config_path (Path): 配置文件路径
    :param timeout (float): 超时秒数

    :return Dict: 配置内容
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            return json.loads(config_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            time.sleep(0.2)
    raise TimeoutError(f"配置文件未生成: {config_path}")


def _request(url: str, token: str = "") -> Any:
    """
    发起带管理令牌的 HTTP 请求

    :param url (str): 请求地址
    :param token (str): 管理令牌

    :return Any: 响应对象
    """
    headers = {"X-Admin-Token": token} if token else {}
    return urlopen(Request(url, headers=headers), timeout=2)


def _check_endpoints(base_url: str, token: str, version: str, timeout: float) -> None:
    """
    轮询校验 Web 页面、管理 API、健康检查与版本号

    :param base_url (str): 管理服务基地址
    :param token (str): 管理令牌
    :param version (str): 预期版本号
    :param timeout (float): 超时秒数

    :return None:
    """
    deadline = time.monotonic() + timeout
    last_error = "服务未就绪"
    while time.monotonic() < deadline:
        try:
            with _request(base_url + "/") as response:
                html = response.read().decode("utf-8")
                if response.status != 200 or "115网盘STRM生成与302工具" not in html:
                    raise ValueError("Web 页面响应异常")
            with _request(base_url + "/admin/api/status", token) as response:
                payload = json.loads(response.read().decode("utf-8"))
                if response.status != 200 or payload.get("version") != version:
                    raise ValueError(
                        f"API 版本不匹配: expected={version!r} actual={payload.get('version')!r}"
                    )
            with _request(base_url + "/health/live") as response:
                payload = json.loads(response.read().decode("utf-8"))
                if response.status != 200 or payload.get("status") != "ok":
                    raise ValueError("存活健康检查响应异常")
            with _request(base_url + "/health/ready") as response:
                payload = json.loads(response.read().decode("utf-8"))
                if response.status != 200 or payload.get("status") != "ready":
                    raise ValueError("就绪健康检查响应异常")
            return
        except (HTTPError, URLError, TimeoutError, ValueError, json.JSONDecodeError) as exc:
            last_error = str(exc)
            time.sleep(0.3)
    raise TimeoutError(f"Web/API/健康检查/版本冒烟失败: {last_error}")


def _stop_process(process: subprocess.Popen) -> None:
    """
    终止冒烟进程并确认退出

    :param process (subprocess.Popen): 被测进程

    :return None:
    """
    if process.poll() is None:
        process.terminate()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)
    if process.poll() is None:
        raise TimeoutError("冒烟进程未能在超时内退出")


def smoke_frozen_exe(exe_path: Path, data_dir: Path, version: str, timeout: float) -> None:
    """
    执行冻结 exe 冒烟测试

    :param exe_path (Path): exe 路径
    :param data_dir (Path): 独立数据目录
    :param version (str): 预期版本号
    :param timeout (float): 服务就绪超时秒数

    :return None:
    """
    data_dir.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env["APP_DATA_DIR"] = str(data_dir)
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    process = subprocess.Popen(
        [str(exe_path), "--no-tray"],
        cwd=str(exe_path.parent),
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=creationflags,
    )
    try:
        config = _wait_for_config(data_dir / "config.json", timeout)
        host = str(config.get("admin_host") or "127.0.0.1")
        if host in ("0.0.0.0", "", "::"):
            host = "127.0.0.1"
        port = int(config.get("admin_port") or 8100)
        base_url = f"http://{host}:{port}"
        token = str(config.get("admin_token") or "")
        _check_endpoints(base_url, token, version, timeout)
        print(f"OK: Web/API/health/version {version}")
    finally:
        _stop_process(process)


def main() -> int:
    """
    脚本入口

    :return int: 进程退出码
    """
    parser = argparse.ArgumentParser()
    parser.add_argument("--exe", required=True)
    parser.add_argument("--data-dir", required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--timeout", type=float, default=60.0)
    args = parser.parse_args()
    smoke_frozen_exe(Path(args.exe), Path(args.data_dir), args.version, args.timeout)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
