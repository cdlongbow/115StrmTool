"""
同步与快速扫描回归测试：遍历异常、路径安全、碰撞、迁移、清理与终态
"""
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from conftest import DB_HEAVY_DEPS, stub_modules


@pytest.fixture
def db(tmp_path):
    from database import Database

    database = Database(str(tmp_path / "test.db"))
    yield database
    database.conn.close()


@pytest.fixture
def sg_module():
    with patch.dict("sys.modules", stub_modules(DB_HEAVY_DEPS)):
        import strm_generator as sg

        yield sg


@pytest.fixture
def gen(sg_module, db):
    sg_module.db = db
    generator = sg_module.StrmGenerator(MagicMock(), "http://127.0.0.1:3333")
    generator.set_config(rmt_mediaext="mp4,mkv")
    generator.set_use_rust(False)
    generator._fast_scan = False
    generator._resolve_pan_path = MagicMock(return_value=123)
    return generator


def _reset_db(db):
    db.conn.execute("DELETE FROM files")
    db.conn.execute("DELETE FROM sync_history")
    db.conn.commit()


def _record(pickcode, name, pan_path, local_path, sha1="sha1"):
    return {
        "pickcode": pickcode,
        "file_name": name,
        "file_size": 100,
        "file_type": Path(name).suffix,
        "pan_path": pan_path,
        "local_strm_path": str(local_path),
        "sha1": sha1,
        "parent_id": str(Path(pan_path).parent),
    }


def _attr(name, pickcode, path, sha1="sha1"):
    return {
        "name": name,
        "is_dir": False,
        "pickcode": pickcode,
        "path": path,
        "sha1": sha1,
        "size": 100,
    }


def _fake_iter(items):
    def _iter(*args, **kwargs):
        yield from items

    return _iter


@pytest.mark.parametrize("mode", ["error", "timeout"])
def test_full_sync_traversal_failure_does_not_cleanup(
    sg_module, gen, db, tmp_path, monkeypatch, mode
):
    _reset_db(db)
    gen._cleanup_deleted = True
    old_path = tmp_path / "old.strm"
    old_path.write_text("keep", encoding="utf-8")
    db.batch_add_files([
        _record("old", "old.mp4", "/media/old.mp4", old_path)
    ])
    release = threading.Event()

    if mode == "error":
        def source():
            raise RuntimeError("scan failed")
            yield
    else:
        def source():
            release.wait(timeout=2)
            yield _attr("never.mp4", "never", "/media/never.mp4")

    monkeypatch.setattr(
        sg_module,
        "_iter_files_115",
        lambda *args, **kwargs: sg_module._iter_with_timeout(source(), 0.01),
    )
    try:
        result = gen.full_sync([{"from": "/media", "to": str(tmp_path)}])
    finally:
        release.set()

    assert result["failed"] == 1
    assert result["deleted"] == 0
    assert old_path.exists()
    assert db.get_file_by_pickcode("old") is not None


@pytest.mark.parametrize("state", [0, None])
def test_fast_scan_rejects_unsuccessful_state(sg_module, state):
    response = {"count": 0, "data": []}
    if state is not None:
        response["state"] = state
    wrapper = MagicMock()
    wrapper.scan_get_json.return_value = response

    with pytest.raises(sg_module.FastScanUnavailable):
        sg_module.iter_files_fast(wrapper, 100, root_pan_path="/Media")


def test_fast_scan_missing_pickcode_raises(sg_module):
    wrapper = MagicMock()
    wrapper.scan_get_json.side_effect = [
        {
            "state": True,
            "count": 2,
            "data": [
                {"cid": "200", "n": "ok.mkv", "s": "1", "pc": "ab12"},
                {"cid": "200", "n": "bad.mkv", "s": "1"},
            ],
        },
        {
            "state": True,
            "data": {
                "list": [{"fid": "200", "fn": "Movie", "pid": "100"}],
                "has_next_page": False,
            },
        },
    ]
    pickcode_mod = SimpleNamespace(
        get_stable_point=lambda pc: "0ab1",
        id_to_pickcode=lambda cid, sp, prefix: f"PC{prefix}{cid}",
    )
    with patch.dict("sys.modules", {"p115pickcode": pickcode_mod}):
        with pytest.raises(sg_module.FastScanUnavailable):
            sg_module.iter_files_fast(wrapper, 100, root_pan_path="/Media")


