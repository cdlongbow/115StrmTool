import sqlite3
import sys
import threading
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from logger import logger

if getattr(sys, "frozen", False):
    _BASE_DIR = Path(sys.executable).parent
else:
    _BASE_DIR = Path(__file__).parent

_SCHEMA_VERSION = 1

_BASE_SCHEMA = """
    CREATE TABLE IF NOT EXISTS files (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        pickcode TEXT NOT NULL UNIQUE,
        file_name TEXT NOT NULL,
        file_size INTEGER DEFAULT 0,
        file_type TEXT DEFAULT '',
        pan_path TEXT NOT NULL,
        local_strm_path TEXT NOT NULL,
        sha1 TEXT DEFAULT '',
        parent_id TEXT DEFAULT '',
        created_at TEXT DEFAULT (datetime('now','localtime')),
        updated_at TEXT DEFAULT (datetime('now','localtime')),
        status TEXT DEFAULT 'active'
    );

    CREATE TABLE IF NOT EXISTS sync_history (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        sync_type TEXT NOT NULL,
        start_time TEXT,
        end_time TEXT,
        total_files INTEGER DEFAULT 0,
        new_files INTEGER DEFAULT 0,
        deleted_files INTEGER DEFAULT 0,
        failed_files INTEGER DEFAULT 0,
        status TEXT DEFAULT 'running',
        error_message TEXT DEFAULT ''
    );
"""

# 索引在迁移完成后创建：旧库表可能缺少 status 列，提前建索引会失败
_POST_MIGRATION_STATEMENTS = (
    "CREATE INDEX IF NOT EXISTS idx_files_pan_path ON files(pan_path)",
    "CREATE INDEX IF NOT EXISTS idx_files_status ON files(status)",
)


def _migration_1_ensure_file_columns(conn: sqlite3.Connection):
    """
    补齐早期版本 files 表可能缺失的列，重复执行安全

    :param conn (Connection): SQLite 连接
    """
    existing = {row[1] for row in conn.execute("PRAGMA table_info(files)").fetchall()}
    additions = {
        "file_size": "INTEGER DEFAULT 0",
        "file_type": "TEXT DEFAULT ''",
        "sha1": "TEXT DEFAULT ''",
        "parent_id": "TEXT DEFAULT ''",
        "status": "TEXT DEFAULT 'active'",
    }
    for column, ddl in additions.items():
        if column not in existing:
            conn.execute(f"ALTER TABLE files ADD COLUMN {column} {ddl}")
            logger.info("数据库迁移: files 表补充列 %s", column)


_MIGRATIONS: List[Tuple[int, Callable[[sqlite3.Connection], None]]] = [
    (1, _migration_1_ensure_file_columns),
]


