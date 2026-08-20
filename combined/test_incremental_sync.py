"""
增量同步模拟测试：覆盖所有逻辑分支
"""
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

tmp_dir = Path(tempfile.mkdtemp())


def _build_fake_iter_files(items):
    """构建 _iter_files_115 替换函数"""
    def _fake(*a, **kw):
        for item in items:
            yield item
    return _fake


@pytest.fixture
def db():
    with patch.dict("sys.modules", {
        "p115cipher": MagicMock(),
        "p115client": MagicMock(),
        "p115_client_wrapper": MagicMock(),
        "httpx": MagicMock(),
        "app_ver": MagicMock(),
        "config_manager": MagicMock(),
        "logger": MagicMock(),
    }):
        from database import Database
        _db = Database(str(tmp_dir / "test.db"))
        yield _db
        _db.conn.close()


@pytest.fixture
def gen(db):
    with patch.dict("sys.modules", {
        "p115cipher": MagicMock(),
        "p115client": MagicMock(),
        "p115_client_wrapper": MagicMock(),
        "httpx": MagicMock(),
        "app_ver": MagicMock(),
        "config_manager": MagicMock(),
        "logger": MagicMock(),
        "database": MagicMock(),
    }):
        import strm_generator as sg
        sg.db = db
        _gen = sg.StrmGenerator(MagicMock(), "http://127.0.0.1:8100")
        _gen.set_config(rmt_mediaext="mp4,mkv")
        _gen.set_use_rust(False)
        _gen._resolve_pan_path = MagicMock(return_value=123)
        yield _gen


def _reset_db(db):
    db.conn.execute("DELETE FROM files")
    db.conn.execute("DELETE FROM sync_history")
    db.conn.commit()