def test_fast_scan_rejects_changing_page_total(sg_module):
    wrapper = MagicMock()
    wrapper.scan_get_json.side_effect = [
        {
            "state": True,
            "count": 4,
            "data": [
                {"cid": "200", "n": "a.mkv", "s": "1", "pc": "ab12"},
                {"cid": "200", "n": "b.mkv", "s": "1", "pc": "cd34"},
                {"cid": "200", "n": "c.mkv", "s": "1", "pc": "ef56"},
            ],
        },
        {
            "state": True,
            "count": 1,
            "data": [{"cid": "200", "n": "d.mkv", "s": "1", "pc": "gh78"}],
        },
    ]

    with pytest.raises(sg_module.FastScanUnavailable):
        sg_module.iter_files_fast(wrapper, 100, root_pan_path="/Media")


def test_fast_scan_unavailable_falls_back_to_dfs(sg_module, gen, monkeypatch):
    expected = _attr("a.mp4", "pc1", "/media/a.mp4")
    monkeypatch.setattr(gen, "_fast_scan", True)
    monkeypatch.setattr(
        sg_module,
        "iter_files_fast",
        MagicMock(side_effect=sg_module.FastScanUnavailable("state=0")),
    )
    monkeypatch.setattr(sg_module, "_iter_files_115", _fake_iter([expected]))

    assert list(gen._iter_root_files(100, "/media")) == [expected]


@pytest.mark.parametrize(
    "pan_path",
    [
        "/media/../secret/file.mp4",
        "/media2/file.mp4",
        "media/file.mp4",
    ],
)
def test_to_local_path_rejects_unsafe_paths(gen, tmp_path, pan_path):
    assert gen._to_local_path(pan_path, "/media", str(tmp_path)) is None


def test_ensure_strm_rechecks_local_root_containment(gen, tmp_path):
    root = tmp_path / "root"
    outside = tmp_path / "outside" / "a.strm"
    gen._active_local_roots = [root]

    with pytest.raises(ValueError, match="越界"):
        gen._ensure_strm_file(outside, "pc1", force=True)
    assert not outside.exists()


@pytest.mark.parametrize(
    "names",
    [
        ("movie.mp4", "movie.mkv"),
        ("Movie.mp4", "movie.mkv"),
    ],
)
def test_full_sync_path_collision_skips_conflict(
    sg_module, gen, db, tmp_path, monkeypatch, names
):
    _reset_db(db)
    items = [
        _attr(names[0], "pc1", f"/media/{names[0]}", "sha1"),
        _attr(names[1], "pc2", f"/media/{names[1]}", "sha2"),
    ]
    monkeypatch.setattr(sg_module, "_iter_files_115", _fake_iter(items))

    result = gen.full_sync([{"from": "/media", "to": str(tmp_path)}])

    assert result["new"] == 1
    assert result["failed"] == 1
    assert db.count_active_files() == 1
    assert len(list(tmp_path.glob("*.strm"))) == 1


@pytest.mark.parametrize("overwrite_mode", ["never", "always"])
def test_migrate_target_conflict_preserves_source_and_target(
    gen, tmp_path, overwrite_mode
):
    source = tmp_path / "source.strm"
    target = tmp_path / "target.strm"
    source.write_text("source", encoding="utf-8")
    target.write_text("target", encoding="utf-8")
    gen._overwrite_mode = overwrite_mode

    assert gen._migrate_strm_file(
        {"local_strm_path": str(source), "pickcode": "pc1"}, target
    ) is False
    assert source.read_text(encoding="utf-8") == "source"
    assert target.read_text(encoding="utf-8") == "target"


def test_incremental_target_conflict_keeps_old_record(
    sg_module, gen, db, tmp_path, monkeypatch
):
    _reset_db(db)
    old_path = tmp_path / "old.strm"
    target_path = tmp_path / "new.strm"
    old_path.write_text("old", encoding="utf-8")
    target_path.write_text("target", encoding="utf-8")
    db.batch_add_files([
        _record("pc1", "old.mp4", "/media/old.mp4", old_path)
    ])
    monkeypatch.setattr(
        sg_module,
        "_iter_files_115",
        _fake_iter([_attr("new.mp4", "pc1", "/media/new.mp4")]),
    )

    result = gen.incremental_sync([{"from": "/media", "to": str(tmp_path)}])

    assert result["changed"] == 0
    assert result["failed"] == 1
    assert old_path.read_text(encoding="utf-8") == "old"
    assert target_path.read_text(encoding="utf-8") == "target"
    record = db.get_file_by_pickcode("pc1")
    assert record["local_strm_path"] == str(old_path)


