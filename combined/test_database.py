"""
Database 模块测试：CRUD 操作和会话管理
"""
import tempfile
from pathlib import Path

import pytest


@pytest.fixture
def db():
    from database import Database
    _db = Database(str(Path(tempfile.mkdtemp()) / "test.db"))
    yield _db
    _db.conn.close()


class TestDatabase:

    def test_add_and_get_file(self, db):
        fid = db.add_file({
            "pickcode": "abc123", "file_name": "test.mp4", "file_size": 1024,
            "file_type": ".mp4", "pan_path": "/movies/test.mp4",
            "local_strm_path": "/strm/test.strm", "sha1": "sha1_val",
            "parent_id": "/movies",
        })
        assert fid > 0
        f = db.get_file_by_pickcode("abc123")
        assert f is not None
        assert f["file_name"] == "test.mp4"
        assert f["sha1"] == "sha1_val"

    def test_get_nonexistent_file(self, db):
        assert db.get_file_by_pickcode("nonexistent") is None

    def test_count_active_files(self, db):
        assert db.count_active_files() == 0
        db.add_file({
            "pickcode": "a1", "file_name": "a.mp4", "file_size": 100,
            "file_type": ".mp4", "pan_path": "/a.mp4",
            "local_strm_path": "/s/a.strm", "sha1": "s1", "parent_id": "/",
        })
        assert db.count_active_files() == 1

    def test_mark_file_deleted(self, db):
        db.add_file({
            "pickcode": "del1", "file_name": "del.mp4", "file_size": 100,
            "file_type": ".mp4", "pan_path": "/del.mp4",
            "local_strm_path": "/s/del.strm", "sha1": "s2", "parent_id": "/",
        })
        db.mark_file_deleted("del1")
        assert db.count_active_files() == 0
        assert db.get_file_by_pickcode("del1") is None

    def test_batch_add_files(self, db):
        items = [{
            "pickcode": f"b{i}", "file_name": f"f{i}.mp4", "file_size": 100,
            "file_type": ".mp4", "pan_path": f"/f{i}.mp4",
            "local_strm_path": f"/s/f{i}.strm", "sha1": f"sh{i}",
            "parent_id": "/",
        } for i in range(3)]
        db.batch_add_files(items)
        assert db.count_active_files() == 3

    def test_get_active_files_by_parent(self, db):
        db.batch_add_files([
            {"pickcode": "p1", "file_name": "a.mp4", "file_size": 100,
             "file_type": ".mp4", "pan_path": "/movies/a.mp4",
             "local_strm_path": "/s/a.strm", "sha1": "s1", "parent_id": "/movies"},
            {"pickcode": "p2", "file_name": "b.mp4", "file_size": 200,
             "file_type": ".mp4", "pan_path": "/tv/b.mp4",
             "local_strm_path": "/s/b.strm", "sha1": "s2", "parent_id": "/tv"},
        ])
        movies = db.get_active_files_by_parent("/movies")
        assert len(movies) == 1
        assert movies[0]["pickcode"] == "p1"

    def test_sibling_prefix_isolation(self, db):
        """兄弟前缀目录隔离：/movies 不匹配 /movies2"""
        db.batch_add_files([
            {"pickcode": "m1", "file_name": "x.mp4", "file_size": 100,
             "file_type": ".mp4", "pan_path": "/movies/x.mp4",
             "local_strm_path": "/s/x.strm", "sha1": "sx", "parent_id": "/movies"},
            {"pickcode": "m2", "file_name": "y.mp4", "file_size": 100,
             "file_type": ".mp4", "pan_path": "/movies2/y.mp4",
             "local_strm_path": "/s/y.strm", "sha1": "sy", "parent_id": "/movies2"},
            {"pickcode": "m3", "file_name": "z.mp4", "file_size": 100,
             "file_type": ".mp4", "pan_path": "/moviesbackup/z.mp4",
             "local_strm_path": "/s/z.strm", "sha1": "sz", "parent_id": "/moviesbackup"},
        ])
        res = db.get_active_files_by_parent("/movies")
        assert len(res) == 1
        assert res[0]["pickcode"] == "m1"

    def test_trailing_slash_normalized(self, db):
        """尾部斜杠规范化：/test/ 等价于 /test"""
        db.batch_add_files([
            {"pickcode": "t1", "file_name": "f.mp4", "file_size": 100,
             "file_type": ".mp4", "pan_path": "/test/f.mp4",
             "local_strm_path": "/s/f.strm", "sha1": "sf", "parent_id": "/test"},
        ])
        res1 = db.get_active_files_by_parent("/test")
        res2 = db.get_active_files_by_parent("/test/")
        assert len(res1) == 1 and len(res2) == 1
        assert res1[0]["pickcode"] == res2[0]["pickcode"] == "t1"

    def test_sync_history(self, db):
        hid = db.add_sync_history("full")
        assert hid > 0
        db.finish_sync_history(hid, 10, 2, 1, 0)
        history = db.get_sync_history(10)
        assert len(history) >= 1
        assert history[0]["sync_type"] == "full"
        assert history[0]["total_files"] == 10

    def test_clear_sync_history(self, db):
        hid = db.add_sync_history("incremental")
        db.finish_sync_history(hid, 5, 1, 0, 0)
        db.clear_sync_history()
        assert db.get_sync_history(10) == []

    def test_clear_all_files(self, db):
        db.batch_add_files([{
            "pickcode": f"c{i}", "file_name": f"f{i}.mp4", "file_size": 100,
            "file_type": ".mp4", "pan_path": f"/f{i}.mp4",
            "local_strm_path": f"/s/f{i}.strm", "sha1": f"sh{i}", "parent_id": "/",
        } for i in range(2)])
        assert db.count_active_files() == 2
        db.clear_all_files()
        assert db.count_active_files() == 0

    def test_get_stats(self, db):
        stats = db.get_stats()
        assert "total_files" in stats
        assert "total_size" in stats
