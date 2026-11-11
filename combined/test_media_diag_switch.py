"""诊断日志开关：默认关闭，管理面板配置或环境变量显式开启。"""
from pathlib import Path

import pytest

from config_manager import config_manager
from utils import MEDIA_DIAG_ENV_VAR, media_diag_enabled


@pytest.fixture(autouse=True)
def _isolated_config(monkeypatch):
    """隔离测试环境：不依赖本机 config.json，也不受其它用例污染。"""
    monkeypatch.delenv(MEDIA_DIAG_ENV_VAR, raising=False)
    monkeypatch.setattr(config_manager, "get_flag", lambda key, default=False: False)


def test_diag_disabled_by_default():
    assert media_diag_enabled() is False


def test_diag_enabled_values(monkeypatch):
    for value in ("1", "true", "YES", " on "):
        monkeypatch.setenv(MEDIA_DIAG_ENV_VAR, value)
        assert media_diag_enabled() is True, value


def test_diag_disabled_values(monkeypatch):
    for value in ("", "0", "false", "off", "no", "  "):
        monkeypatch.setenv(MEDIA_DIAG_ENV_VAR, value)
        assert media_diag_enabled() is False, value


def test_every_diag_log_is_gated():
    """防止后续新增诊断日志时忘记套开关，导致正式版又写日志。"""
    for name in ("proxy_app.py", "redirect_service.py"):
        lines = Path(__file__).with_name(name).read_text(encoding="utf-8").splitlines()
        for idx, line in enumerate(lines):
            if "诊断:" not in line or line.lstrip().startswith("#"):
                continue
            window = lines[max(0, idx - 12):idx + 1]
            assert any("if media_diag_enabled():" in w for w in window), (
                f"{name}:{idx + 1} 诊断日志未受开关保护: {line.strip()[:60]}"
            )
