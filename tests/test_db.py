"""Tests for database schema, migrations, and state directory initialization."""
import sqlite3
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest


def test_migrations_create_tables():
    """Test that migrations create expected tables."""
    from aros_meta_loop.db.migrations import run_migrations

    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "test.db"
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        run_migrations(conn)

        # Check tables exist
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        assert "meta_events" in tables
        assert "meta_iterations" in tables
        assert "_schema_version" in tables

        # Check meta_events columns
        cols = {row[1] for row in conn.execute("PRAGMA table_info(meta_events)").fetchall()}
        assert "bot_id" in cols
        assert "event_type" in cols
        assert "session_id" in cols
        assert "data" in cols
        assert "created_at" in cols

        # Check meta_iterations columns
        cols = {row[1] for row in conn.execute("PRAGMA table_info(meta_iterations)").fetchall()}
        assert "bot_id" in cols
        assert "cycle_num" in cols
        assert "trigger" in cols
        assert "perceive_data" in cols
        assert "critique_output" in cols
        assert "policy_changes" in cols
        assert "identity_verdict" in cols
        assert "status" in cols

        conn.close()


def test_migrations_idempotent():
    """Test that running migrations twice is safe."""
    from aros_meta_loop.db.migrations import run_migrations

    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "test.db"
        conn = sqlite3.connect(str(db_path))
        run_migrations(conn)
        run_migrations(conn)  # Should not raise

        versions = conn.execute("SELECT version FROM _schema_version").fetchall()
        assert len(versions) == 1  # Only version 1
        conn.close()


def test_meta_events_insertable():
    """Test that we can insert into meta_events."""
    from aros_meta_loop.db.migrations import run_migrations

    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "test.db"
        conn = sqlite3.connect(str(db_path))
        run_migrations(conn)

        conn.execute(
            "INSERT INTO meta_events (bot_id, event_type, session_id, data) VALUES (?, ?, ?, ?)",
            ("bot1", "tool_call", "sess1", '{"key": "value"}'),
        )
        conn.commit()

        row = conn.execute("SELECT * FROM meta_events WHERE bot_id='bot1'").fetchone()
        assert row is not None
        conn.close()


def test_init_state_dir(tmp_path):
    """Test that init_state_dir creates directory structure."""
    from aros_meta_loop.config import init_state_dir

    test_dir = tmp_path / ".aros"
    with patch("aros_meta_loop.config.AROS_STATE_DIR", test_dir):
        init_state_dir()

        assert (test_dir / "data").is_dir()
        assert (test_dir / "signals").is_dir()
        assert (test_dir / "pending-review").is_dir()
        assert (test_dir / "state").is_dir()
        assert (test_dir / "meta-cognition.toml").exists()
        assert (test_dir / "self-model.toml").exists()
        assert (test_dir / "policy.toml").exists()

        # Verify TOML content has expected keys
        content = (test_dir / "meta-cognition.toml").read_text()
        assert "normal_interval_hours = 4" in content
        assert "away_interval_minutes = 30" in content
        assert "max_cycles_per_hour = 4" in content
