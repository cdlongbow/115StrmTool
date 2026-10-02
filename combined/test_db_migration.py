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
