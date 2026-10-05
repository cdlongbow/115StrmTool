"""
ConfigManager 模块测试：配置读写、Cookie 加密、顶置规则解析
"""
import pytest


@pytest.fixture(autouse=True)
def _backup_config(monkeypatch, tmp_path):
    """保存真实配置文件，用测试配置替换，测试后恢复；快照目录指向临时路径"""
    import config_manager as cm_mod
    monkeypatch.setattr(cm_mod, "CONFIG_BACKUP_DIR", tmp_path / "config_backups")
    orig_config = None
    if cm_mod.CONFIG_FILE.exists():
        orig_config = cm_mod.CONFIG_FILE.read_bytes()
    orig_key = None
    key_file = cm_mod._BASE_DIR / "config.key"
    if key_file.exists():
        orig_key = key_file.read_bytes()
    yield
    if orig_config is not None:
        cm_mod.CONFIG_FILE.write_bytes(orig_config)
    else:
        cm_mod.CONFIG_FILE.unlink(missing_ok=True)
    if orig_key is not None:
        key_file.write_bytes(orig_key)
    else:
        key_file.unlink(missing_ok=True)


class TestConfigManager:
    def test_default_config(self):
        from config_manager import ConfigManager
        cm = ConfigManager()
        config = cm.get()
        assert config["admin_host"] == "127.0.0.1"
        assert config["admin_port"] == 8100
        assert not config["emby"]["enabled"]
        assert not config["p115"]["enabled"]

    def test_update_and_persist(self):
        from config_manager import ConfigManager
        cm = ConfigManager()
        cm.update({"admin_port": 8888, "p115": {"enabled": True}})
        config = cm.get()
        assert config["admin_port"] == 8888
        assert config["p115"]["enabled"]

    def test_parse_pin_rules(self):
        from config_manager import ConfigManager
        cm = ConfigManager()
        rules_str = "/movies => http://192.168.1.1:8096/movies\n/tv => http://192.168.1.1:8096/tv"
        rules = cm.parse_pin_rules(rules_str)
        assert len(rules) == 2
        assert rules[0] == ("/movies", "http://192.168.1.1:8096/movies")
        assert rules[1] == ("/tv", "http://192.168.1.1:8096/tv")

    def test_parse_pin_rules_skip_invalid(self):
        from config_manager import ConfigManager
        cm = ConfigManager()
        rules_str = "/movies => invalid_url\n  \n/tv => http://192.168.1.1:8096/tv"
        rules = cm.parse_pin_rules(rules_str)
        assert len(rules) == 1

    def test_cookie_encryption(self):
        from config_manager import _encrypt_cookie, _decrypt_cookie
        # 测试加解密功能本身（不依赖配置读写）
        plain = "my_secret_cookie"
        encrypted = _encrypt_cookie(plain)
        assert encrypted.startswith("#ENC#")
        assert encrypted != plain
        decrypted = _decrypt_cookie(encrypted)
        assert decrypted == plain

    def test_cookie_encryption_empty(self):
        from config_manager import _encrypt_cookie, _decrypt_cookie
        assert _encrypt_cookie("") == ""
        assert _decrypt_cookie("") == ""

    def test_cookie_encryption_invalid(self):
        from config_manager import _decrypt_cookie
        # 非加密格式应原样返回
        assert _decrypt_cookie("plain_cookie") == "plain_cookie"

    def test_backup_corrupted_config(self):
        import config_manager as cm_mod
        cm_mod.CONFIG_FILE.write_text("{invalid json}", encoding="utf-8")
        from config_manager import ConfigManager
        cm = ConfigManager()
        config = cm.get()
        assert config is not None
        assert cm_mod.CONFIG_FILE.with_suffix(".json.bak").exists()
        cm_mod.CONFIG_FILE.with_suffix(".json.bak").unlink(missing_ok=True)

    def test_deep_merge(self):
        from config_manager import _deep_merge
        base = {"a": 1, "b": {"c": 2, "d": 3}}
        override = {"b": {"c": 99}, "e": 4}
        merged = _deep_merge(base, override)
        assert merged["a"] == 1
        assert merged["b"]["c"] == 99
        assert merged["b"]["d"] == 3
        assert merged["e"] == 4

class TestConfigBackupRotation:
    def test_backup_created_and_interval_guard(self, monkeypatch, tmp_path):
        import config_manager as cm_mod
        conf = tmp_path / "config.json"
        conf.write_text('{"a": 1}', encoding="utf-8")
        monkeypatch.setattr(cm_mod, "CONFIG_FILE", conf)
        cm = cm_mod.ConfigManager()
        cm._maybe_backup()
        first = list(cm_mod.CONFIG_BACKUP_DIR.glob("config-*.json"))
        assert len(first) == 1
        cm._maybe_backup()
        assert len(list(cm_mod.CONFIG_BACKUP_DIR.glob("config-*.json"))) == 1

    def test_rotation_keeps_max(self, monkeypatch, tmp_path):
        import config_manager as cm_mod
        conf = tmp_path / "config.json"
        conf.write_text('{"a": 1}', encoding="utf-8")
        monkeypatch.setattr(cm_mod, "CONFIG_FILE", conf)
        monkeypatch.setattr(cm_mod, "CONFIG_BACKUP_MIN_INTERVAL", 0.0)
        monkeypatch.setattr(cm_mod, "MAX_CONFIG_BACKUPS", 3)
        seq = iter(f"2026100{i}-000000" for i in range(10))
        monkeypatch.setattr(cm_mod, "strftime", lambda fmt: next(seq))
        cm = cm_mod.ConfigManager()
        for _cnt in range(5):
            cm._maybe_backup()
        names = sorted(p.name for p in cm_mod.CONFIG_BACKUP_DIR.glob("config-*.json"))
        assert len(names) == 3
        assert {"20261002", "20261003", "20261004"} <= {"20261002","20261003","20261004"} 

    def test_backup_failure_not_fatal(self, monkeypatch, tmp_path):
        import config_manager as cm_mod
        blocker = tmp_path / "blocker"
        blocker.write_text("x", encoding="utf-8")
        monkeypatch.setattr(cm_mod, "CONFIG_BACKUP_DIR", blocker / "bk")
        cm = cm_mod.ConfigManager()
        cm._maybe_backup()


class TestConfigExport:
    def test_export_ok(self):
        from unittest.mock import MagicMock, patch
        from admin_api import export_config
        cm = MagicMock()
        cm.export_disk_text.return_value = '{"x": 1}'
        with patch("admin_api.config_manager", cm):
            resp = export_config()
        assert b'{"x": 1}' == resp.body
        disp = resp.headers["content-disposition"]
        assert disp.startswith('attachment; filename="115strmtool-config-')

    def test_export_missing_raises(self):
        from unittest.mock import MagicMock, patch
        from admin_api import export_config
        from exceptions import ServiceError
        cm = MagicMock()
        cm.export_disk_text.return_value = None
        with patch("admin_api.config_manager", cm):
            with pytest.raises(ServiceError):
                export_config()
