"""
115 状态快照测试：默认只读本地统计，显式请求时才读取用户与存储信息
"""
import time

import api_routes
from admin_api import get_p115_status


class _ReadyClient:
    """
    状态快照用 115 客户端替身

    Attributes:
        user_calls: 用户信息接口调用次数
        storage_calls: 存储信息接口调用次数
    """

    def __init__(self):
        self.user_calls = 0
        self.storage_calls = 0

    def is_ready(self) -> bool:
        """
        返回客户端就绪状态

        :return bool: 固定返回可读取状态
        """
        return True

    def get_user_info(self):
        """
        返回用户信息并计数

        :return Dict: 固定用户信息
        """
        self.user_calls += 1
        return {"user_name": "ok"}

    def get_storage_info(self):
        """
        返回存储信息并计数

        :return Dict: 固定存储信息
        """
        self.storage_calls += 1
        return {"total_size": 10}


class _FailingStorageClient(_ReadyClient):
    """
    存储信息读取失败的 115 客户端替身
    """

    def get_storage_info(self):
        """
        模拟 115 SDK 在登录失效或网络异常时抛错

        :raises RuntimeError: 固定抛出存储读取错误
        """
        self.storage_calls += 1
        raise RuntimeError("cookie expired")


def _patch_status_state(monkeypatch):
    """
    隔离状态快照会触碰的本地统计与远程缓存

    :param monkeypatch: pytest monkeypatch
    """
    monkeypatch.setattr(api_routes.db, "get_stats", lambda: {"total_files": 7})
    monkeypatch.setattr(
        api_routes,
        "_status_remote_cache",
        {"ts": 0.0, "user_info": None, "storage": None},
    )


def test_build_p115_status_default_skips_remote(monkeypatch):
    """
    未显式请求远程字段时不应访问 115 用户与存储接口
    """
    _patch_status_state(monkeypatch)
    client = _ReadyClient()
    result = api_routes.build_p115_status(client)
    assert result["client_ready"] is True
    assert result["stats"]["total_files"] == 7
    assert result["user_info"] is None
    assert result["storage"] is None
    assert client.user_calls == 0
    assert client.storage_calls == 0


def test_build_p115_status_include_remote_uses_cache(monkeypatch):
    """
    显式请求远程字段时首次拉取，30 秒内复用进程内缓存
    """
    _patch_status_state(monkeypatch)
    client = _ReadyClient()
    first = api_routes.build_p115_status(client, include_remote=True)
    monkeypatch.setattr(api_routes, "_status_remote_cache", {
        "ts": time.monotonic(),
        "user_info": first["user_info"],
        "storage": first["storage"],
    })
    second = api_routes.build_p115_status(client, include_remote=True)
    assert first["user_info"] == {"user_name": "ok"}
    assert first["storage"] == {"total_size": 10}
    assert second["user_info"] == first["user_info"]
    assert second["storage"] == first["storage"]
    assert client.user_calls == 1
    assert client.storage_calls == 1


def test_build_p115_status_include_remote_defaults_none_on_error(monkeypatch):
    """
    远程存储读取失败时状态快照保留空字段且不中断
    """
    _patch_status_state(monkeypatch)
    client = _FailingStorageClient()
    result = api_routes.build_p115_status(client, include_remote=True)
    assert result["client_ready"] is True
    assert result["user_info"] is None
    assert result["storage"] is None
    assert client.user_calls == 1
    assert client.storage_calls == 1


def test_admin_p115_status_default_skips_remote(monkeypatch):
    """
    管理端状态端点默认沿用本地模式并返回 running
    """
    _patch_status_state(monkeypatch)
    client = _ReadyClient()
    monkeypatch.setitem(get_p115_status.__globals__["_p115_client_ref"], "instance", client)
    monkeypatch.setitem(get_p115_status.__globals__["_p115_status"], "running", True)
    result = get_p115_status()
    assert result["running"] is True
    assert result["client_ready"] is True
    assert result["user_info"] is None
    assert client.user_calls == 0
    assert client.storage_calls == 0