def test_full_sync_empty_root_cleans_residual(sg_module, gen, db, tmp_path, monkeypatch):
    _reset_db(db)
    gen._cleanup_deleted = True
    old_path = tmp_path / "old.strm"
    old_path.write_text("old", encoding="utf-8")
    db.batch_add_files([
        _record("pc1", "old.mp4", "/media/old.mp4", old_path)
    ])
    monkeypatch.setattr(sg_module, "_iter_files_115", _fake_iter([]))

    result = gen.full_sync([{"from": "/media", "to": str(tmp_path)}])

    assert result["deleted"] == 1
    assert not old_path.exists()
    assert db.get_file_by_pickcode("pc1") is None


def test_incremental_unlink_failure_keeps_active(
    sg_module, gen, db, tmp_path, monkeypatch
):
    _reset_db(db)
    gen._cleanup_deleted = True
    old_path = tmp_path / "old.strm"
    old_path.write_text("old", encoding="utf-8")
    db.batch_add_files([
        _record("pc1", "old.mp4", "/media/old.mp4", old_path)
    ])
    monkeypatch.setattr(sg_module, "_iter_files_115", _fake_iter([]))
    original_unlink = Path.unlink

    def fail_target_unlink(path, *args, **kwargs):
        if path == old_path:
            raise PermissionError("denied")
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_target_unlink)

    result = gen.incremental_sync([{"from": "/media", "to": str(tmp_path)}])

    assert result["deleted"] == 0
    assert result["failed"] == 1
    assert old_path.exists()
    assert db.get_file_by_pickcode("pc1") is not None


def test_cancel_records_cancelled_terminal_state(sg_module, gen, db, tmp_path, monkeypatch):
    _reset_db(db)
    old_path = tmp_path / "old.strm"
    old_path.write_text("old", encoding="utf-8")
    db.batch_add_files([
        _record("pc1", "old.mp4", "/media/old.mp4", old_path)
    ])

    def cancel_during_iter(*args, **kwargs):
        gen.cancel()
        yield _attr("old.mp4", "pc1", "/media/old.mp4")

    monkeypatch.setattr(sg_module, "_iter_files_115", cancel_during_iter)

    result = gen.incremental_sync([{"from": "/media", "to": str(tmp_path)}])

    assert result["cancelled"] is True
    assert gen.get_progress()["phase"] == "cancelled"
    history = db.get_sync_history(1)
    assert history[0]["status"] == "cancelled"


@pytest.mark.parametrize("sync_type", ["full", "incremental"])
def test_initialization_failure_sets_error_and_releases_lock(
    sg_module, gen, db, tmp_path, monkeypatch, sync_type
):
    _reset_db(db)
    if sync_type == "full":
        monkeypatch.setattr(db, "add_sync_history", MagicMock(side_effect=RuntimeError("db down")))
        result = gen.full_sync([{"from": "/media", "to": str(tmp_path)}])
    else:
        monkeypatch.setattr(db, "count_active_files", MagicMock(side_effect=RuntimeError("db down")))
        result = gen.incremental_sync([{"from": "/media", "to": str(tmp_path)}])

    assert "error" in result
    assert gen.get_progress()["phase"] == "error"
    assert gen.is_syncing() is False


def test_sha_and_path_change_failure_preserves_old_strm(
    sg_module, gen, db, tmp_path, monkeypatch
):
    _reset_db(db)
    old_path = tmp_path / "old.strm"
    old_path.write_text("old", encoding="utf-8")
    db.batch_add_files([
        _record("pc1", "old.mp4", "/media/old.mp4", old_path, "sha-old")
    ])
    monkeypatch.setattr(
        sg_module,
        "_iter_files_115",
        _fake_iter([_attr("new.mp4", "pc1", "/media/new.mp4", "sha-new")]),
    )
    monkeypatch.setattr(
        Path,
        "replace",
        MagicMock(side_effect=OSError("replace failed")),
    )
    monkeypatch.setattr(
        gen,
        "_ensure_strm_file",
        MagicMock(side_effect=OSError("write failed")),
    )

    result = gen.incremental_sync([{"from": "/media", "to": str(tmp_path)}])

    assert result["changed"] == 0
    assert result["failed"] == 1
    assert old_path.read_text(encoding="utf-8") == "old"
    record = db.get_file_by_pickcode("pc1")
    assert record["local_strm_path"] == str(old_path)
    assert record["sha1"] == "sha-old"


