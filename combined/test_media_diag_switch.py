"""诊断日志开关：正式发版默认静默，需要排查时显式开启。"""
from pathlib import Path

from utils import MEDIA_DIAG_ENV_VAR, media_diag_enabled


def test_diag_disabled_by_default(monkeypatch):
    monkeypatch.delenv(MEDIA_DIAG_ENV_VAR, raising=False)
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
