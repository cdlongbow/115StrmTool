"""
数据库版本化迁移测试：user_version 推进、旧库补列、幂等重开
"""
import sqlite3
import tempfile
from pathlib import Path


LEGACY_SCHEMA = """
CREATE TABLE files (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    pickcode TEXT NOT NULL UNIQUE,
    file_name TEXT NOT NULL,
    pan_path TEXT NOT NULL,
    local_strm_path TEXT NOT NULL,
    created_at TEXT,
    updated_at TEXT
);
"""

# 缺少 UNIQUE 约束的历史建表形态：迁移只补列时 UPSERT 会报冲突目标不匹配
NO_UNIQUE_SCHEMA = """
CREATE TABLE files (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    pickcode TEXT NOT NULL,
    file_name TEXT NOT NULL,
    file_size INTEGER DEFAULT 0,
    file_type TEXT DEFAULT '',
    pan_path TEXT NOT NULL,
    local_strm_path TEXT NOT NULL,
    sha1 TEXT DEFAULT '',
    parent_id TEXT DEFAULT '',
    created_at TEXT,
    updated_at TEXT,
    status TEXT DEFAULT 'active'
);
"""


def _file_row(pickcode, name="a.mp4", pan_path="/x/a.mp4"):
    return {
        "pickcode": pickcode,
        "file_name": name,
        "file_size": 1,
        "file_type": "mp4",
        "pan_path": pan_path,
        "local_strm_path": "E:/x/a.strm",
        "sha1": "",
        "parent_id": "",
    }


def _new_path():
    return str(Path(tempfile.mkdtemp()) / "migrate.db")


def _columns(db_path):
    conn = sqlite3.connect(db_path)
    try:
        return {row[1] for row in conn.execute("PRAGMA table_info(files)")}
    finally:
        conn.close()


def test_fresh_db_sets_schema_version():
    from database import Database, _SCHEMA_VERSION

    path = _new_path()
    db = Database(path)
    version = db.conn.execute("PRAGMA user_version").fetchone()[0]
    assert version == _SCHEMA_VERSION
    cols = _columns(path)
    assert {"file_size", "file_type", "sha1", "parent_id", "status"} <= cols
    conn = sqlite3.connect(path)
    names = {
        row[1] for row in conn.execute("PRAGMA index_list(files)")
    }
    conn.close()
    assert {"idx_files_pan_path", "idx_files_status"} <= names


def test_legacy_db_gets_missing_columns():
    from database import Database, _SCHEMA_VERSION

    path = _new_path()
    conn = sqlite3.connect(path)
    conn.executescript(LEGACY_SCHEMA)
    pickcode = "l" * 17
    conn.execute(
        "INSERT INTO files (pickcode, file_name, pan_path, local_strm_path) "
        "VALUES (?, ?, ?, ?)",
        (pickcode, "old.mkv", "/movies/old.mkv", "D:/old.strm"),
    )
    conn.commit()
    conn.close()

    db = Database(path)
    version = db.conn.execute("PRAGMA user_version").fetchone()[0]
    assert version == _SCHEMA_VERSION
    cols = _columns(path)
    assert {"file_size", "file_type", "sha1", "parent_id", "status"} <= cols
    row = db.get_file_by_pickcode(pickcode)
    assert row is not None, "补列后旧行默认值应命中 status='active' 查询"
    assert row["status"] == "active"


def test_reopen_is_idempotent():
    from database import Database, _SCHEMA_VERSION

    path = _new_path()
    Database(path).conn.close()
    before = _columns(path)
    db2 = Database(path)
    after = _columns(path)
    version = db2.conn.execute("PRAGMA user_version").fetchone()[0]
    assert version == _SCHEMA_VERSION
    assert before == after


def _unique_pickcode_indexes(db_path):
    conn = sqlite3.connect(db_path)
    try:
        result = []
        for idx in conn.execute("PRAGMA index_list(files)").fetchall():
            if not idx[2]:
                continue
            cols = [r[2] for r in conn.execute(f"PRAGMA index_info({idx[1]})")]
            if cols == ["pickcode"]:
                result.append(idx[1])
        return result
    finally:
        conn.close()


def test_legacy_db_without_unique_constraint_gets_one():
    from database import Database, _SCHEMA_VERSION

    path = _new_path()
    conn = sqlite3.connect(path)
    conn.executescript(NO_UNIQUE_SCHEMA)
    conn.execute(
        "INSERT INTO files (pickcode, file_name, pan_path, local_strm_path) "
        "VALUES (?, ?, ?, ?)",
        ("l" * 17, "old.mkv", "/movies/old.mkv", "D:/old.strm"),
    )
    conn.commit()
    conn.close()

    db = Database(path)
    assert db.conn.execute("PRAGMA user_version").fetchone()[0] == _SCHEMA_VERSION
    assert _unique_pickcode_indexes(path), "迁移后 pickcode 应具备唯一约束"


def test_upsert_works_after_migration_on_legacy_db():
    from database import Database

    path = _new_path()
    conn = sqlite3.connect(path)
    conn.executescript(NO_UNIQUE_SCHEMA)
    conn.commit()
    conn.close()

    db = Database(path)
    db.batch_add_files([_file_row("p1", "first.mp4")])
    db.batch_add_files([_file_row("p1", "second.mp4")])
    row = db.get_file_by_pickcode("p1")
    assert row is not None
    assert row["file_name"] == "second.mp4", "同一 pickcode 应更新而非报冲突"


def test_duplicate_pickcode_rows_merged_keeping_active():
    from database import Database

    path = _new_path()
    conn = sqlite3.connect(path)
    conn.executescript(NO_UNIQUE_SCHEMA)
    conn.execute(
        "INSERT INTO files (pickcode, file_name, pan_path, local_strm_path, status) "
        "VALUES (?, ?, ?, ?, ?)",
        ("dup", "deleted-old", "/x/1", "E:/1", "deleted"),
    )
    conn.execute(
        "INSERT INTO files (pickcode, file_name, pan_path, local_strm_path, status) "
        "VALUES (?, ?, ?, ?, ?)",
        ("dup", "active-new", "/x/2", "E:/2", "active"),
    )
    conn.commit()
    conn.close()

    db = Database(path)
    rows = db.conn.execute(
        "SELECT file_name FROM files WHERE pickcode='dup'"
    ).fetchall()
    assert len(rows) == 1, "重复 pickcode 应合并为一行"
    assert rows[0][0] == "active-new", "应保留 active 行"


def test_migration_2_is_idempotent():
    from database import Database

    path = _new_path()
    conn = sqlite3.connect(path)
    conn.executescript(NO_UNIQUE_SCHEMA)
    conn.commit()
    conn.close()

    Database(path).conn.close()
    before = _unique_pickcode_indexes(path)
    Database(path).conn.close()
    after = _unique_pickcode_indexes(path)
    assert before == after
