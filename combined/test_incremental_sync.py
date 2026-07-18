"""
增量同步模拟测试：覆盖所有逻辑分支
"""
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock

# mock 所有外部依赖
for m in ["p115cipher", "p115client", "httpx"]:
    sys.modules[m] = MagicMock()
fake = MagicMock()
fake.P115ClientWrapper = MagicMock
sys.modules["p115_client_wrapper"] = fake
for m in ["app_ver", "config_manager", "logger"]:
    sys.modules[m] = MagicMock()
sys.modules["logger"].logger = MagicMock()

import importlib.util

spec = importlib.util.spec_from_file_location("database", "database.py")
db_mod = importlib.util.module_from_spec(spec)
sys.modules["database"] = db_mod
spec.loader.exec_module(db_mod)

spec2 = importlib.util.spec_from_file_location("strm_generator", "strm_generator.py")
sg_mod = importlib.util.module_from_spec(spec2)
sys.modules["strm_generator"] = sg_mod
spec2.loader.exec_module(sg_mod)

tmp_dir = Path(tempfile.mkdtemp())
_orig_db = db_mod.db
db_mod.db = db_mod.Database(str(tmp_dir / "test.db"))
sg_mod.db = db_mod.db

passed = 0
failed = 0


def reset_db():
    db_mod.db.conn.execute("DELETE FROM files")
    db_mod.db.conn.commit()


def make_gen():
    gen = sg_mod.StrmGenerator(MagicMock(), "http://127.0.0.1:8100")
    gen.set_config(rmt_mediaext="mp4,mkv")
    gen.set_use_rust(False)
    gen._resolve_pan_path = MagicMock(return_value=123)
    return gen


def set_iter_files(items):
    """替换模块级 _iter_files_115"""
    def _fake(*a, **kw):
        for item in items:
            yield item
    sg_mod._iter_files_115 = _fake


def run(name, fn):
    global passed, failed
    reset_db()
    try:
        fn()
        passed += 1
        print(f"[PASS] {name}")
    except AssertionError as e:
        failed += 1
        print(f"[FAIL] {name}: {e}")
    except Exception as e:
        failed += 1
        import traceback
        print(f"[FAIL] {name}: {e}")
        traceback.print_exc()


def test_first_run_fallback():
    gen = make_gen()
    gen.full_sync = MagicMock(return_value={"total": 10, "new": 10, "deleted": 0, "failed": 0, "cancelled": False})
    r = gen.incremental_sync([{"from": "/test", "to": str(tmp_dir)}])
    gen.full_sync.assert_called_once()
    assert r["total"] == 10


def test_new():
    db_mod.db.batch_add_files([{
        "pickcode": "old001", "file_name": "old.mp4", "file_size": 100,
        "file_type": ".mp4", "pan_path": "/test/old.mp4",
        "local_strm_path": str(tmp_dir / "old.strm"),
        "sha1": "sha_old", "parent_id": "/test",
    }])
    gen = make_gen()
    set_iter_files([
        {"name": "old.mp4", "is_dir": False, "pickcode": "old001",
         "pick_code": "old001", "sha1": "sha_old", "id": 1, "parent_id": 0, "size": 100},
        {"name": "new.mkv", "is_dir": False, "pickcode": "new001",
         "pick_code": "new001", "sha1": "sha_new", "id": 2, "parent_id": 0, "size": 200},
    ])
    r = gen.incremental_sync([{"from": "/test", "to": str(tmp_dir)}])
    assert r["new"] == 1, f"r={r}"
    assert r["unchanged"] == 1
    assert r["changed"] == 0
    assert r["deleted"] == 0
    assert (tmp_dir / "new.strm").exists()


def test_changed():
    sp = tmp_dir / "movie.strm"
    sp.write_text("old_content", encoding="utf-8")
    db_mod.db.batch_add_files([{
        "pickcode": "pc001", "file_name": "movie.mkv", "file_size": 100,
        "file_type": ".mkv", "pan_path": "/test/movie.mkv",
        "local_strm_path": str(sp), "sha1": "v1", "parent_id": "/test",
    }])
    gen = make_gen()
    set_iter_files([
        {"name": "movie.mkv", "is_dir": False, "pickcode": "pc001",
         "pick_code": "pc001", "sha1": "v2", "id": 1, "parent_id": 0, "size": 100},
    ])
    r = gen.incremental_sync([{"from": "/test", "to": str(tmp_dir)}])
    assert r["changed"] == 1, f"r={r}"
    assert r["new"] == 0
    assert sp.read_text(encoding="utf-8") != "old_content"


