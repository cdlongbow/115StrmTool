"""
WebDAV 远端备份测试：协议归类、档案自检、上传闭环、恢复闭环与 API 收口
"""
import copy
import json
import zipfile
from urllib.parse import unquote

import httpx
import pytest


def _multistatus(rows):
    """
    构造 PROPFIND multistatus XML

    :param rows (List): (root 相对路径, size 文本或 None) 元组列表，目录路径以斜杠结尾

    :return str: XML 文本
    """
    parts = ["<?xml version='1.0'?>", "<d:multistatus xmlns:d='DAV:'>"]
    for name, size in rows:
        parts.append("<d:response><d:href>/dav/" + name + "</d:href>")
        parts.append("<d:propstat><d:prop>")
        if size is not None:
            parts.append("<d:getcontentlength>" + str(size) + "</d:getcontentlength>")
        parts.append("<d:getlastmodified>Tue, 20 Oct 2026 00:00:00 GMT</d:getlastmodified>")
        parts.append("<d:resourcetype/>")
        parts.append("</d:prop><d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response>")
    parts.append("</d:multistatus>")
    return "".join(parts)


class FakeDav:
    """内存 WebDAV 替身：文件表 + 请求记录 + 故障注入"""

    def __init__(self):
        self.base = "/dav/"
        self.files = {}
        self.dirs = set()
        self.requests = []
        self.fail = {}
        self.get_override = None
        self.propfind_text = None
        self.propfind_rows = None

    def fail_for(self, method, rel=None, code="net"):
        """
        注入故障：method 或[method, rel]级，net 表示抛连接异常

        :param method (str): HTTP method
        :param rel (str): 精确相对路径，None 表示该 method 全部
        :param code (int|str): 状态码或网络类 "net"

        :return None:
        """
        self.fail[(method, rel) if rel else method] = code

    def handler(self, request):
        """MockTransport 处理入口"""
        method = request.method
        path = request.url.path
        rel = unquote(path[len(self.base):]) if path.startswith(self.base) else path
        self.requests.append((method, rel))
        rule = self.fail.get((method, rel), self.fail.get(method))
        if rule == "net":
            raise httpx.ConnectError("mock network down")
        if isinstance(rule, int):
            return httpx.Response(rule)
        if method == "MKCOL":
            if rel in self.dirs:
                return httpx.Response(405)
            self.dirs.add(rel)
            return httpx.Response(201)
        if method == "PUT":
            self.files[rel] = request.content
            return httpx.Response(201)
        if method == "GET":
            if self.get_override is not None:
                return httpx.Response(200, content=self.get_override)
            if rel in self.files:
                return httpx.Response(200, content=self.files[rel])
            return httpx.Response(404)
        if method == "DELETE":
            self.files.pop(rel, None)
            return httpx.Response(204)
        if method == "PROPFIND":
            if self.propfind_text is not None:
                return httpx.Response(207, text=self.propfind_text)
            prefix = rel if rel.endswith("/") else rel + "/"
            if self.propfind_rows is not None:
                return httpx.Response(207, text=_multistatus(self.propfind_rows))
            rows = [(prefix, None)]
            for key in sorted(self.files):
                if key.startswith(prefix) and key != prefix:
                    rows.append((key, str(len(self.files[key]))))
            return httpx.Response(207, text=_multistatus(rows))
        return httpx.Response(405)


def _client(fake):
    """
    用替身构造 WebdavClient

    :param fake (FakeDav): 替身

    :return: WebdavClient 实例
    """
    import webdav_backup as wb

    return wb.WebdavClient(
        "http://dav.test/dav/", username="u", password="p",
        transport=httpx.MockTransport(fake.handler),
    )


