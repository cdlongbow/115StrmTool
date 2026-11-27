"""
ConfigManager 模块测试：配置读写、Cookie 加密、顶置规则解析
"""
import base64
import hashlib
import json
import os
from pathlib import Path
from typing import Any, Dict

import pytest

_VALID_P115_COOKIE = "UID=100;CID=200;SEID=abc123"


@pytest.fixture(autouse=True)
def _isolate_config(monkeypatch, tmp_path):
    """将配置、密钥与快照全部隔离到临时目录"""
    import _paths

    config_file = tmp_path / "config.json"
    key_file = tmp_path / "config.key"
    backup_dir = tmp_path / "config_backups"
    monkeypatch.setattr(_paths, "CONFIG_FILE", config_file)
    monkeypatch.setattr(_paths, "CONFIG_KEY_FILE", key_file)
    monkeypatch.setattr(_paths, "CONFIG_BACKUP_DIR", backup_dir)
    monkeypatch.setattr(_paths, "_BASE_DIR", tmp_path)

    import config_manager as cm_mod

    monkeypatch.setattr(cm_mod, "CONFIG_FILE", config_file)
    monkeypatch.setattr(cm_mod, "CONFIG_KEY_FILE", key_file)
    monkeypatch.setattr(cm_mod, "CONFIG_BACKUP_DIR", backup_dir)
    monkeypatch.setattr(cm_mod, "_BASE_DIR", tmp_path)
    yield


def _legacy_encrypt(plaintext: str, key: bytes) -> str:
    salt = bytes(range(16))
    derived = hashlib.pbkdf2_hmac("sha256", key, salt, 100000)
    encrypted = bytes(
        byte ^ derived[index % len(derived)]
        for index, byte in enumerate(plaintext.encode("utf-8"))
    )
    return "#ENC#" + base64.b64encode(salt + encrypted).decode("ascii")


def _write_config(data: Dict[str, Any]) -> None:
    from config_manager import CONFIG_FILE

    CONFIG_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


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
        from config_manager import _decrypt_cookie, _encrypt_cookie, _key_metadata_file
        # 测试加解密功能本身（不依赖配置读写）
        plain = "my_secret_cookie"
        encrypted = _encrypt_cookie(plain)
        assert encrypted.startswith("#ENC#")
        assert encrypted != plain
        assert _key_metadata_file().exists()
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
        assert names == [
            "config-20261003-000000.json",
            "config-20261004-000000.json",
            "config-20261005-000000.json",
        ]

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


class TestWebdavBackupConfig:
    def test_webdav_defaults(self):
        from config_manager import ConfigManager
        cm = ConfigManager()
        webdav = cm.get()["webdav"]
        assert webdav["enabled"] is False
        assert webdav["url"] == ""
        assert webdav["remote_subdir"] == "115strmtool-backups"
        assert webdav["keep_count"] == 10

    def test_webdav_password_encrypted_on_disk(self):
        from config_manager import CONFIG_FILE, ConfigManager
        cm = ConfigManager()
        cm.update({"webdav": {"url": "https://dav.example.com/d", "password": "s3cret-pw"}})
        disk_text = CONFIG_FILE.read_text(encoding="utf-8")
        assert "s3cret-pw" not in disk_text
        assert "#ENC#" in disk_text
        reopened = ConfigManager()
        assert reopened.get()["webdav"]["password"] == "s3cret-pw"
        assert reopened.get()["webdav"]["url"] == "https://dav.example.com/d"

    def test_mask_config_webdav_password(self):
        from config_manager import COOKIE_MASK, mask_config
        masked = mask_config(
            {"p115": {"cookie": "c"}, "webdav": {"password": "p", "url": "u"}, "admin_token": "t"}
        )
        assert masked["webdav"]["password"] == COOKIE_MASK
        assert masked["webdav"]["url"] == "u"
        assert masked["p115"]["cookie"] == COOKIE_MASK
        assert masked["admin_token"] == COOKIE_MASK

    def test_mask_config_without_webdav_section(self):
        from config_manager import mask_config
        masked = mask_config({"admin_host": "127.0.0.1"})
        assert masked == {"admin_host": "127.0.0.1"}

    def test_plaintext_password_backward_compat(self, monkeypatch, tmp_path):
        import json

        import config_manager as cm_mod
        conf = tmp_path / "config.json"
        conf.write_text(
            json.dumps({"webdav": {"enabled": True, "password": "legacy-plain"}}),
            encoding="utf-8",
        )
        monkeypatch.setattr(cm_mod, "CONFIG_FILE", conf)
        cm = cm_mod.ConfigManager()
        assert cm.get()["webdav"]["password"] == "legacy-plain"
        disk_text = conf.read_text(encoding="utf-8")
        assert "legacy-plain" not in disk_text
        assert "#ENC#" in disk_text

    def test_keep_count_invalid_falls_back(self):
        from config_manager import ConfigManager
        cm = ConfigManager()
        cm.update({"webdav": {"keep_count": 0}})
        assert cm.get()["webdav"]["keep_count"] == 10
        cm.update({"webdav": {"keep_count": 5}})
        assert cm.get()["webdav"]["keep_count"] == 5


