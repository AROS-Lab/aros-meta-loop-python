"""Database engine for AROS Meta Loop — SQLite with WAL mode."""
import sqlite3
import threading
from pathlib import Path

from aros_meta_loop.config import DATABASE_PATH
from aros_meta_loop.db.migrations import run_migrations

_local = threading.local()
_write_lock = threading.Lock()


def get_db() -> sqlite3.Connection:
    """Return a thread-local SQLite connection, creating it if needed."""
    if not hasattr(_local, "conn") or _local.conn is None:
        DATABASE_PATH.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(DATABASE_PATH), timeout=5)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        run_migrations(conn)
        _local.conn = conn
    return _local.conn


def db_write_lock() -> threading.Lock:
    """Return the global write-serialization lock."""
    return _write_lock