class TestIncrementalSync:

    def test_first_run_fallback(self, gen, db):
        _reset_db(db)
        gen.full_sync = MagicMock(return_value={"total": 10, "new": 10, "deleted": 0, "failed": 0, "cancelled": False})
        r = gen.incremental_sync([{"from": "/test", "to": str(tmp_dir)}])
        gen.full_sync.assert_called_once()
        assert r["total"] == 10

    def test_new(self, gen, db):
        _reset_db(db)
        db.batch_add_files([{
            "pickcode": "old001", "file_name": "old.mp4", "file_size": 100,
            "file_type": ".mp4", "pan_path": "/test/old.mp4",
            "local_strm_path": str(tmp_dir / "old.strm"),
            "sha1": "sha_old", "parent_id": "/test",
        }])
        import strm_generator as sg
        sg._iter_files_115 = _build_fake_iter_files([
            {"name": "old.mp4", "is_dir": False, "pickcode": "old001",
             "pick_code": "old001", "sha1": "sha_old", "id": 1, "parent_id": 0, "size": 100,
             "path": "/test/old.mp4"},
            {"name": "new.mkv", "is_dir": False, "pickcode": "new001",
             "pick_code": "new001", "sha1": "sha_new", "id": 2, "parent_id": 0, "size": 200,
             "path": "/test/new.mkv"},
        ])
        r = gen.incremental_sync([{"from": "/test", "to": str(tmp_dir)}])
        assert r["new"] == 1, f"r={r}"
        assert r["unchanged"] == 1
        assert r["changed"] == 0
        assert r["deleted"] == 0
        assert (tmp_dir / "new.strm").exists()

    def test_changed(self, gen, db):
        _reset_db(db)
        sp = tmp_dir / "movie.strm"
        sp.write_text("old_content", encoding="utf-8")
        db.batch_add_files([{
            "pickcode": "pc001", "file_name": "movie.mkv", "file_size": 100,
            "file_type": ".mkv", "pan_path": "/test/movie.mkv",
            "local_strm_path": str(sp), "sha1": "v1", "parent_id": "/test",
        }])
        import strm_generator as sg
        sg._iter_files_115 = _build_fake_iter_files([
            {"name": "movie.mkv", "is_dir": False, "pickcode": "pc001",
             "pick_code": "pc001", "sha1": "v2", "id": 1, "parent_id": 0, "size": 100,
             "path": "/test/movie.mkv"},
        ])
        r = gen.incremental_sync([{"from": "/test", "to": str(tmp_dir)}])
        assert r["changed"] == 1, f"r={r}"
        assert r["new"] == 0
        assert sp.read_text(encoding="utf-8") != "old_content"

    def test_unchanged(self, gen, db):
        _reset_db(db)
        sp = tmp_dir / "keep.strm"
        sp.write_text("keep", encoding="utf-8")
        db.batch_add_files([{
            "pickcode": "pc002", "file_name": "keep.mp4", "file_size": 100,
            "file_type": ".mp4", "pan_path": "/test/keep.mp4",
            "local_strm_path": str(sp), "sha1": "sha_keep", "parent_id": "/test",
        }])
        import strm_generator as sg
        sg._iter_files_115 = _build_fake_iter_files([
            {"name": "keep.mp4", "is_dir": False, "pickcode": "pc002",
             "pick_code": "pc002", "sha1": "sha_keep", "id": 1, "parent_id": 0, "size": 100,
             "path": "/test/keep.mp4"},
        ])
        r = gen.incremental_sync([{"from": "/test", "to": str(tmp_dir)}])
        assert r["unchanged"] == 1, f"r={r}"
        assert sp.read_text(encoding="utf-8") == "keep"

    def test_moved_rename(self, gen, db):
        _reset_db(db)
        old_sp = tmp_dir / "old.strm"
        old_sp.write_text("http://127.0.0.1:8100/redirect?pickcode=move001", encoding="utf-8")
        db.batch_add_files([{
            "pickcode": "move001", "file_name": "movie.mp4", "file_size": 100,
            "file_type": ".mp4", "pan_path": "/test/old/movie.mp4",
            "local_strm_path": str(old_sp), "sha1": "sha_same", "parent_id": "/test",
        }])
        import strm_generator as sg
        sg._iter_files_115 = _build_fake_iter_files([
            {"name": "movie.mp4", "is_dir": False, "pickcode": "move001",
             "pick_code": "move001", "sha1": "sha_same", "id": 1, "parent_id": 0, "size": 100,
             "path": "/test/newdir/movie.mp4"},
        ])
        r = gen.incremental_sync([{"from": "/test", "to": str(tmp_dir)}])
        assert r["changed"] == 1, f"r={r}"
        assert r["unchanged"] == 0
        assert not old_sp.exists(), "旧 STRM 应被迁移移除"
        new_sp = tmp_dir / "newdir" / "movie.strm"
        assert new_sp.exists(), f"新 STRM 应存在: {new_sp}"
        assert new_sp.read_text(encoding="utf-8") == "http://127.0.0.1:8100/redirect?pickcode=move001"
        rec = db.get_file_by_pickcode("move001")
        assert rec is not None
        assert rec["pan_path"] == "/test/newdir/movie.mp4"
        assert rec["local_strm_path"] == str(new_sp)

    def test_renamed_file(self, gen, db):
        _reset_db(db)
        old_sp = tmp_dir / "oldname.strm"
        old_sp.write_text("http://127.0.0.1:8100/redirect?pickcode=ren001", encoding="utf-8")
        db.batch_add_files([{
            "pickcode": "ren001", "file_name": "oldname.mp4", "file_size": 100,
            "file_type": ".mp4", "pan_path": "/test/oldname.mp4",
            "local_strm_path": str(old_sp), "sha1": "sha_ren", "parent_id": "/test",
        }])
        import strm_generator as sg
        sg._iter_files_115 = _build_fake_iter_files([
            {"name": "newname.mp4", "is_dir": False, "pickcode": "ren001",
             "pick_code": "ren001", "sha1": "sha_ren", "id": 1, "parent_id": 0, "size": 100,
             "path": "/test/newname.mp4"},
        ])
        r = gen.incremental_sync([{"from": "/test", "to": str(tmp_dir)}])
        assert r["changed"] == 1, f"r={r}"
        assert not old_sp.exists(), "旧 STRM 应被迁移移除"
        new_sp = tmp_dir / "newname.strm"
        assert new_sp.exists(), f"新 STRM 应存在: {new_sp}"
        rec = db.get_file_by_pickcode("ren001")
        assert rec["file_name"] == "newname.mp4"
        assert rec["local_strm_path"] == str(new_sp)

    def test_deleted(self, gen, db):
        _reset_db(db)
        sp = tmp_dir / "dead.strm"
        sp.write_text("dead", encoding="utf-8")
        db.batch_add_files([{
            "pickcode": "dead001", "file_name": "dead.mp4", "file_size": 100,
            "file_type": ".mp4", "pan_path": "/test/dead.mp4",
            "local_strm_path": str(sp), "sha1": "sha_dead", "parent_id": "/test",
        }])
        import strm_generator as sg
        sg._iter_files_115 = _build_fake_iter_files([])
        r = gen.incremental_sync([{"from": "/test", "to": str(tmp_dir)}])
        assert r["deleted"] == 1, f"r={r}"
        assert not sp.exists()
        assert db.get_file_by_pickcode("dead001") is None

    def test_cancel(self, gen, db):
        _reset_db(db)
        db.batch_add_files([{
            "pickcode": "pc003", "file_name": "a.mp4", "file_size": 100,
            "file_type": ".mp4", "pan_path": "/test/a.mp4",
            "local_strm_path": str(tmp_dir / "a.strm"),
            "sha1": "sha_a", "parent_id": "/test",
        }])
        import strm_generator as sg
        def cancel_during_iter(*a, **kw):
            gen.cancel()
            yield {"name": "a.mp4", "is_dir": False, "pickcode": "pc003",
                   "pick_code": "pc003", "sha1": "sha_a", "id": 1, "parent_id": 0, "size": 100,
                   "path": "/test/a.mp4"}
        sg._iter_files_115 = cancel_during_iter
        r = gen.incremental_sync([{"from": "/test", "to": str(tmp_dir)}])
        assert r["cancelled"] is True

    def test_disabled(self, gen, db):
        _reset_db(db)
        db.batch_add_files([{
            "pickcode": "pc004", "file_name": "b.mp4", "file_size": 100,
            "file_type": ".mp4", "pan_path": "/test/b.mp4",
            "local_strm_path": str(tmp_dir / "b.strm"),
            "sha1": "sha_b", "parent_id": "/test",
        }])
        import strm_generator as sg
        sg._iter_files_115 = _build_fake_iter_files([])
        r = gen.incremental_sync([{"from": "/test", "to": str(tmp_dir), "enabled": False}])
        assert r["total"] == 0
        assert r["deleted"] == 0

    def test_mixed(self, gen, db):
        _reset_db(db)
        entries = [
            ("keep", "sha1", "keep.mp4"),
            ("change", "old_sha", "change.mkv"),
            ("delete", "sha_del", "delete.mp4"),
        ]
        for pc, sha, name in entries:
            p = tmp_dir / f"{pc}.strm"
            p.write_text("x", encoding="utf-8")
            db.batch_add_files([{
                "pickcode": pc, "file_name": name, "file_size": 100,
                "file_type": Path(name).suffix, "pan_path": f"/test/{name}",
                "local_strm_path": str(p), "sha1": sha, "parent_id": "/test",
            }])
        import strm_generator as sg
        sg._iter_files_115 = _build_fake_iter_files([
            {"name": "keep.mp4", "is_dir": False, "pickcode": "keep",
             "pick_code": "keep", "sha1": "sha1", "id": 1, "parent_id": 0, "size": 100,
             "path": "/test/keep.mp4"},
            {"name": "change.mkv", "is_dir": False, "pickcode": "change",
             "pick_code": "change", "sha1": "new_sha", "id": 2, "parent_id": 0, "size": 100,
             "path": "/test/change.mkv"},
            {"name": "newfile.mp4", "is_dir": False, "pickcode": "new001",
             "pick_code": "new001", "sha1": "sha_new", "id": 3, "parent_id": 0, "size": 100,
             "path": "/test/newfile.mp4"},
        ])
        r = gen.incremental_sync([{"from": "/test", "to": str(tmp_dir)}])
        assert r["new"] == 1, f"r={r}"
        assert r["changed"] == 1
        assert r["unchanged"] == 1
        assert r["deleted"] == 1
        assert r["total"] == 4
        assert not (tmp_dir / "delete.strm").exists()
