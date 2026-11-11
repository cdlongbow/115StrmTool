"""管理面板「播放诊断日志」开关：配置持久化、立即生效与异常兜底。"""
from pathlib import Path
from unittest.mock import MagicMock, patch

from admin_api import ConfigUpdateRequest, update_config
from config_manager import RootConfig, config_manager
from utils import MEDIA_DIAG_ENV_VAR, media_diag_enabled


def test_root_config_default_off():
    assert RootConfig().media_diag is False


def test_update_config_persists_diag_flag():
    cm = MagicMock()
    cm.get.return_value = {"media_diag": True}
    with patch("admin_api.config_manager", cm):
        result = update_config(ConfigUpdateRequest(media_diag=True))
    cm.update.assert_called_once_with({"media_diag": True})
    assert result["media_diag"] is True


def test_update_config_without_flag_leaves_it_untouched():
    cm = MagicMock()
    cm.get.return_value = {}
    with patch("admin_api.config_manager", cm):
        update_config(ConfigUpdateRequest(p115={"enabled": True}))
    assert "media_diag" not in cm.update.call_args.args[0]


def test_panel_switch_takes_effect_without_restart(monkeypatch):
    monkeypatch.delenv(MEDIA_DIAG_ENV_VAR, raising=False)
    monkeypatch.setattr(config_manager, "get_flag", lambda key, default=False: False)
    assert media_diag_enabled() is False
    monkeypatch.setattr(config_manager, "get_flag", lambda key, default=False: True)
    assert media_diag_enabled() is True


def test_env_overrides_panel(monkeypatch):
    monkeypatch.setattr(config_manager, "get_flag", lambda key, default=False: True)
    monkeypatch.setenv(MEDIA_DIAG_ENV_VAR, "0")
    assert media_diag_enabled() is False
    monkeypatch.setenv(MEDIA_DIAG_ENV_VAR, "1")
    monkeypatch.setattr(config_manager, "get_flag", lambda key, default=False: False)
    assert media_diag_enabled() is True


def test_abnormal_config_value_fails_closed(monkeypatch):
    """配置模块被替身或返回非 bool 时一律按关闭处理。"""
    monkeypatch.delenv(MEDIA_DIAG_ENV_VAR, raising=False)
    monkeypatch.setattr(
        config_manager, "get_flag", lambda key, default=False: MagicMock()
    )
    assert media_diag_enabled() is False


def test_admin_ui_exposes_diag_switch():
    """前端开关的元素、变更绑定与状态回填必须齐全，否则界面上找不到开关。"""
    html = (Path(__file__).parent / "web" / "index.html").read_text(encoding="utf-8")
    assert 'id="media_diag"' in html
    assert 'onchange="saveMediaDiag()"' in html
    assert "body:JSON.stringify({media_diag: enabled})" in html
    assert "config.media_diag === true" in html
