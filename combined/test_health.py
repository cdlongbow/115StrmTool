"""
管理应用健康检查端点测试
"""
import copy
import socket
from pathlib import Path
from types import ModuleType
from typing import Any, Dict, Iterator

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def main_module(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    """
    在隔离数据目录中加载管理应用模块并复用已有模块缓存

    :param tmp_path (Path): 临时数据目录
    :param monkeypatch (pytest.MonkeyPatch): 测试替换工具

    :return ModuleType: 管理应用模块
    """
    monkeypatch.setenv("APP_DATA_DIR", str(tmp_path))
    import main

    return main


@pytest.fixture
def health_config() -> Dict[str, Any]:
    """
    返回包含敏感占位值的完整测试配置

    :return Dict: 管理令牌、远端地址和账号配置
    """
    return {
        "admin_token": "health-test-admin-token",
        "admin_host": "10.20.30.40",
        "admin_port": 18100,
        "p115": {
            "enabled": True,
            "cookie": "health-test-cookie",
            "strm_url_prefix": "http://health-user.example:3333",
        },
        "emby": {"enabled": True, "emby_host": "http://emby-user.example:8096"},
        "webdav": {
            "enabled": True,
            "url": "https://webdav-user.example/private",
            "username": "health-webdav-user",
            "password": "health-test-password",
        },
    }


@pytest.fixture
def client(
    main_module: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    health_config: Dict[str, Any],
) -> Iterator[TestClient]:
    """
    创建使用隔离配置的管理应用测试客户端

    :param main_module (ModuleType): 管理应用模块
    :param monkeypatch (pytest.MonkeyPatch): 测试替换工具
    :param health_config (Dict): 测试配置

    :return Iterator: 测试客户端生成器
    """
    monkeypatch.setattr(main_module.config_manager, "get", lambda: copy.deepcopy(health_config))
    with TestClient(main_module.create_admin_app()) as test_client:
        yield test_client


def test_health_live_returns_minimal_response_without_token(client: TestClient) -> None:
    """
    存活端点无需令牌并仅返回进程状态
    """
    response = client.get("/health/live")

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/json"
    assert response.json() == {"status": "ok"}


def test_health_ready_returns_minimal_response_after_initialization(client: TestClient) -> None:
    """
    管理应用初始化完成后就绪端点返回最小状态
    """
    response = client.get("/health/ready")

    assert client.app.state.ready is True
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/json"
    assert response.json() == {"status": "ready"}


def test_health_readiness_follows_application_lifespan(main_module: ModuleType) -> None:
    """
    就绪状态仅在管理应用 lifespan 运行期间置位
    """
    app = main_module.create_admin_app()
    assert getattr(app.state, "ready", False) is False

    with TestClient(app) as started_client:
        assert app.state.ready is True
        assert started_client.get("/health/live").status_code == 200
        ready_response = started_client.get("/health/ready")
        assert ready_response.status_code == 200
        assert ready_response.json() == {"status": "ready"}

    assert app.state.ready is False
    unstarted_client = TestClient(app)
    try:
        assert unstarted_client.get("/health/live").status_code == 200
        pending_response = unstarted_client.get("/health/ready")
        assert pending_response.status_code == 503
        assert pending_response.json() == {"status": "not_ready"}
    finally:
        unstarted_client.close()


def test_health_ready_reports_pending_initialization_without_affecting_liveness(
    client: TestClient,
) -> None:
    """
    初始化未完成时就绪端点返回 503 存活端点保持可响应
    """
    client.app.state.ready = False

    ready_response = client.get("/health/ready")
    live_response = client.get("/health/live")

    assert ready_response.status_code == 503
    assert ready_response.json() == {"status": "not_ready"}
    assert live_response.status_code == 200
    assert live_response.json() == {"status": "ok"}


def test_health_responses_do_not_expose_sensitive_configuration(client: TestClient) -> None:
    """
    健康响应保持固定字段且不包含令牌、地址、路径或账号信息
    """
    for path in ("/health/live", "/health/ready"):
        response = client.get(path, headers={"X-Admin-Token": "wrong-token"})
        body = response.text

        assert response.status_code == 200
        assert set(response.json()) == {"status"}
        for sensitive_value in (
            "health-test-admin-token",
            "health-test-cookie",
            "health-test-password",
            "health-webdav-user",
            "10.20.30.40",
            "18100",
            "health-user.example",
            "emby-user.example",
            "webdav-user.example",
            "/private",
            "admin_token",
            "cookie",
            "password",
            "username",
        ):
            assert sensitive_value not in body


def test_health_endpoints_do_not_access_external_services(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    健康端点不触发 DNS 或外部服务连接
    """
    def _block_network(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("健康端点不应访问网络")

    monkeypatch.setattr(socket, "getaddrinfo", _block_network)
    monkeypatch.setattr(socket, "create_connection", _block_network)
    monkeypatch.setattr(socket.socket, "connect", _block_network)
    monkeypatch.setattr(socket.socket, "connect_ex", _block_network)

    assert client.get("/health/live").status_code == 200
    assert client.get("/health/ready").status_code == 200


@pytest.mark.parametrize("path", ["/api/status", "/admin/api/status"])
def test_health_addition_preserves_existing_api_authentication(client: TestClient, path: str) -> None:
    """
    新增健康端点后现有 API 仍要求正确管理令牌
    """
    assert client.get(path).status_code == 401
    assert client.get(path, headers={"X-Admin-Token": "wrong-token"}).status_code == 401
    assert client.get(path, headers={"X-Admin-Token": "health-test-admin-token"}).status_code == 200
