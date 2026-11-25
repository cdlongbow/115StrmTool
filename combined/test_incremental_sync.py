"""
增量同步模拟测试：覆盖所有逻辑分支
"""
import tempfile
import threading
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from conftest import DB_HEAVY_DEPS, stub_modules

tmp_dir = Path(tempfile.mkdtemp())


def _build_fake_iter_files(items):
    """构建 _iter_files_115 替换函数"""
    def _fake(*a, **kw):
        for item in items:
            yield item
    return _fake


@pytest.fixture
def db():
    with patch.dict("sys.modules", stub_modules(DB_HEAVY_DEPS)):
        from database import Database
        _db = Database(str(tmp_dir / "test.db"))
        yield _db
        _db.conn.close()


@pytest.fixture
def gen(db):
    with patch.dict("sys.modules", {
        **stub_modules(DB_HEAVY_DEPS),
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

    def test_changed_and_moved(self, gen, db):
        _reset_db(db)
        old_sp = tmp_dir / "cm_old" / "movie.strm"
        old_sp.parent.mkdir(parents=True, exist_ok=True)
        old_sp.write_text("old_content", encoding="utf-8")
        db.batch_add_files([{
            "pickcode": "cm001", "file_name": "movie.mp4", "file_size": 100,
            "file_type": ".mp4", "pan_path": "/test/cm_old/movie.mp4",
            "local_strm_path": str(old_sp), "sha1": "v1", "parent_id": "/test",
        }])
        import strm_generator as sg
        sg._iter_files_115 = _build_fake_iter_files([
            {"name": "movie.mp4", "is_dir": False, "pickcode": "cm001",
             "pick_code": "cm001", "sha1": "v2", "id": 1, "parent_id": 0, "size": 100,
             "path": "/test/cm_new/movie.mp4"},
        ])
        r = gen.incremental_sync([{"from": "/test", "to": str(tmp_dir)}])
        assert r["changed"] == 1, f"r={r}"
        assert not old_sp.exists(), "内容变化且路径变化时旧 STRM 应被删除"
        new_sp = tmp_dir / "cm_new" / "movie.strm"
        assert new_sp.exists(), f"新 STRM 应存在: {new_sp}"
        rec = db.get_file_by_pickcode("cm001")
        assert rec["pan_path"] == "/test/cm_new/movie.mp4"
        assert rec["sha1"] == "v2"
        assert rec["local_strm_path"] == str(new_sp)

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
        gen._cleanup_deleted = True
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

    def test_deleted_requires_cleanup_flag(self, gen, db):
        """未开启残留清理时增量只提示不删除"""
        _reset_db(db)
        sp = tmp_dir / "keeporphan.strm"
        sp.write_text("keep", encoding="utf-8")
        db.batch_add_files([{
            "pickcode": "keeporphan001", "file_name": "keeporphan.mp4", "file_size": 100,
            "file_type": ".mp4", "pan_path": "/test/keeporphan.mp4",
            "local_strm_path": str(sp), "sha1": "sha_keeporphan", "parent_id": "/test",
        }])
        import strm_generator as sg
        sg._iter_files_115 = _build_fake_iter_files([])
        r = gen.incremental_sync([{"from": "/test", "to": str(tmp_dir)}])
        assert r["deleted"] == 0, f"r={r}"
        assert sp.exists(), "未开启清理时残留 STRM 应保留"
        rec = db.get_file_by_pickcode("keeporphan001")
        assert rec is not None and rec["status"] == "active"
        assert any("未开启" in n for n in gen._sync_notes), f"notes={gen._sync_notes}"

    def test_dir_rename_moves_sidecars_and_prunes_empty_dirs(self, gen, db):
        """目录级改名应整目录搬移侧车文件并清掉旧空目录"""
        _reset_db(db)
        old_dir = tmp_dir / "c1_move" / "Show A" / "Season 1"
        old_dir.mkdir(parents=True, exist_ok=True)
        old_strm = old_dir / "ep1.strm"
        old_strm.write_text("http://x/redirect?pickcode=mov001", encoding="utf-8")
        (old_dir / "ep1.zh.srt").write_text("sub", encoding="utf-8")
        (old_dir / "ep1.nfo").write_text("nfo", encoding="utf-8")
        db.batch_add_files([{
            "pickcode": "mov001", "file_name": "ep1.mkv", "file_size": 100,
            "file_type": ".mkv", "pan_path": "/test/c1_move/Show A/Season 1/ep1.mkv",
            "local_strm_path": str(old_strm), "sha1": "sha_mov", "parent_id": "/test",
        }])
        import strm_generator as sg
        sg._iter_files_115 = _build_fake_iter_files([
            {"name": "ep1.mkv", "is_dir": False, "pickcode": "mov001",
             "pick_code": "mov001", "sha1": "sha_mov", "id": 1, "parent_id": 0,
             "size": 100, "path": "/test/c1_move/Show B/Season 1/ep1.mkv"},
        ])
        r = gen.incremental_sync([{"from": "/test", "to": str(tmp_dir)}])
        assert r["changed"] == 1, f"r={r}"
        new_dir = tmp_dir / "c1_move" / "Show B" / "Season 1"
        assert (new_dir / "ep1.strm").exists()
        assert (new_dir / "ep1.zh.srt").read_text(encoding="utf-8") == "sub"
        assert (new_dir / "ep1.nfo").exists()
        assert not (tmp_dir / "c1_move" / "Show A").exists(), "旧目录应随整目录迁移消失"
        rec = db.get_file_by_pickcode("mov001")
        assert rec["local_strm_path"] == str(new_dir / "ep1.strm")

    def test_dir_rename_falls_back_when_target_exists(self, gen, db):
        """目标目录已存在时不整目录搬移，仅迁移 STRM"""
        _reset_db(db)
        old_dir = tmp_dir / "c1_conflict" / "Old" / "Season 1"
        new_dir = tmp_dir / "c1_conflict" / "New" / "Season 1"
        old_dir.mkdir(parents=True, exist_ok=True)
        new_dir.mkdir(parents=True, exist_ok=True)
        (new_dir / "keep.txt").write_text("keep", encoding="utf-8")
        old_strm = old_dir / "ep2.strm"
        old_strm.write_text("http://x/redirect?pickcode=mov002", encoding="utf-8")
        (old_dir / "ep2.zh.srt").write_text("sub2", encoding="utf-8")
        db.batch_add_files([{
            "pickcode": "mov002", "file_name": "ep2.mkv", "file_size": 100,
            "file_type": ".mkv", "pan_path": "/test/c1_conflict/Old/Season 1/ep2.mkv",
            "local_strm_path": str(old_strm), "sha1": "sha_mov2", "parent_id": "/test",
        }])
        import strm_generator as sg
        sg._iter_files_115 = _build_fake_iter_files([
            {"name": "ep2.mkv", "is_dir": False, "pickcode": "mov002",
             "pick_code": "mov002", "sha1": "sha_mov2", "id": 2, "parent_id": 0,
             "size": 100, "path": "/test/c1_conflict/New/Season 1/ep2.mkv"},
        ])
        r = gen.incremental_sync([{"from": "/test", "to": str(tmp_dir)}])
        assert r["changed"] == 1, f"r={r}"
        assert (new_dir / "ep2.strm").exists()
        assert (old_dir / "ep2.zh.srt").exists(), "目标已存在时侧车保留在旧目录"
        assert not (new_dir / "ep2.zh.srt").exists()

    def test_partial_directory_rename_does_not_move_whole_dir(self, gen, db):
        """目录内仍有未改名文件时不整目录搬移"""
        _reset_db(db)
        old_dir = tmp_dir / "c1_partial" / "Old" / "Season 1"
        old_dir.mkdir(parents=True, exist_ok=True)
        p1 = old_dir / "ep1.strm"
        p2 = old_dir / "ep2.strm"
        p1.write_text("http://x/redirect?pickcode=part001", encoding="utf-8")
        p2.write_text("http://x/redirect?pickcode=part002", encoding="utf-8")
        db.batch_add_files([
            {"pickcode": "part001", "file_name": "ep1.mkv", "file_size": 100,
             "file_type": ".mkv", "pan_path": "/test/c1_partial/Old/Season 1/ep1.mkv",
             "local_strm_path": str(p1), "sha1": "sha_p1", "parent_id": "/test"},
            {"pickcode": "part002", "file_name": "ep2.mkv", "file_size": 100,
             "file_type": ".mkv", "pan_path": "/test/c1_partial/Old/Season 1/ep2.mkv",
             "local_strm_path": str(p2), "sha1": "sha_p2", "parent_id": "/test"},
        ])
        import strm_generator as sg
        sg._iter_files_115 = _build_fake_iter_files([
            {"name": "ep1.mkv", "is_dir": False, "pickcode": "part001",
             "pick_code": "part001", "sha1": "sha_p1", "id": 1, "parent_id": 0,
             "size": 100, "path": "/test/c1_partial/New/Season 1/ep1.mkv"},
            {"name": "ep2.mkv", "is_dir": False, "pickcode": "part002",
             "pick_code": "part002", "sha1": "sha_p2", "id": 2, "parent_id": 0,
             "size": 100, "path": "/test/c1_partial/Old/Season 1/ep2.mkv"},
        ])
        r = gen.incremental_sync([{"from": "/test", "to": str(tmp_dir)}])
        assert r["changed"] == 1 and r["unchanged"] == 1, f"r={r}"
        assert (tmp_dir / "c1_partial" / "New" / "Season 1" / "ep1.strm").exists()
        assert p2.exists(), "未改名文件应留在旧目录"
        assert not p1.exists(), "改名文件应从旧目录移走"

    def test_dir_and_file_rename_removes_stale_strm(self, gen, db):
        """目录与文件名同时变化时不留下旧文件名的 STRM"""
        _reset_db(db)
        old_dir = tmp_dir / "c1_both" / "Old" / "Season 1"
        old_dir.mkdir(parents=True, exist_ok=True)
        old_strm = old_dir / "ep3.strm"
        old_strm.write_text("http://x/redirect?pickcode=both001", encoding="utf-8")
        db.batch_add_files([{
            "pickcode": "both001", "file_name": "ep3.mkv", "file_size": 100,
            "file_type": ".mkv", "pan_path": "/test/c1_both/Old/Season 1/ep3.mkv",
            "local_strm_path": str(old_strm), "sha1": "sha_both", "parent_id": "/test",
        }])
        import strm_generator as sg
        sg._iter_files_115 = _build_fake_iter_files([
            {"name": "ep3-renamed.mkv", "is_dir": False, "pickcode": "both001",
             "pick_code": "both001", "sha1": "sha_both", "id": 1, "parent_id": 0,
             "size": 100, "path": "/test/c1_both/New/Season 1/ep3-renamed.mkv"},
        ])
        r = gen.incremental_sync([{"from": "/test", "to": str(tmp_dir)}])
        assert r["changed"] == 1, f"r={r}"
        new_dir = tmp_dir / "c1_both" / "New" / "Season 1"
        assert (new_dir / "ep3-renamed.strm").exists()
        assert not (new_dir / "ep3.strm").exists(), "旧文件名 STRM 不应残留"

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
        gen._cleanup_deleted = True
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

    def test_overlapping_prefix_no_false_deletion(self, gen, db):
        """重叠前缀映射防护：第二个映射的文件不被第一个映射误判删除"""
        _reset_db(db)
        gen._cleanup_deleted = True
        sub_dir = tmp_dir / "sub_output"
        sub_dir.mkdir(exist_ok=True)
        sub_strm = sub_dir / "file.strm"
        sub_strm.write_text("http://127.0.0.1:8100/redirect?pickcode=sub001", encoding="utf-8")
        db.batch_add_files([{
            "pickcode": "sub001", "file_name": "file.mp4", "file_size": 200,
            "file_type": ".mp4", "pan_path": "/media/sub/file.mp4",
            "local_strm_path": str(sub_strm), "sha1": "sha_sub", "parent_id": "/media/sub",
        }])
        import strm_generator as sg
        call_count = [0]
        def _fake_iter_with_sequence(*a, **kw):
            call_count[0] += 1
            if call_count[0] == 1:
                return iter([])
            else:
                return iter([
                    {"name": "file.mp4", "is_dir": False, "pickcode": "sub001",
                     "pick_code": "sub001", "sha1": "sha_sub", "id": 1, "parent_id": 0, "size": 200,
                     "path": "/media/sub/file.mp4"}
                ])
        sg._iter_files_115 = _fake_iter_with_sequence
        r = gen.incremental_sync([
            {"from": "/media", "to": str(tmp_dir)},
            {"from": "/media/sub", "to": str(sub_dir)},
        ])
        assert r["deleted"] == 0, f"第二个映射的文件不应在第一个映射扫描时被误删: r={r}"
        assert sub_strm.exists(), "兄弟映射的 STRM 文件应保留"
        rec = db.get_file_by_pickcode("sub001")
        assert rec is not None and rec["status"] == "active"

    def test_scan_failure_no_false_deletion(self, gen, db):
        """根扫描失败防护：异常根目录的存量不应被当作已删除清掉"""
        _reset_db(db)
        gen._cleanup_deleted = True
        sp = tmp_dir / "guard.strm"
        sp.write_text("guard", encoding="utf-8")
        db.batch_add_files([{
            "pickcode": "guard001", "file_name": "guard.mp4", "file_size": 100,
            "file_type": ".mp4", "pan_path": "/test/guard.mp4",
            "local_strm_path": str(sp), "sha1": "sha_guard", "parent_id": "/test",
        }])
        import strm_generator as sg

        def _boom(*a, **kw):
            raise RuntimeError("scan failed")

        sg._iter_files_115 = _boom
        r = gen.incremental_sync([{"from": "/test", "to": str(tmp_dir)}])
        assert r["failed"] == 1, f"r={r}"
        assert r["deleted"] == 0, f"扫描失败的根不应触发删除: r={r}"
        assert sp.exists(), "扫描失败时残留 STRM 应保留"
        rec = db.get_file_by_pickcode("guard001")
        assert rec is not None and rec["status"] == "active"
        assert any("扫描失败" in n for n in gen._sync_notes), f"notes={gen._sync_notes}"

    def test_full_sync_scan_failure_no_false_deletion(self, gen, db):
        """全量同步根扫描失败防护：成功根的清理不应波及失败根"""
        _reset_db(db)
        gen._cleanup_deleted = True
        sp = tmp_dir / "fsguard.strm"
        sp.write_text("x", encoding="utf-8")
        db.batch_add_files([{
            "pickcode": "fsg001", "file_name": "fsguard.mp4", "file_size": 100,
            "file_type": ".mp4", "pan_path": "/broot/fsguard.mp4",
            "local_strm_path": str(sp), "sha1": "sha_fsg", "parent_id": "/broot",
        }])
        import strm_generator as sg
        calls = {"n": 0}

        def _seq(*a, **kw):
            calls["n"] += 1
            if calls["n"] == 1:
                return iter([
                    {"name": "ok.mp4", "is_dir": False, "pickcode": "ok001",
                     "pick_code": "ok001", "sha1": "sha_ok", "id": 1, "parent_id": 0,
                     "size": 100, "path": "/aroot/ok.mp4"},
                ])
            raise RuntimeError("root scan failed")

        sg._iter_files_115 = _seq
        r = gen.full_sync([
            {"from": "/aroot", "to": str(tmp_dir)},
            {"from": "/broot", "to": str(tmp_dir)},
        ])
        assert r["failed"] == 1, f"r={r}"
        assert r["deleted"] == 0, f"扫描失败的根不应误删: r={r}"
        assert sp.exists(), "失败根的残留 STRM 应保留"
        rec = db.get_file_by_pickcode("fsg001")
        assert rec is not None and rec["status"] == "active"
        assert any("扫描失败" in n for n in gen._sync_notes), f"notes={gen._sync_notes}"


class TestSyncLockAndSingleton:

    @pytest.fixture
    def sg_module(self):
        with patch.dict(
            "sys.modules",
            {**stub_modules(DB_HEAVY_DEPS), "database": MagicMock()},
        ):
            import strm_generator as sg
            sg.strm_generator = None
            yield sg
            sg.strm_generator = None

    def test_is_syncing_detects_running_sync(self, sg_module):
        sg = sg_module
        _gen = sg.StrmGenerator(MagicMock(), "http://x")
        assert not _gen.is_syncing()
        barrier = threading.Event()
        done = threading.Event()

        def _hold_lock():
            _gen._sync_lock.acquire()
            barrier.set()
            done.wait(timeout=5)
            _gen._sync_lock.release()

        t = threading.Thread(target=_hold_lock)
        t.start()
        barrier.wait(timeout=5)
        assert _gen.is_syncing()
        done.set()
        t.join(timeout=5)
        assert not _gen.is_syncing()

    def test_get_strm_generator_refreshes_refs(self, sg_module):
        sg = sg_module
        client_a = MagicMock(name="client_a")
        g1 = sg.get_strm_generator(client_a, "http://a/")
        assert g1._client is client_a
        assert g1._url_prefix == "http://a"
        client_b = MagicMock(name="client_b")
        g2 = sg.get_strm_generator(client_b, "http://b/")
        assert g2 is g1, "应返回同一单例"
        assert g2._client is client_b, "客户端引用应刷新为最新"
        assert g2._url_prefix == "http://b", "URL 前缀应刷新为最新"

    def test_get_strm_generator_keeps_refs_on_empty(self, sg_module):
        sg = sg_module
        client_a = MagicMock(name="client_a")
        g1 = sg.get_strm_generator(client_a, "http://a/")
        g2 = sg.get_strm_generator(None, "")
        assert g2 is g1
        assert g2._client is client_a, "传入 None 客户端时保留现有引用"
        assert g2._url_prefix == "http://a", "传入空前缀时保留现有前缀"


class _FakeRustResults:
    def __init__(self, count=0, failures=None):
        self.strm_results_count = count
        self.fail_results = failures or []


class _FakeFailInfo:
    def __init__(self, path, reason="mock fail"):
        self.path_in_pan = path
        self.reason = reason


class TestRustAccelerated:

    def _mock_processor(self, gen, processor):
        gen._use_rust = True
        gen._get_rust_processor = MagicMock(return_value=processor)

    def test_set_config_use_rust(self, gen):
        gen.set_config(use_rust=True)
        assert gen._use_rust is True
        gen.set_config(rmt_mediaext="mp4")
        assert gen._use_rust is True, "未显式传 use_rust 时保留原值"
        gen.set_config(use_rust=False)
        assert gen._use_rust is False

    def test_process_rust_batch_backfills_when_processor_missing(self, gen):
        sp = tmp_dir / "rust_a" / "x.strm"
        gen._use_rust = True
        gen._get_rust_processor = MagicMock(return_value=None)
        gen._process_rust_batch(
            [{"path": "/p/x.mp4", "pickcode": "pcx"}],
            {"/p/x.mp4": (sp, "pcx", False)},
        )
        assert sp.exists(), "处理器不可用时应由 Python 回填 STRM"
        assert "pickcode=pcx" in sp.read_text(encoding="utf-8")

    def test_process_rust_batch_backfills_on_exception(self, gen):
        sp = tmp_dir / "rust_b" / "y.strm"
        fake = MagicMock()
        fake.process_batch.side_effect = RuntimeError("boom")
        self._mock_processor(gen, fake)
        gen._process_rust_batch(
            [{"path": "/p/y.mp4", "pickcode": "pcy"}],
            {"/p/y.mp4": (sp, "pcy", False)},
        )
        assert sp.exists(), "批处理异常时应由 Python 回填全部 STRM"

    def test_process_rust_batch_partial_fail_backfills_only_failed(self, gen):
        ok_sp = tmp_dir / "rust_c" / "ok.strm"
        fail_sp = tmp_dir / "rust_c" / "fail.strm"
        fake = MagicMock()
        fake.process_batch.return_value = _FakeRustResults(
            count=1, failures=[_FakeFailInfo("/p/fail.mp4")]
        )
        self._mock_processor(gen, fake)
        gen._process_rust_batch(
            [
                {"path": "/p/ok.mp4", "pickcode": "pcok"},
                {"path": "/p/fail.mp4", "pickcode": "pcfail"},
            ],
            {
                "/p/ok.mp4": (ok_sp, "pcok", False),
                "/p/fail.mp4": (fail_sp, "pcfail", False),
            },
        )
        assert not ok_sp.exists(), "Rust 成功的文件不应由 Python 重写"
        assert fail_sp.exists(), "Rust 报失败的文件应由 Python 回填"

    def test_full_sync_rust_skips_python_write(self, gen, db):
        _reset_db(db)
        fake = MagicMock()
        fake.process_batch.return_value = _FakeRustResults(count=1)
        self._mock_processor(gen, fake)
        import strm_generator as sg
        sg._iter_files_115 = _build_fake_iter_files([
            {"name": "rust.mp4", "is_dir": False, "pickcode": "rst001",
             "pick_code": "rst001", "sha1": "sha_r1", "id": 1, "parent_id": 0,
             "size": 100, "path": "/test/rust.mp4"},
        ])
        r = gen.full_sync([{"from": "/test", "to": str(tmp_dir)}])
        assert r["new"] == 1, f"r={r}"
        fake.process_batch.assert_called_once()
        assert not (tmp_dir / "rust.strm").exists(), "Rust 模式下 Python 不应写 STRM"

    def test_full_sync_rust_exception_backfills(self, gen, db):
        _reset_db(db)
        fake = MagicMock()
        fake.process_batch.side_effect = RuntimeError("boom")
        self._mock_processor(gen, fake)
        import strm_generator as sg
        sg._iter_files_115 = _build_fake_iter_files([
            {"name": "fall.mp4", "is_dir": False, "pickcode": "fbk001",
             "pick_code": "fbk001", "sha1": "sha_f1", "id": 1, "parent_id": 0,
             "size": 100, "path": "/test/fall.mp4"},
        ])
        gen.full_sync([{"from": "/test", "to": str(tmp_dir)}])
        sp = tmp_dir / "fall.strm"
        assert sp.exists(), "Rust 批处理失败时应回填全部 STRM，防止 DB 与磁盘不一致"
        assert "pickcode=fbk001" in sp.read_text(encoding="utf-8")

    def test_incremental_sync_rust_exception_backfills(self, gen, db):
        _reset_db(db)
        fake = MagicMock()
        fake.process_batch.side_effect = RuntimeError("boom")
        self._mock_processor(gen, fake)
        import strm_generator as sg
        sg._iter_files_115 = _build_fake_iter_files([
            {"name": "inc_new.mkv", "is_dir": False, "pickcode": "inc001",
             "pick_code": "inc001", "sha1": "sha_i1", "id": 2, "parent_id": 0,
             "size": 200, "path": "/test/inc_new.mkv"},
        ])
        r = gen.incremental_sync([{"from": "/test", "to": str(tmp_dir)}])
        assert r["new"] == 1, f"r={r}"
        assert (tmp_dir / "inc_new.strm").exists(), "增量同步 Rust 失败时应回填 STRM"
