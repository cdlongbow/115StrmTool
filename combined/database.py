import sqlite3
import json
import sys
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from logger import logger

if getattr(sys, "frozen", False):
    _BASE_DIR = Path(sys.executable).parent
else:
    _BASE_DIR = Path(__file__).parent


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
        conn.executescript("""
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
            CREATE INDEX IF NOT EXISTS idx_files_pan_path ON files(pan_path);
            CREATE INDEX IF NOT EXISTS idx_files_status ON files(status);

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

            CREATE TABLE IF NOT EXISTS share_transfer_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                share_url TEXT NOT NULL,
                source_path TEXT DEFAULT '',
                target_path TEXT DEFAULT '',
                status TEXT DEFAULT 'pending',
                file_count INTEGER DEFAULT 0,
                created_at TEXT DEFAULT (datetime('now','localtime'))
            );

            CREATE TABLE IF NOT EXISTS offline_tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                url TEXT NOT NULL,
                name TEXT DEFAULT '',
                status TEXT DEFAULT 'pending',
                progress INTEGER DEFAULT 0,
                file_size INTEGER DEFAULT 0,
                save_path TEXT DEFAULT '',
                created_at TEXT DEFAULT (datetime('now','localtime'))
            );
        """)
        conn.commit()
        conn.close()

    def add_file(self, file_info: Dict[str, Any]) -> int:
        cursor = self.conn.execute(
            """INSERT OR REPLACE INTO files
               (pickcode, file_name, file_size, file_type, pan_path, local_strm_path, sha1, parent_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                file_info["pickcode"],
                file_info["file_name"],
                file_info.get("file_size", 0),
                file_info.get("file_type", ""),
                file_info["pan_path"],
                file_info["local_strm_path"],
                file_info.get("sha1", ""),
                file_info.get("parent_id", ""),
            ),
        )
        self.conn.commit()
        return cursor.lastrowid

    def batch_add_files(self, files: List[Dict[str, Any]]):
        cursor = self.conn.cursor()
        cursor.execute("BEGIN")
        try:
            cursor.executemany(
                """INSERT OR REPLACE INTO files
                   (pickcode, file_name, file_size, file_type, pan_path, local_strm_path, sha1, parent_id)
                   VALUES (:pickcode, :file_name, :file_size, :file_type, :pan_path, :local_strm_path, :sha1, :parent_id)""",
                files,
            )
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

    def remove_file_by_pan_path(self, pan_path: str):
        self.conn.execute(
            "UPDATE files SET status='deleted', updated_at=datetime('now','localtime') WHERE pan_path=?",
            (pan_path,),
        )
        self.conn.commit()

    def get_file_by_pickcode(self, pickcode: str) -> Optional[Dict]:
        cursor = self.conn.execute(
            "SELECT * FROM files WHERE pickcode=? AND status='active'", (pickcode,)
        )
        row = cursor.fetchone()
        return dict(row) if row else None

    def get_file_by_path(self, pan_path: str) -> Optional[Dict]:
        cursor = self.conn.execute(
            "SELECT * FROM files WHERE pan_path=? AND status='active'", (pan_path,)
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
        self.conn.commit()

    def add_share_transfer(self, share_url: str, target_path: str) -> int:
        cursor = self.conn.execute(
            "INSERT INTO share_transfer_history (share_url, target_path) VALUES (?, ?)",
            (share_url, target_path),
        )
        self.conn.commit()
        return cursor.lastrowid

    def add_offline_task(self, url: str, name: str, save_path: str) -> int:
        cursor = self.conn.execute(
            "INSERT INTO offline_tasks (url, name, save_path) VALUES (?, ?, ?)",
            (url, name, save_path),
        )
        self.conn.commit()
        return cursor.lastrowid

    def get_active_files_by_parent(self, parent_prefix: str) -> List[Dict]:
        cursor = self.conn.execute(
            "SELECT pickcode, pan_path, local_strm_path, sha1 FROM files WHERE status='active' AND pan_path LIKE ?",
            (parent_prefix + "%",),
        )
        return [dict(row) for row in cursor.fetchall()]

    def mark_file_deleted(self, pickcode: str):
        self.conn.execute(
            "UPDATE files SET status='deleted', updated_at=datetime('now','localtime') WHERE pickcode=? AND status='active'",
            (pickcode,),
        )
        self.conn.commit()

    def batch_mark_deleted(self, pickcodes: List[str]):
        if not pickcodes:
            return
        cursor = self.conn.cursor()
        cursor.executemany(
            "UPDATE files SET status='deleted', updated_at=datetime('now','localtime') WHERE pickcode=? AND status='active'",
            [(pc,) for pc in pickcodes],
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