@pytest.fixture()
def dav_env(monkeypatch, tmp_path):
    """隔离配置与快照目录到 tmp，备份服务注入内存 WebDAV"""
    import config_manager as cm
    import webdav_backup as wb

    conf = tmp_path / "config.json"
    conf.write_text("{}", encoding="utf-8")
    (tmp_path / "config.key").write_bytes(b"k" * 32)
    backup_dir = tmp_path / "config_backups"
    monkeypatch.setattr(cm, "CONFIG_FILE", conf)
    monkeypatch.setattr(cm, "CONFIG_BACKUP_DIR", backup_dir)
    monkeypatch.setattr(cm, "_BASE_DIR", tmp_path)
    monkeypatch.setattr(wb, "CONFIG_FILE", conf)
    monkeypatch.setattr(wb, "CONFIG_BACKUP_DIR", backup_dir)
    monkeypatch.setattr(wb, "_BASE_DIR", tmp_path)
    monkeypatch.setattr(wb, "CHECKIN_STATE_FILE", tmp_path / "checkin_state.json")
    orig = copy.deepcopy(cm.config_manager._config)
    cm.config_manager._config = copy.deepcopy(cm.DEFAULT_CONFIG)
    orig_last = wb.backup_service._last_result
    wb.backup_service._last_result = {}
    yield wb, conf, backup_dir, tmp_path
    cm.config_manager._config = orig
    wb.backup_service._last_result = orig_last


class TestWebdavClient:
    def _names(self, items):
        return [one["name"] for one in items]

    def test_connection_ok_and_probe_cleanup(self):
        import webdav_backup as wb
        fake = FakeDav()
        client = _client(fake)
        ok, msg = client.test_connection("bk")
        client.close()
        assert ok is True
        assert "可读写" in msg
        assert ("MKCOL", "bk/") in fake.requests
        assert ("PROPFIND", "bk/") in fake.requests
        probe_keys = [k for k in fake.files if wb._PROBE_NAME in k]
        assert probe_keys == []
        deleted = [rel for method, rel in fake.requests if method == "DELETE"]
        assert any(wb._PROBE_NAME in rel for rel in deleted)

    def test_connection_network_error(self):
        fake = FakeDav()
        fake.fail_for("MKCOL")
        client = _client(fake)
        ok, msg_network = client.test_connection("bk")
        client.close()
        assert ok is False
        assert "不可达" in msg_network

    def test_connection_auth_failed(self):
        fake = FakeDav()
        fake.fail_for("PROPFIND", code=401)
        client = _client(fake)
        ok, msg = client.test_connection("bk")
        client.close()
        assert ok is False
        assert "认证失败" in msg

    def test_connection_write_forbidden(self):
        fake = FakeDav()
        fake.fail_for("PUT", code=403)
        client = _client(fake)
        ok, msg = client.test_connection("bk")
        client.close()
        assert ok is False
        assert "权限" in msg

    def test_propfind_filters_and_sorts(self):
        fake = FakeDav()
        fake.propfind_text = ""
        rows = [
            ("bk/", None),
            ("bk/readme.txt", "9"),
            ("bk/115strmtool-backup-20261018-000002.zip", "12"),
            ("bk/115strmtool-backup-20261018-000001.zip", "3"),
        ]
        fake.propfind_text = None
        fake.propfind_rows = rows
        client = _client(fake)
        out = client.list_archives("bk")
        client.close()
        assert self._names(out) == [
            "115strmtool-backup-20261018-000001.zip",
            "115strmtool-backup-20261018-000002.zip",
        ]
        assert out[0]["size"] == 3

    def test_propfind_lenient_size_and_modified(self):
        fake = FakeDav()
        fake.propfind_rows = [
            ("bk/115strmtool-backup-20261018-000000.zip", None),
            ("bk/115strmtool-backup-20261018-000001.zip", "1.9"),
            ("bk/115strmtool-backup-20261018-000002.zip", "abc"),
        ]
        client = _client(fake)
        out = client.list_archives("bk")
        client.close()
        assert [one["size"] for one in out] == [0, 1, 0]
        assert all(isinstance(one["modified"], str) for one in out)

    def test_propfind_bad_xml(self):
        from exceptions import ServiceError
        fake = FakeDav()
        fake.propfind_text = "<html>proxy error</html>"
        client = _client(fake)
        with pytest.raises(ServiceError):
            client.list_archives("bk")
        client.close()

    def test_put_get_delete_roundtrip_and_404(self):
        from exceptions import NotFoundError
        fake = FakeDav()
        client = _client(fake)
        client.put("bk/a.bin", b"data-1")
        assert fake.files["bk/a.bin"] == b"data-1"
        assert client.get("bk/a.bin") == b"data-1"
        client.delete("bk/a.bin")
        assert "bk/a.bin" not in fake.files
        with pytest.raises(NotFoundError):
            client.get("bk/missing.bin")
        client.close()