def test_sha_and_directory_change_moves_sidecar(
    sg_module, gen, db, tmp_path, monkeypatch
):
    _reset_db(db)
    old_dir = tmp_path / "Show A"
    old_dir.mkdir()
    old_path = old_dir / "ep.strm"
    sidecar = old_dir / "ep.zh.srt"
    old_path.write_text("old", encoding="utf-8")
    sidecar.write_text("sub", encoding="utf-8")
    db.batch_add_files([
        _record("pc1", "ep.mkv", "/media/Show A/ep.mkv", old_path, "sha-old")
    ])
    monkeypatch.setattr(
        sg_module,
        "_iter_files_115",
        _fake_iter([
            _attr("ep.mkv", "pc1", "/media/Show B/ep.mkv", "sha-new")
        ]),
    )

    result = gen.incremental_sync([{"from": "/media", "to": str(tmp_path)}])

    assert result["changed"] == 1
    new_dir = tmp_path / "Show B"
    assert (new_dir / "ep.strm").exists()
    assert (new_dir / "ep.zh.srt").read_text(encoding="utf-8") == "sub"
    assert not old_dir.exists()
    record = db.get_file_by_pickcode("pc1")
    assert record["local_strm_path"] == str(new_dir / "ep.strm")
    assert record["sha1"] == "sha-new"


def test_sha_directory_and_name_change_moves_sidecar(
    sg_module, gen, db, tmp_path, monkeypatch
):
    _reset_db(db)
    old_dir = tmp_path / "Show A"
    old_dir.mkdir()
    old_path = old_dir / "ep.strm"
    sidecar = old_dir / "ep.zh.srt"
    old_path.write_text("old", encoding="utf-8")
    sidecar.write_text("sub", encoding="utf-8")
    db.batch_add_files([
        _record("pc1", "ep.mkv", "/media/Show A/ep.mkv", old_path, "sha-old")
    ])
    monkeypatch.setattr(
        sg_module,
        "_iter_files_115",
        _fake_iter([
            _attr("ep-renamed.mkv", "pc1", "/media/Show B/ep-renamed.mkv", "sha-new")
        ]),
    )

    result = gen.incremental_sync([{"from": "/media", "to": str(tmp_path)}])

    assert result["changed"] == 1
    new_dir = tmp_path / "Show B"
    assert (new_dir / "ep-renamed.strm").exists()
    assert (new_dir / "ep.zh.srt").read_text(encoding="utf-8") == "sub"
    assert not (new_dir / "ep.strm").exists()
    assert not old_dir.exists()


def test_iter_files_115_raises_when_client_closed(sg_module):
    wrapper = SimpleNamespace(client=None)

    with pytest.raises(sg_module.TraversalError):
        list(sg_module._iter_files_115(wrapper, 100))


def test_full_sync_closed_client_does_not_cleanup(
    sg_module, gen, db, tmp_path
):
    _reset_db(db)
    gen._cleanup_deleted = True
    gen._client = SimpleNamespace(client=None)
    gen._resolve_pan_path = MagicMock(return_value=100)
    old_path = tmp_path / "old.strm"
    old_path.write_text("keep", encoding="utf-8")
    db.batch_add_files([
        _record("old", "old.mp4", "/media/old.mp4", old_path)
    ])
    result = gen.full_sync([{"from": "/media", "to": str(tmp_path)}])

    assert result["failed"] == 1
    assert result["deleted"] == 0
    assert old_path.exists()
    assert db.get_file_by_pickcode("old") is not None


@pytest.mark.parametrize("sync_type", ["full", "incremental"])
def test_pickcode_replacement_conflict_preserves_old_record(
    sg_module, gen, db, tmp_path, monkeypatch, sync_type
):
    _reset_db(db)
    gen._cleanup_deleted = True
    old_path = tmp_path / "movie.strm"
    old_path.write_text("pickcode=old-pc", encoding="utf-8")
    db.batch_add_files([
        _record("old-pc", "movie.mp4", "/media/movie.mp4", old_path)
    ])
    items = [
        _attr("movie.mkv", "new-pc", "/media/movie.mkv", "sha-new"),
        _attr("other.mp4", "other-pc", "/media/other.mp4", "sha-other"),
    ]
    monkeypatch.setattr(sg_module, "_iter_files_115", _fake_iter(items))

    if sync_type == "full":
        result = gen.full_sync([{"from": "/media", "to": str(tmp_path)}])
    else:
        result = gen.incremental_sync([{"from": "/media", "to": str(tmp_path)}])

    assert result["failed"] == 1
    assert result["deleted"] == 0
    assert old_path.read_text(encoding="utf-8") == "pickcode=old-pc"
    assert db.get_file_by_pickcode("old-pc") is not None
    assert db.get_file_by_pickcode("new-pc") is None


