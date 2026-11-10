"""安全与正确性修复的回归测试。

覆盖：/system/info 端口改写、伪造 XFF 绕过内网门禁、管理面板令牌门禁、
WebDAV 档案不再携带解密密钥、配置导出不再外带明文令牌。
"""
import asyncio
import json
import zipfile
from types import SimpleNamespace
from unittest.mock import patch

import httpx

from fastapi.testclient import TestClient


class TestSystemInfoRewrite:
    def test_system_info_returns_200_when_upstream_ok(self):
        """曾因 tuple | frozenset 抛 TypeError，导致 /system/info 稳定 500"""

        async def run():
            from proxy_app import create_app

            app = create_app(emby_host="http://emby.test")
            lifespan = app.router.lifespan_context(app)
            await lifespan.__aenter__()

            def upstream(_request):
                return httpx.Response(
                    200,
                    json={
                        "WebSocketPortNumber": 8096,
                        "HttpServerPortNumber": 8096,
                        "LocalAddresses": ["http://192.168.2.100:8096"],
                    },
                )

            await app.state.http_client_follow.aclose()
            app.state.http_client_follow = httpx.AsyncClient(
                transport=httpx.MockTransport(upstream)
            )
            client = httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://proxy.test"
            )
            try:
                async with client:
                    return await client.get("/system/info")
            finally:
                await lifespan.__aexit__(None, None, None)

        resp = asyncio.run(run())
        assert resp.status_code == 200, resp.text
        assert "WebSocketPortNumber" in resp.json()


class TestPrivateSource:
    @staticmethod
    def _request(peer, xff=""):
        return SimpleNamespace(
            client=SimpleNamespace(host=peer) if peer else None,
            headers={"x-forwarded-for": xff} if xff else {},
        )

    def test_forged_xff_cannot_pass_gate(self):
        from proxy_app import _is_private_source

        assert _is_private_source(self._request("8.8.8.8", "192.168.1.10")) is False

    def test_real_private_peer_still_passes(self):
        from proxy_app import _is_private_source

        assert _is_private_source(self._request("192.168.2.50", "203.0.113.9")) is True

    def test_missing_client_is_rejected(self):
        from proxy_app import _is_private_source

        assert _is_private_source(self._request(None)) is False


class TestAdminTokenGate:
    def test_missing_or_wrong_token_rejected(self):
        import main

        app = main.create_admin_app()
        with patch.object(main.config_manager, "get", return_value={"admin_token": "s3cr3t"}):
            with TestClient(app) as client:
                assert client.get("/api/status").status_code == 401
                assert client.get(
                    "/api/status", headers={"X-Admin-Token": "wrong"}
                ).status_code == 401
                ok = client.get("/api/status", headers={"X-Admin-Token": "s3cr3t"})
                assert ok.status_code != 401

    def test_empty_token_is_not_open_to_non_loopback(self):
        import main

        app = main.create_admin_app()
        with patch.object(main.config_manager, "get", return_value={"admin_token": ""}):
            with TestClient(app) as client:
                assert client.get("/api/status").status_code == 401

    def test_docs_and_openapi_are_disabled(self):
        import main

        app = main.create_admin_app()
        with patch.object(main.config_manager, "get", return_value={"admin_token": "s3cr3t"}):
            with TestClient(app) as client:
                assert client.get("/docs").status_code == 404
                assert client.get("/openapi.json").status_code == 404
                assert client.get("/redoc").status_code == 404


class TestBackupArchiveContents:
    def test_archive_excludes_key_and_local_snapshots(self, tmp_path, monkeypatch):
        import webdav_backup as wb

        cfg = tmp_path / "config.json"
        cfg.write_text('{"admin_token": "plain"}', encoding="utf-8")
        (tmp_path / "config.key").write_bytes(b"K" * 32)
        backups = tmp_path / "config_backups"
        backups.mkdir()
        (backups / "config-20260101-000000.json").write_text("{}", encoding="utf-8")
        (tmp_path / "checkin_state.json").write_text("{}", encoding="utf-8")
        staging = tmp_path / "staging"
        staging.mkdir()

        monkeypatch.setattr(wb, "CONFIG_FILE", cfg)
        monkeypatch.setattr(wb, "CONFIG_BACKUP_DIR", backups)
        monkeypatch.setattr(wb, "CHECKIN_STATE_FILE", tmp_path / "checkin_state.json")

        svc = wb.BackupService.__new__(wb.BackupService)
        archive = svc._create_archive(staging)
        with zipfile.ZipFile(archive) as zf:
            names = zf.namelist()
        assert "config.json" in names
        assert "config.key" not in names
        assert "checkin_state.json" in names
        assert not any(name.startswith("backups/") for name in names)
        ok, reason = svc._verify_archive(archive)
        assert ok, reason

    def test_legacy_archive_with_key_still_verifies(self, tmp_path, monkeypatch):
        import webdav_backup as wb

        cfg = tmp_path / "config.json"
        cfg.write_text("{}", encoding="utf-8")
        monkeypatch.setattr(wb, "CONFIG_FILE", cfg)
        monkeypatch.setattr(wb, "CONFIG_BACKUP_DIR", tmp_path / "config_backups")
        monkeypatch.setattr(wb, "CHECKIN_STATE_FILE", tmp_path / "checkin_state.json")

        legacy = tmp_path / "legacy.zip"
        manifest = {"app_version": "dev", "created_at": "now", "members": []}
        with zipfile.ZipFile(legacy, "w") as zf:
            for name, payload in (
                ("config.json", b"{}"),
                ("config.key", b"K" * 32),
            ):
                zf.writestr(name, payload)
                manifest["members"].append(
                    {"path": name, "size": len(payload), "sha256": wb.BackupService._sha256_bytes(payload)}
                )
            zf.writestr("manifest.json", json.dumps(manifest))

        svc = wb.BackupService.__new__(wb.BackupService)
        ok, reason = svc._verify_archive(legacy)
        assert ok, reason


class TestConfigExport:
    def test_export_strips_plaintext_admin_token(self, tmp_path, monkeypatch):
        import config_manager as cm

        cfg = tmp_path / "config.json"
        cfg.write_text(
            json.dumps({"admin_token": "topsecret", "emby": {"enabled": True}}),
            encoding="utf-8",
        )
        monkeypatch.setattr(cm, "CONFIG_FILE", cfg)
        instance = cm.ConfigManager.__new__(cm.ConfigManager)
        exported = json.loads(instance.export_disk_text())
        assert "admin_token" not in exported
        assert exported["emby"]["enabled"] is True
        assert "topsecret" in cfg.read_text(encoding="utf-8")