def _sha(data):
    import hashlib
    return hashlib.sha256(data).hexdigest()


def _build_zip(members, manifest_members):
    """
    手工组一个含清单的档案字节

    :param members (Dict): arcname 到内容 bytes 的映射
    :param manifest_members (List): manifest.json 的 members 宣告内容

    :return bytes: zip 字节
    """
    import io
    import json

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, data in members.items():
            zf.writestr(name, data)
        zf.writestr(
            "manifest.json",
            json.dumps({
                "app_version": "test",
                "created_at": "2026-10-18T00:00:00",
                "members": manifest_members,
            }),
        )
    return buf.getvalue()


def _good_members():
    cfg = b'{"p115": {"cookie": "orig"}}'
    key = b"k" * 32
    return {"config.json": cfg, "config.key": key}, [
        {"path": "config.json", "size": len(cfg), "sha256": _sha(cfg)},
        {"path": "config.key", "size": len(key), "sha256": _sha(key)},
    ]


class TestArchiveSelfCheck:
    def test_create_roundtrip_and_manifest(self, dav_env):
        wb, conf, backup_dir, tmp = dav_env
        backup_dir.mkdir(parents=True, exist_ok=True)
        (backup_dir / "config-20261010-000000.json").write_bytes(b"{}")
        (tmp / "checkin_state.json").write_bytes(b'{"last_done": "2026-10-17"}')
        staging = tmp / "staging"
        staging.mkdir()
        archive = wb.backup_service._create_archive(staging)
        assert archive.name.startswith("115strmtool-backup-")
        ok, reason = wb.backup_service._verify_archive(archive)
        assert ok is True, reason
        with zipfile.ZipFile(archive) as zf:
            manifest = json.loads(zf.read("manifest.json"))
        declared = [m["path"] for m in manifest["members"]]
        assert declared == sorted(declared)
        assert "checkin_state.json" in declared
        assert any(one.startswith("backups/") for one in declared)
        assert manifest["app_version"]

    def test_create_requires_config_key(self, dav_env):
        wb, conf, backup_dir, tmp = dav_env
        from exceptions import ServiceError
        (tmp / "config.key").unlink()
        staging = tmp / "staging"
        staging.mkdir()
        with pytest.raises(ServiceError):
            wb.backup_service._create_archive(staging)

    def test_create_skips_missing_checkin(self, dav_env):
        wb, conf, backup_dir, tmp = dav_env
        staging = tmp / "staging"
        staging.mkdir()
        archive = wb.backup_service._create_archive(staging)
        with zipfile.ZipFile(archive) as zf:
            assert "checkin_state.json" not in zf.namelist()

    def test_verify_detects_extra_member(self, tmp_path):
        import webdav_backup as wb_mod
        members, declared = _good_members()
        members["extra.bin"] = b"junk"
        blob = _build_zip(members, declared)
        path = tmp_path / "115strmtool-backup-20261018-000000.zip"
        path.write_bytes(blob)
        ok, reason = wb_mod.backup_service._verify_archive(path)
        assert ok is False and "清单外" in reason

    def test_verify_detects_size_mismatch(self, tmp_path):
        import webdav_backup as wb_mod
        members, declared = _good_members()
        declared[0]["size"] = declared[0]["size"] + 1
        path = tmp_path / "115strmtool-backup-20261018-000001.zip"
        path.write_bytes(_build_zip(members, declared))
        ok, reason = wb_mod.backup_service._verify_archive(path)
        assert ok is False and "大小不符" in reason

    def test_verify_detects_sha_mismatch(self, tmp_path):
        import webdav_backup as wb_mod
        members, declared = _good_members()
        declared[1]["sha256"] = "0" * 64
        path = tmp_path / "115strmtool-backup-20261018-000002.zip"
        path.write_bytes(_build_zip(members, declared))
        ok, reason = wb_mod.backup_service._verify_archive(path)
        assert ok is False and "摘要不符" in reason

    def test_verify_detects_missing_required(self, tmp_path):
        import webdav_backup as wb_mod
        members, declared = _good_members()
        del members["config.key"]
        declared = [declared[0]]
        path = tmp_path / "115strmtool-backup-20261018-000003.zip"
        path.write_bytes(_build_zip(members, declared))
        ok, reason = wb_mod.backup_service._verify_archive(path)
        assert ok is False and "必备成员" in reason

    def test_verify_rejects_traversal_entry(self, tmp_path):
        import webdav_backup as wb_mod
        cfg = b"x"
        members = {
            "config.json": cfg,
            "config.key": b"k" * 32,
            "../evil.txt": b"bad",
        }
        declared = [
            {"path": "config.json", "size": 1, "sha256": _sha(cfg)},
            {"path": "config.key", "size": 32, "sha256": _sha(members["config.key"])},
            {"path": "../evil.txt", "size": 3, "sha256": _sha(b"bad")},
        ]
        path = tmp_path / "115strmtool-backup-20261018-000004.zip"
        path.write_bytes(_build_zip(members, declared))
        ok, reason = wb_mod.backup_service._verify_archive(path)
        assert ok is False and "危险路径" in reason

    def test_verify_rejects_broken_zip(self, tmp_path):
        import webdav_backup as wb_mod
        path = tmp_path / "115strmtool-backup-20261018-000005.zip"
        path.write_bytes(b"PK garbage truncated")
        ok, reason = wb_mod.backup_service._verify_archive(path)
        assert ok is False and reason