class Database:
    def __init__(self, db_path: str = None):
        self._local = threading.local()
        if db_path is None:
            db_path = str(_BASE_DIR / "data" / "strm.db")
        self.db_path = db_path
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    @property
    def conn(self) -> sqlite3.Connection:
        if not hasattr(self._local, "conn") or self._local.conn is None:
            self._local.conn = sqlite3.connect(self.db_path)
            self._local.conn.row_factory = sqlite3.Row
            self._local.conn.execute("PRAGMA journal_mode=WAL")
            self._local.conn.execute("PRAGMA foreign_keys=ON")
        return self._local.conn

    def _init_db(self):
        conn = sqlite3.connect(self.db_path)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(_BASE_SCHEMA)
        self._run_migrations(conn)
        for stmt in _POST_MIGRATION_STATEMENTS:
            conn.execute(stmt)
        conn.commit()
        conn.close()

    def _run_migrations(self, conn: sqlite3.Connection):
        """
        按 ``PRAGMA user_version`` 执行增量迁移，已是最新版本时跳过

        :param conn (Connection): SQLite 连接
        """
        current = conn.execute("PRAGMA user_version").fetchone()[0]
        if current >= _SCHEMA_VERSION:
            return
        logger.info("数据库迁移: 当前版本 %s -> %s", current, _SCHEMA_VERSION)
        for version, migration in _MIGRATIONS:
            if current < version:
                migration(conn)
                current = version
        conn.execute(f"PRAGMA user_version={_SCHEMA_VERSION}")


    def batch_add_files(self, files: List[Dict[str, Any]]):
        """
        批量写入或更新文件记录

        使用 UPSERT 而非 INSERT OR REPLACE，重扫已有文件时保留
        created_at 等历史字段，仅刷新元数据并重新置为 active

        :param files (List): 文件记录字典列表
        """
        with self.conn:
            self.conn.executemany(
                """INSERT INTO files
                   (pickcode, file_name, file_size, file_type, pan_path, local_strm_path, sha1, parent_id, status)
                   VALUES (:pickcode, :file_name, :file_size, :file_type, :pan_path, :local_strm_path, :sha1, :parent_id, 'active')
                   ON CONFLICT(pickcode) DO UPDATE SET
                       file_name=excluded.file_name,
                       file_size=excluded.file_size,
                       file_type=excluded.file_type,
                       pan_path=excluded.pan_path,
                       local_strm_path=excluded.local_strm_path,
                       sha1=excluded.sha1,
                       parent_id=excluded.parent_id,
                       status='active',
                       updated_at=datetime('now','localtime')""",
                files,
            )

    def get_file_by_pickcode(self, pickcode: str) -> Optional[Dict]:
        cursor = self.conn.execute(
            "SELECT * FROM files WHERE pickcode=? AND status='active'", (pickcode,)
        )
        row = cursor.fetchone()
        return dict(row) if row else None

    def count_active_files(self) -> int:
        cursor = self.conn.execute("SELECT COUNT(*) FROM files WHERE status='active'")
        return cursor.fetchone()[0]

    def add_sync_history(self, sync_type: str) -> int:
        cursor = self.conn.execute(
            "INSERT INTO sync_history (sync_type, start_time, status) VALUES (?, datetime('now','localtime'), 'running')",
            (sync_type,),
        )
        self.conn.commit()
        return cursor.lastrowid

    def finish_sync_history(
        self,
        history_id: int,
        total: int,
        new_count: int,
        deleted: int,
        failed: int,
        error: str = "",
    ):
        self.conn.execute(
            """UPDATE sync_history SET
               end_time=datetime('now','localtime'),
               total_files=?, new_files=?, deleted_files=?, failed_files=?,
               status=?, error_message=?
               WHERE id=?""",
            (total, new_count, deleted, failed, "failed" if error else "completed", error, history_id),
        )
        self.conn.commit()

    def get_sync_history(self, limit: int = 20) -> List[Dict]:
        cursor = self.conn.execute(
            "SELECT * FROM sync_history ORDER BY id DESC LIMIT ?", (limit,)
        )
        return [dict(row) for row in cursor.fetchall()]

    def clear_sync_history(self):
        self.conn.execute("DELETE FROM sync_history")
        self.conn.commit()

    def clear_all_files(self):
        self.conn.execute("DELETE FROM files")
        self.conn.commit()
        self.conn.execute("VACUUM")

    def get_active_files_by_parent(self, parent_prefix: str) -> List[Dict]:
        """
        查询指定网盘目录及子目录下的活动文件记录

        采用精确边界匹配：仅命中目录本身或其真实子路径，
        防止兄弟目录（如 /movies 与 /movies2）因 LIKE 前缀歧义被误判删除

        :param parent_prefix (str): 网盘目录前缀，允许携带尾部斜杠

        :return List: 活动文件记录列表
        """
        base = parent_prefix.rstrip("/") or "/"
        if base == "/":
            cursor = self.conn.execute(
                "SELECT pickcode, pan_path, local_strm_path, sha1 "
                "FROM files WHERE status='active'"
            )
        else:
            escaped = (
                base.replace("\\", "\\\\")
                .replace("%", "\\%")
                .replace("_", "\\_")
            )
            cursor = self.conn.execute(
                "SELECT pickcode, pan_path, local_strm_path, sha1 FROM files "
                "WHERE status='active' AND (pan_path = ? OR pan_path LIKE ? ESCAPE '\\')",
                (base, escaped + "/%"),
            )
        return [dict(row) for row in cursor.fetchall()]

    def mark_file_deleted(self, pickcode: str):
        self.conn.execute(
            "UPDATE files SET status='deleted', updated_at=datetime('now','localtime') WHERE pickcode=? AND status='active'",
            (pickcode,),
        )
        self.conn.commit()

    def get_stats(self) -> Dict:
        total = self.count_active_files()
        cursor = self.conn.execute(
            "SELECT SUM(file_size) FROM files WHERE status='active'"
        )
        total_size = cursor.fetchone()[0] or 0
        cursor = self.conn.execute(
            "SELECT COUNT(*) FROM sync_history WHERE status='completed'"
        )
        sync_count = cursor.fetchone()[0]
        return {
            "total_files": total,
            "total_size": total_size,
            "sync_count": sync_count,
        }


db = Database()