class TestCipherMigration:
    def test_legacy_ciphertext_migrates_and_preserves_unknown_fields(self, caplog):
        import config_manager as cm_mod

        key = b"\x01" * 32
        cookie = _VALID_P115_COOKIE
        password = "legacy-password-" + "p" * 64
        cm_mod.CONFIG_KEY_FILE.write_bytes(key)
        _write_config(
            {
                "p115": {
                    "enabled": True,
                    "cookie": _legacy_encrypt(cookie, key),
                    "future_p115": {"keep": 1},
                },
                "webdav": {
                    "enabled": True,
                    "password": _legacy_encrypt(password, key),
                    "future_webdav": [1, 2],
                },
                "future_top": {"keep": True},
            }
        )
        original = cm_mod.CONFIG_FILE.read_bytes()

        cm = cm_mod.ConfigManager()

        assert cm.get()["p115"]["cookie"] == cookie
        assert cm.get()["webdav"]["password"] == password
        disk_text = cm_mod.CONFIG_FILE.read_text(encoding="utf-8")
        disk = json.loads(disk_text)
        assert disk["p115"]["cookie"].startswith("#ENC#v2:")
        assert disk["webdav"]["password"].startswith("#ENC#v2:")
        assert disk["p115"]["future_p115"] == {"keep": 1}
        assert disk["webdav"]["future_webdav"] == [1, 2]
        assert disk["future_top"] == {"keep": True}
        assert cookie not in disk_text
        assert password not in disk_text
        assert cookie not in caplog.text
        assert password not in caplog.text
        assert "残余风险" in caplog.text
        assert cm_mod.CONFIG_KEY_FILE.read_bytes() == key
        metadata_file = cm_mod._key_metadata_file()
        metadata = json.loads(metadata_file.read_text(encoding="utf-8"))
        assert metadata["version"] == 1
        assert metadata["sha256"] == hashlib.sha256(key).hexdigest()
        backups = list(cm_mod.CONFIG_BACKUP_DIR.glob("config-*.json"))
        assert len(backups) == 1
        snapshot = json.loads(backups[0].read_text(encoding="utf-8"))
        assert snapshot["p115"]["cookie"].startswith("#ENC#v2:")
        assert snapshot["webdav"]["password"].startswith("#ENC#v2:")
        assert snapshot["future_top"] == {"keep": True}
        pre_migration = list(cm_mod.CONFIG_BACKUP_DIR.glob("pre-migration-config-*.json"))
        assert len(pre_migration) == 1
        assert pre_migration[0].read_bytes() == original
        if os.name != "nt":
            assert pre_migration[0].stat().st_mode & 0o777 == 0o600

    def test_legacy_migration_is_idempotent(self, monkeypatch):
        import config_manager as cm_mod

        key = b"\x01" * 32
        cm_mod.CONFIG_KEY_FILE.write_bytes(key)
        _write_config(
            {
                "p115": {"cookie": _legacy_encrypt(_VALID_P115_COOKIE, key)},
                "webdav": {"password": _legacy_encrypt("legacy-password", key)},
            }
        )
        cm_mod.ConfigManager()
        migrated = cm_mod.CONFIG_FILE.read_bytes()
        backups = list(cm_mod.CONFIG_BACKUP_DIR.glob("config-*.json"))
        assert len(backups) == 1

        def _unexpected_write(self):
            pytest.fail("已迁移配置不应在重复启动时再次写盘")

        monkeypatch.setattr(cm_mod.ConfigManager, "_write", _unexpected_write)
        reopened = cm_mod.ConfigManager()

        assert reopened.get()["p115"]["cookie"] == _VALID_P115_COOKIE
        assert reopened.get()["webdav"]["password"] == "legacy-password"
        assert cm_mod.CONFIG_FILE.read_bytes() == migrated
        assert list(cm_mod.CONFIG_BACKUP_DIR.glob("config-*.json")) == backups

    @pytest.mark.parametrize("cipher_version", ["legacy", "v2"])
    def test_wrong_key_rejects_without_changing_files(self, cipher_version, caplog):
        import config_manager as cm_mod

        key = b"\x01" * 32
        secret = _VALID_P115_COOKIE
        if cipher_version == "legacy":
            encrypted = _legacy_encrypt(secret, key)
        else:
            cm_mod.CONFIG_KEY_FILE.write_bytes(key)
            encrypted = cm_mod._encrypt_cookie(secret)
        _write_config({"p115": {"cookie": encrypted}, "future": {"keep": True}})
        cm_mod._key_metadata_file().unlink(missing_ok=True)
        cm_mod.CONFIG_KEY_FILE.write_bytes(b"\x02" * 32)
        config_before = cm_mod.CONFIG_FILE.read_bytes()
        key_before = cm_mod.CONFIG_KEY_FILE.read_bytes()

        with pytest.raises(ValueError, match="解密失败"):
            cm_mod.ConfigManager()

        assert cm_mod.CONFIG_FILE.read_bytes() == config_before
        assert cm_mod.CONFIG_KEY_FILE.read_bytes() == key_before
        assert not cm_mod._key_metadata_file().exists()
        assert secret not in caplog.text

    def test_failed_wrong_key_does_not_create_metadata_and_correct_key_recovers(self):
        import config_manager as cm_mod

        correct_key = b"\x01" * 32
        wrong_key = b"\x02" * 32
        _write_config(
            {
                "p115": {"cookie": _legacy_encrypt(_VALID_P115_COOKIE, correct_key)},
                "future": {"keep": True},
            }
        )
        cm_mod.CONFIG_KEY_FILE.write_bytes(wrong_key)
        config_before = cm_mod.CONFIG_FILE.read_bytes()

        with pytest.raises(ValueError, match="解密失败"):
            cm_mod.ConfigManager()

        assert cm_mod.CONFIG_FILE.read_bytes() == config_before
        assert not cm_mod._key_metadata_file().exists()
        assert cm_mod._read_encryption_key() == wrong_key
        assert not cm_mod._key_metadata_file().exists()

        cm_mod.CONFIG_KEY_FILE.write_bytes(correct_key)
        cm = cm_mod.ConfigManager()

        assert cm.get()["p115"]["cookie"] == _VALID_P115_COOKIE
        metadata_file = cm_mod._key_metadata_file()
        metadata = json.loads(metadata_file.read_text(encoding="utf-8"))
        assert metadata["sha256"] == hashlib.sha256(correct_key).hexdigest()

    def test_missing_key_rejects_without_creating_key(self):
        import config_manager as cm_mod

        key = b"\x01" * 32
        _write_config({"p115": {"cookie": _legacy_encrypt(_VALID_P115_COOKIE, key)}})
        config_before = cm_mod.CONFIG_FILE.read_bytes()

        with pytest.raises(ValueError, match="缺少密钥"):
            cm_mod.ConfigManager()

        assert cm_mod.CONFIG_FILE.read_bytes() == config_before
        assert not cm_mod.CONFIG_KEY_FILE.exists()

    def test_v2_ciphertext_is_compatible_and_not_rewritten(self, monkeypatch):
        import config_manager as cm_mod

        key = b"\x01" * 32
        cm_mod.CONFIG_KEY_FILE.write_bytes(key)
        cookie = _VALID_P115_COOKIE
        password = "current-password"
        raw = json.loads(json.dumps(cm_mod.DEFAULT_CONFIG))
        raw["p115"]["cookie"] = cm_mod._encrypt_cookie(cookie)
        raw["webdav"]["password"] = cm_mod._encrypt_cookie(password)
        raw["future_top"] = {"keep": True}
        _write_config(raw)
        config_before = cm_mod.CONFIG_FILE.read_bytes()

        def _unexpected_write(self):
            pytest.fail("新版密文配置不应在重复启动时再次写盘")

        monkeypatch.setattr(cm_mod.ConfigManager, "_write", _unexpected_write)
        cm = cm_mod.ConfigManager()

        assert cm.get()["p115"]["cookie"] == cookie
        assert cm.get()["webdav"]["password"] == password
        assert cm.get()["future_top"] == {"keep": True}
        assert cm_mod.CONFIG_FILE.read_bytes() == config_before
        assert cm_mod.CONFIG_KEY_FILE.read_bytes() == key

    def test_plaintext_secrets_are_migrated_to_v2(self):
        import config_manager as cm_mod

        _write_config(
            {
                "p115": {"cookie": _VALID_P115_COOKIE, "future_p115": 1},
                "webdav": {"password": "plain-password", "future_webdav": 2},
                "future_top": 3,
            }
        )

        cm = cm_mod.ConfigManager()

        assert cm.get()["p115"]["cookie"] == _VALID_P115_COOKIE
        assert cm.get()["webdav"]["password"] == "plain-password"
        disk = json.loads(cm_mod.CONFIG_FILE.read_text(encoding="utf-8"))
        assert disk["p115"]["cookie"].startswith("#ENC#v2:")
        assert disk["webdav"]["password"].startswith("#ENC#v2:")
        assert disk["p115"]["future_p115"] == 1
        assert disk["webdav"]["future_webdav"] == 2
        assert disk["future_top"] == 3

    def test_ciphertext_is_not_encrypted_again(self):
        from config_manager import _encrypt_cookie, _encrypt_secrets

        encrypted = _encrypt_cookie("secret-value")
        config = {
            "p115": {"cookie": encrypted},
            "webdav": {"password": encrypted},
        }

        _encrypt_secrets(config)

        assert config["p115"]["cookie"] == encrypted
        assert config["webdav"]["password"] == encrypted

    def test_pre_migration_backup_ignores_rotation_interval(self):
        import config_manager as cm_mod

        key = b"\x01" * 32
        cm_mod.CONFIG_KEY_FILE.write_bytes(key)
        _write_config(
            {
                "p115": {"cookie": _legacy_encrypt(_VALID_P115_COOKIE, key)},
                "future": {"keep": True},
            }
        )
        original = cm_mod.CONFIG_FILE.read_bytes()
        cm_mod.CONFIG_BACKUP_DIR.mkdir(mode=0o700)
        recent = cm_mod.CONFIG_BACKUP_DIR / "config-recent.json"
        recent.write_bytes(b"{}")

        cm_mod.ConfigManager()

        pre_migration = list(cm_mod.CONFIG_BACKUP_DIR.glob("pre-migration-config-*.json"))
        assert len(pre_migration) == 1
        assert pre_migration[0].read_bytes() == original
        if os.name != "nt":
            assert pre_migration[0].stat().st_mode & 0o777 == 0o600
        assert list(cm_mod.CONFIG_BACKUP_DIR.glob("config-*.json")) == [recent]

    def test_wrong_key_with_valid_utf8_is_rejected(self, monkeypatch):
        import config_manager as cm_mod

        correct_key = b"\x01" * 32
        wrong_key = b"\x02" * 32
        cookie_bytes = _VALID_P115_COOKIE.encode("utf-8")
        wrong_plaintext = "x" * len(cookie_bytes)
        wrong_bytes = wrong_plaintext.encode("utf-8")
        assert wrong_bytes.decode("utf-8") == wrong_plaintext
        derived_correct = bytes(range(32))
        derived_wrong = bytearray(32)
        for index, byte in enumerate(cookie_bytes):
            derived_wrong[index] = derived_correct[index] ^ byte ^ wrong_bytes[index]
        salt = bytes(range(16))
        encrypted = bytes(
            byte ^ derived_correct[index % len(derived_correct)]
            for index, byte in enumerate(cookie_bytes)
        )
        encoded = "#ENC#" + base64.b64encode(salt + encrypted).decode("ascii")
        monkeypatch.setattr(
            cm_mod,
            "_derive_key",
            lambda key, _salt: bytes(derived_correct if key == correct_key else derived_wrong),
        )
        assert cm_mod._decrypt_cookie(encoded, key=wrong_key) == wrong_plaintext
        cm_mod.CONFIG_KEY_FILE.write_bytes(wrong_key)
        _write_config({"p115": {"cookie": encoded}})
        config_before = cm_mod.CONFIG_FILE.read_bytes()
        key_before = cm_mod.CONFIG_KEY_FILE.read_bytes()

        with pytest.raises(ValueError, match="结构校验失败"):
            cm_mod.ConfigManager()

        assert cm_mod.CONFIG_FILE.read_bytes() == config_before
        assert cm_mod.CONFIG_KEY_FILE.read_bytes() == key_before
        assert not list(cm_mod.CONFIG_BACKUP_DIR.glob("pre-migration-config-*.json"))

    def test_key_fingerprint_mismatch_rejects_without_changing_files(self):
        import config_manager as cm_mod

        key = b"\x01" * 32
        cm_mod.CONFIG_KEY_FILE.write_bytes(key)
        _write_config({"future": {"keep": True}})
        assert cm_mod._read_encryption_key() == key
        metadata_file = cm_mod._key_metadata_file()
        assert not metadata_file.exists()
        cm_mod._ensure_key_metadata(key)
        metadata = json.loads(metadata_file.read_text(encoding="utf-8"))
        assert metadata["version"] == 1
        assert metadata["sha256"] == hashlib.sha256(key).hexdigest()
        cm_mod.CONFIG_KEY_FILE.write_bytes(b"\x02" * 32)
        config_before = cm_mod.CONFIG_FILE.read_bytes()
        key_before = cm_mod.CONFIG_KEY_FILE.read_bytes()

        with pytest.raises(ValueError, match="指纹"):
            cm_mod.ConfigManager()

        assert cm_mod.CONFIG_FILE.read_bytes() == config_before
        assert cm_mod.CONFIG_KEY_FILE.read_bytes() == key_before

    @pytest.mark.skipif(os.name == "nt", reason="POSIX permissions")
    def test_existing_key_and_metadata_permissions_are_tightened(self):
        import config_manager as cm_mod

        key = b"\x01" * 32
        cm_mod.CONFIG_KEY_FILE.write_bytes(key)
        os.chmod(cm_mod.CONFIG_KEY_FILE, 0o644)
        assert cm_mod._read_encryption_key() == key
        assert cm_mod.CONFIG_KEY_FILE.stat().st_mode & 0o777 == 0o600
        metadata_file = cm_mod._key_metadata_file()
        assert not metadata_file.exists()
        cm_mod._ensure_key_metadata(key)
        os.chmod(metadata_file, 0o644)
        assert cm_mod._read_encryption_key() == key
        assert metadata_file.stat().st_mode & 0o777 == 0o600

    @pytest.mark.skipif(os.name == "nt", reason="POSIX permissions")
    def test_atomic_write_temp_file_is_0600(self, monkeypatch):
        import config_manager as cm_mod

        observed_modes = []
        real_replace = os.replace

        def _checked_replace(source, destination):
            observed_modes.append(Path(source).stat().st_mode & 0o777)
            return real_replace(source, destination)

        monkeypatch.setattr(cm_mod.os, "replace", _checked_replace)
        cm_mod.ConfigManager()

        assert observed_modes == [0o600]
        assert cm_mod.CONFIG_FILE.stat().st_mode & 0o777 == 0o600