@pytest.fixture()
def wired(monkeypatch, dav_env):
    """备份服务接到内存 FakeDav 上，返回 (wb, conf, backup_dir, tmp, fake, monkeypatch)"""
    wb, conf, backup_dir, tmp = dav_env
    fake = FakeDav()
    monkeypatch.setattr(wb.backup_service, "_transport", httpx.MockTransport(fake.handler))
    from config_manager import config_manager
    config_manager.update({
        "webdav": {
            "enabled": True,
            "url": "http://dav.test/dav/",
            "username": "u",
            "password": "p",
            "remote_subdir": "bk",
            "keep_count": 10,
        },
        "p115": {"cookie": "BACKUP-ORIG"},
    })
    return wb, conf, backup_dir, tmp, fake, monkeypatch


def _seed_old(fake, stamps):
    """预置旧档案"""
    for stamp in stamps:
        fake.files[f"bk/115strmtool-backup-{stamp}.zip"] = b"old-" + stamp.encode()


class TestBackupRestoreFlow:
    def test_run_success_list_and_prune(self, wired):
        wb, conf, backup_dir, tmp, fake, _ = wired
        from config_manager import config_manager
        config_manager.update({"webdav": {"keep_count": 2}})
        _seed_old(fake, ["19700101-000001", "19700101-000002", "19700101-000003"])
        result = wb.backup_service.run_backup()
        assert result["ok"] is True
        assert result["name"].startswith("115strmtool-backup-")
        assert result["pruned"] == 2
        names = [rel for rel in fake.files if rel.endswith(".zip")]
        assert any("19700101-000003" in n for n in names)
        assert not any("19700101-000001" in n for n in names)
        archives = wb.backup_service.list_remote()
        assert [one["name"] for one in archives][-1] == result["name"]

    def test_selfcheck_failure_blocks_upload(self, wired):
        wb, conf, backup_dir, tmp, fake, monkeypatch = wired
        from exceptions import ServiceError

        def broken_create(self, staging):
            bad = staging / "broken.zip"
            bad.write_bytes(b"not a zip")
            return bad

        monkeypatch.setattr(wb.BackupService, "_create_archive", broken_create)
        with pytest.raises(ServiceError) as exc:
            wb.backup_service.run_backup()
        assert "自检" in str(exc.value)
        assert not any(m == "PUT" for m, _ in fake.requests)

    def test_upload_size_mismatch_keeps_remote_intact(self, wired):
        wb, conf, backup_dir, tmp, fake, _ = wired
        from exceptions import ServiceError
        _seed_old(fake, ["19700101-000001"])
        fake.get_override = b"short-mismatch"
        with pytest.raises(ServiceError) as exc:
            wb.backup_service.run_backup()
        assert "不一致" in str(exc.value)
        assert not any(m == "DELETE" for m, _ in fake.requests)
        assert any("19700101-000001" in rel for rel in fake.files)

    def test_busy_lock_rejects_second_run(self, wired):
        wb, conf, backup_dir, tmp, fake, _ = wired
        from exceptions import ServiceError
        wb.backup_service._task_lock.acquire()
        try:
            with pytest.raises(ServiceError) as exc:
                wb.backup_service.run_backup()
            assert "进行中" in str(exc.value)
        finally:
            wb.backup_service._task_lock.release()
        assert fake.requests == []

    def test_run_rejected_when_disabled(self, wired):
        wb, conf, backup_dir, tmp, fake, _ = wired
        from config_manager import config_manager
        from exceptions import ServiceError
        config_manager.update({"webdav": {"enabled": False}})
        with pytest.raises(ServiceError):
            wb.backup_service.run_backup()
        with pytest.raises(ServiceError):
            wb.backup_service.list_remote()
        assert fake.requests == []

    def test_prune_failure_is_soft(self, wired):
        wb, conf, backup_dir, tmp, fake, _ = wired
        from config_manager import config_manager
        config_manager.update({"webdav": {"keep_count": 1}})
        _seed_old(fake, ["19700101-000001", "19700101-000002"])
        fake.fail_for("DELETE", code=500)
        result = wb.backup_service.run_backup()
        assert result["ok"] is True
        assert result["pruned"] == 0
        assert len([k for k in fake.files if k.endswith(".zip")]) == 3

    def test_resolve_remote_name_appends_suffix(self, dav_env):
        wb, conf, backup_dir, tmp = dav_env
        fake = FakeDav()
        stem = "bk/115strmtool-backup-20261018-100000"
        fake.files[stem + ".zip"] = b"a"
        fake.files[stem + "-2.zip"] = b"b"
        client = _client(fake)
        out = wb.backup_service._resolve_remote_name(
            client, "bk", "115strmtool-backup-20261018-100000.zip",
        )
        client.close()
        assert out == "115strmtool-backup-20261018-100000-3.zip"

    def test_restore_roundtrip_hot_reload(self, wired):
        wb, conf, backup_dir, tmp, fake, _ = wired
        from config_manager import config_manager
        result = wb.backup_service.run_backup()
        assert result["ok"] is True
        config_manager.update({"p115": {"cookie": "AFTER-RUINED"}})
        assert config_manager.get()["p115"]["cookie"] == "AFTER-RUINED"
        out = wb.backup_service.restore_backup(result["name"])
        assert out["ok"] is True
        restored_cookie = config_manager.get()["p115"]["cookie"]
        assert restored_cookie == "BACKUP-ORIG"
        guards = list(backup_dir.glob("restore-guard-*.json"))
        assert len(guards) == 1
        assert (backup_dir / (guards[0].stem + ".key")).exists()

    def test_restore_verify_failure_keeps_current(self, wired):
        wb, conf, backup_dir, tmp, fake, _ = wired
        from exceptions import ServiceError
        fake.files["bk/115strmtool-backup-20261018-010000.zip"] = b"garbage-not-archive"
        before = conf.read_bytes()
        with pytest.raises(ServiceError) as exc:
            wb.backup_service.restore_backup("115strmtool-backup-20261018-010000.zip")
        assert "校验失败" in str(exc.value)
        assert conf.read_bytes() == before
        assert list(backup_dir.glob("restore-guard-*")) == []

    def test_restore_rejects_illegal_names(self, wired):
        wb, conf, backup_dir, tmp, fake, _ = wired
        from exceptions import ServiceError
        for bad_name in ["..%2f..%2fetc.zip", "evil.exe", "115strmtool-backup-9.zip"]:
            with pytest.raises(ServiceError):
                wb.backup_service.restore_backup(bad_name)
            with pytest.raises(ServiceError):
                wb.backup_service.download_remote(bad_name)
        assert fake.requests == []

    def test_restore_write_failure_rolls_back(self, wired):
        wb, conf, backup_dir, tmp, fake, monkeypatch = wired
        from config_manager import config_manager
        from exceptions import ServiceError
        result = wb.backup_service.run_backup()
        config_manager.update({"p115": {"cookie": "OTHER-VALUE"}})
        before = conf.read_bytes()
        original = wb.BackupService._atomic_write_bytes
        counters = {"n": 0}

        def flaky_write(path, data):
            counters["n"] += 1
            if counters["n"] == 2:
                raise OSError("simulated disk failure")
            original(path, data)

        monkeypatch.setattr(wb.BackupService, "_atomic_write_bytes", staticmethod(flaky_write))
        with pytest.raises(ServiceError) as exc:
            wb.backup_service.restore_backup(result["name"])
        assert "还原" in str(exc.value)
        assert conf.read_bytes() == before
        assert config_manager.get()["p115"]["cookie"] == "OTHER-VALUE"


