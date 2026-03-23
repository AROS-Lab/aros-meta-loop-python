"""Database migrations for AROS Meta Loop."""
import sqlite3
import logging

logger = logging.getLogger(__name__)

MIGRATIONS = [
    (1, "baseline: meta_events and meta_iterations", """
        CREATE TABLE IF NOT EXISTS meta_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            bot_id TEXT NOT NULL,
            event_type TEXT NOT NULL,
            session_id TEXT,
            data TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE INDEX IF NOT EXISTS idx_meta_events_bot_type ON meta_events(bot_id, event_type);
        CREATE INDEX IF NOT EXISTS idx_meta_events_created ON meta_events(created_at);

        CREATE TABLE IF NOT EXISTS meta_iterations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            bot_id TEXT NOT NULL,
            cycle_num INTEGER NOT NULL,
            trigger TEXT NOT NULL,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            steps_completed INTEGER DEFAULT 0,
            perceive_data TEXT,
            critique_output TEXT,
            policy_changes TEXT,
            identity_verdict TEXT,
            status TEXT NOT NULL DEFAULT 'running'
        );
        CREATE INDEX IF NOT EXISTS idx_meta_iterations_bot ON meta_iterations(bot_id);
    """),
]


def run_migrations(db: sqlite3.Connection):
    """Apply any unapplied migrations to the database."""
    db.execute("""
        CREATE TABLE IF NOT EXISTS _schema_version (
            version INTEGER PRIMARY KEY,
            description TEXT,
            applied_at TEXT DEFAULT (datetime('now'))
        )
    """)
    applied = {row[0] for row in db.execute("SELECT version FROM _schema_version").fetchall()}
    for version, description, sql in MIGRATIONS:
        if version not in applied:
            if sql.strip():
                for stmt in sql.split(";"):
                    stmt = stmt.strip()
                    if stmt:
                        try:
                            db.execute(stmt)
                        except sqlite3.OperationalError as e:
                            if "already exists" not in str(e) and "duplicate column" not in str(e):
                                raise
            db.execute(
                "INSERT INTO _schema_version (version, description) VALUES (?, ?)",
                (version, description),
            )
            db.commit()
            logger.info(f"Applied migration v{version}: {description}")