def test_cancel_after_full_sync_write_still_commits(
    sg_module, gen, db, tmp_path, monkeypatch
):
    _reset_db(db)
    original_ensure = gen._ensure_strm_file

    def write_then_cancel(*args, **kwargs):
        written = original_ensure(*args, **kwargs)
        gen.cancel()
        return written

    monkeypatch.setattr(gen, "_ensure_strm_file", write_then_cancel)
    monkeypatch.setattr(
        sg_module,
        "_iter_files_115",
        _fake_iter([_attr("a.mp4", "pc1", "/media/a.mp4")]),
    )

    result = gen.full_sync([{"from": "/media", "to": str(tmp_path)}])

    assert result["cancelled"] is True
    assert (tmp_path / "a.strm").exists()
    assert db.get_file_by_pickcode("pc1") is not None


def test_cancel_after_incremental_rename_still_commits(
    sg_module, gen, db, tmp_path, monkeypatch
):
    _reset_db(db)
    old_path = tmp_path / "old.strm"
    old_path.write_text("pickcode=pc1", encoding="utf-8")
    db.batch_add_files([
        _record("pc1", "old.mp4", "/media/old.mp4", old_path)
    ])
    original_migrate = gen._migrate_strm_file

    def migrate_then_cancel(*args, **kwargs):
        migrated = original_migrate(*args, **kwargs)
        gen.cancel()
        return migrated

    monkeypatch.setattr(gen, "_migrate_strm_file", migrate_then_cancel)
    monkeypatch.setattr(
        sg_module,
        "_iter_files_115",
        _fake_iter([_attr("new.mp4", "pc1", "/media/new.mp4")]),
    )

    result = gen.incremental_sync([{"from": "/media", "to": str(tmp_path)}])

    assert result["cancelled"] is True
    assert result["changed"] == 1
    assert not old_path.exists()
    assert (tmp_path / "new.strm").exists()
    record = db.get_file_by_pickcode("pc1")
    assert record["local_strm_path"] == str(tmp_path / "new.strm")


def test_never_mode_orphan_strm_is_not_registered(
    sg_module, gen, db, tmp_path, monkeypatch
):
    _reset_db(db)
    gen._overwrite_mode = "never"
    orphan = tmp_path / "movie.strm"
    orphan.write_text("http://old/redirect?pickcode=old-pc", encoding="utf-8")
    monkeypatch.setattr(
        sg_module,
        "_iter_files_115",
        _fake_iter([_attr("movie.mp4", "new-pc", "/media/movie.mp4")]),
    )

    result = gen.full_sync([{"from": "/media", "to": str(tmp_path)}])

    assert result["new"] == 0
    assert result["failed"] == 1
    assert orphan.read_text(encoding="utf-8").endswith("pickcode=old-pc")
    assert db.get_file_by_pickcode("new-pc") is None


def test_fast_scan_root_numeric_zero_parent_is_skipped(sg_module):
    wrapper = MagicMock()
    wrapper.scan_get_json.side_effect = [
        {
            "state": True,
            "count": 1,
            "data": [{"cid": "100", "n": "top.mkv", "s": "1", "pc": "ab12"}],
        },
        {
            "state": True,
            "data": {
                "list": [{"fid": "100", "fn": "Media", "pid": 0}],
                "has_next_page": False,
            },
        },
    ]
    pickcode_mod = SimpleNamespace(
        get_stable_point=lambda pc: "0ab1",
        id_to_pickcode=lambda cid, sp, prefix: f"PC{prefix}{cid}",
    )
    with patch.dict("sys.modules", {"p115pickcode": pickcode_mod}):
        results = sg_module.iter_files_fast(wrapper, 100, root_pan_path="/Media")

    assert results[0]["path"] == "/Media/top.mkv"


def test_mapping_to_change_triggers_strm_migration(
    sg_module, gen, db, tmp_path, monkeypatch
):
    _reset_db(db)
    old_root = tmp_path / "old-root"
    new_root = tmp_path / "new-root"
    old_root.mkdir()
    old_path = old_root / "keep.strm"
    old_path.write_text("keep", encoding="utf-8")
    db.batch_add_files([
        _record("pc1", "keep.mp4", "/media/keep.mp4", old_path)
    ])
    monkeypatch.setattr(
        sg_module,
        "_iter_files_115",
        _fake_iter([_attr("keep.mp4", "pc1", "/media/keep.mp4")]),
    )

    result = gen.incremental_sync([{"from": "/media", "to": str(new_root)}])

    assert result["changed"] == 1
    assert result["unchanged"] == 0
    assert not old_path.exists()
    assert (new_root / "keep.strm").exists()
    record = db.get_file_by_pickcode("pc1")
    assert record["local_strm_path"] == str(new_root / "keep.strm")