def test_unchanged():
    sp = tmp_dir / "keep.strm"
    sp.write_text("keep", encoding="utf-8")
    db_mod.db.batch_add_files([{
        "pickcode": "pc002", "file_name": "keep.mp4", "file_size": 100,
        "file_type": ".mp4", "pan_path": "/test/keep.mp4",
        "local_strm_path": str(sp), "sha1": "sha_keep", "parent_id": "/test",
    }])
    gen = make_gen()
    set_iter_files([
        {"name": "keep.mp4", "is_dir": False, "pickcode": "pc002",
         "pick_code": "pc002", "sha1": "sha_keep", "id": 1, "parent_id": 0, "size": 100},
    ])
    r = gen.incremental_sync([{"from": "/test", "to": str(tmp_dir)}])
    assert r["unchanged"] == 1, f"r={r}"
    assert sp.read_text(encoding="utf-8") == "keep"


def test_deleted():
    sp = tmp_dir / "dead.strm"
    sp.write_text("dead", encoding="utf-8")
    db_mod.db.batch_add_files([{
        "pickcode": "dead001", "file_name": "dead.mp4", "file_size": 100,
        "file_type": ".mp4", "pan_path": "/test/dead.mp4",
        "local_strm_path": str(sp), "sha1": "sha_dead", "parent_id": "/test",
    }])
    gen = make_gen()
    set_iter_files([])
    r = gen.incremental_sync([{"from": "/test", "to": str(tmp_dir)}])
    assert r["deleted"] == 1, f"r={r}"
    assert not sp.exists()
    assert db_mod.db.get_file_by_pickcode("dead001") is None


def test_cancel():
    """取消标志在 sync 启动后由另一线程触发，reset_cancel 会清除。此测试验证 cancel 最终透传到返回值"""
    db_mod.db.batch_add_files([{
        "pickcode": "pc003", "file_name": "a.mp4", "file_size": 100,
        "file_type": ".mp4", "pan_path": "/test/a.mp4",
        "local_strm_path": str(tmp_dir / "a.strm"),
        "sha1": "sha_a", "parent_id": "/test",
    }])
    gen = make_gen()
    # 模拟：遍历过程中取消（直接在 _iter_files_115 替换中设置 cancel）
    def cancel_during_iter(*a, **kw):
        gen.cancel()
        yield {"name": "a.mp4", "is_dir": False, "pickcode": "pc003",
               "pick_code": "pc003", "sha1": "sha_a", "id": 1, "parent_id": 0, "size": 100}
    sg_mod._iter_files_115 = cancel_during_iter
    r = gen.incremental_sync([{"from": "/test", "to": str(tmp_dir)}])
    assert r["cancelled"] is True


def test_disabled():
    db_mod.db.batch_add_files([{
        "pickcode": "pc004", "file_name": "b.mp4", "file_size": 100,
        "file_type": ".mp4", "pan_path": "/test/b.mp4",
        "local_strm_path": str(tmp_dir / "b.strm"),
        "sha1": "sha_b", "parent_id": "/test",
    }])
    gen = make_gen()
    set_iter_files([])
    r = gen.incremental_sync([{"from": "/test", "to": str(tmp_dir), "enabled": False}])
    assert r["total"] == 0
    assert r["deleted"] == 0


def test_mixed():
    entries = [
        ("keep", "sha1", "keep.mp4"),
        ("change", "old_sha", "change.mkv"),
        ("delete", "sha_del", "delete.mp4"),
    ]
    for pc, sha, name in entries:
        p = tmp_dir / f"{pc}.strm"
        p.write_text("x", encoding="utf-8")
        db_mod.db.batch_add_files([{
            "pickcode": pc, "file_name": name, "file_size": 100,
            "file_type": Path(name).suffix, "pan_path": f"/test/{name}",
            "local_strm_path": str(p), "sha1": sha, "parent_id": "/test",
        }])
    gen = make_gen()
    set_iter_files([
        {"name": "keep.mp4", "is_dir": False, "pickcode": "keep",
         "pick_code": "keep", "sha1": "sha1", "id": 1, "parent_id": 0, "size": 100},
        {"name": "change.mkv", "is_dir": False, "pickcode": "change",
         "pick_code": "change", "sha1": "new_sha", "id": 2, "parent_id": 0, "size": 100},
        {"name": "newfile.mp4", "is_dir": False, "pickcode": "new001",
         "pick_code": "new001", "sha1": "sha_new", "id": 3, "parent_id": 0, "size": 100},
    ])
    r = gen.incremental_sync([{"from": "/test", "to": str(tmp_dir)}])
    assert r["new"] == 1, f"r={r}"
    assert r["changed"] == 1
    assert r["unchanged"] == 1
    assert r["deleted"] == 1
    assert r["total"] == 4
    assert not (tmp_dir / "delete.strm").exists()


for name, fn in [
    ("首次运行降级全量", test_first_run_fallback),
    ("新增文件检测", test_new),
    ("变更强制重写", test_changed),
    ("未变跳过", test_unchanged),
    ("删除清理", test_deleted),
    ("中途取消", test_cancel),
    ("禁用路径跳过", test_disabled),
    ("混合场景", test_mixed),
]:
    run(name, fn)

print(f"\n{'='*40}")
print(f"结果: {passed} passed, {failed} failed, {passed + failed} total")
