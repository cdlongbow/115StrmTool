"""
应用版本号测试：运行时可读、注入缺失回退 dev、/admin/api/status 透传
"""
import importlib
from unittest.mock import MagicMock, patch


def test_app_version_nonempty_str():
    from app_metadata import APP_VERSION
    assert isinstance(APP_VERSION, str) and APP_VERSION


def test_version_matches_injection_or_dev_fallback():
    module = importlib.reload(importlib.import_module("app_metadata"))
    try:
        from _build_version import __version__ as injected
    except ImportError:
        assert module.APP_VERSION == "dev"
    else:
        assert module.APP_VERSION == injected


def test_combined_status_exposes_version():
    from admin_api import get_combined_status
    from app_metadata import APP_VERSION

    with patch("admin_api.config_manager", MagicMock(get=lambda: {})):
        status = get_combined_status()
    assert status["version"] == APP_VERSION