class TestBackupEndpoints:
    def test_get_config_masks_password(self, dav_env):
        wb, conf, backup_dir, tmp = dav_env
        import admin_api
        from config_manager import COOKIE_MASK, config_manager
        config_manager.update({"webdav": {"enabled": True, "url": "http://x", "password": "real-pw"}})
        out = admin_api.get_backup_webdav_config()
        assert out["password"] == COOKIE_MASK
        assert out["url"] == "http://x"
        assert out["enabled"] is True

    def test_save_config_keeps_masked_password(self, dav_env):
        wb, conf, backup_dir, tmp = dav_env
        import admin_api
        from config_manager import COOKIE_MASK, config_manager
        config_manager.update({"webdav": {"password": "real-pw", "url": ""}})
        req = admin_api.WebdavBackupConfigRequest(password=COOKIE_MASK, url="new-url")
        admin_api.save_backup_webdav_config(req)
        assert config_manager.get()["webdav"]["password"] == "real-pw"
        disk = conf.read_text(encoding="utf-8")
        assert "real-pw" not in disk

    def test_test_endpoint_rejects_disabled(self, dav_env):
        wb, conf, backup_dir, tmp = dav_env
        import admin_api
        from exceptions import ServiceError
        with pytest.raises(ServiceError):
            admin_api.test_backup_webdav()

    def test_status_endpoint_shape(self, dav_env):
        wb, conf, backup_dir, tmp = dav_env
        import admin_api
        out = admin_api.get_backup_status()
        assert set(out) == {"enabled", "last"}
        assert out["enabled"] is False
        assert out["last"] == {}

    def test_restore_endpoint_triggers_restart_callbacks(self, dav_env, monkeypatch):
        wb_mod, conf, backup_dir, tmp = dav_env
        import admin_api
        import webdav_backup as wb
        calls = []
        name = "115strmtool-backup-20261018-000000.zip"

        class StubService:
            def restore_backup(self, archive_name):
                calls.append(archive_name)
                return {"ok": True, "name": archive_name}

        monkeypatch.setattr(wb, "backup_service", StubService())
        admin_api.set_p115_restart_callback(lambda: calls.append("p115"))
        admin_api.set_emby_restart_callback(lambda: calls.append("emby"))
        try:
            out = admin_api.restore_from_backup(admin_api.RestoreRequest(name=name))
        finally:
            admin_api.set_p115_restart_callback(None)
            admin_api.set_emby_restart_callback(None)
        assert out["ok"] is True
        assert calls == [name, "p115", "emby"]
