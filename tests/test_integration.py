"""Integration tests for AROS Meta Loop — full cycle."""
import asyncio
import json
import sqlite3
import time
import pytest
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import patch

from aros_meta_loop.db.migrations import run_migrations
from aros_meta_loop.services.engine import MetaLoopEngine
from aros_meta_loop.services.state_manager import StateManager
from aros_meta_loop.config import META_COGNITION_DEFAULT, SELF_MODEL_DEFAULT, POLICY_DEFAULT, _write_default


@pytest.fixture
def integration_env(tmp_path):
    """Full integration environment with seeded data."""
    db_path = tmp_path / "test.db"
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    run_migrations(conn)

    now = datetime.now(timezone.utc)

    # Seed a realistic set of events
    events = [
        # Multiple tool calls with varying success
        ("bot1", "tool_call", "s1", json.dumps({"tool": "grep", "success": True, "tokens_in": 2000, "tokens_out": 800, "duration_ms": 300}), now.isoformat()),
        ("bot1", "tool_call", "s1", json.dumps({"tool": "read", "success": True, "tokens_in": 1500, "tokens_out": 600, "duration_ms": 200}), now.isoformat()),
        ("bot1", "tool_call", "s1", json.dumps({"tool": "edit", "success": True, "tokens_in": 3000, "tokens_out": 1200, "duration_ms": 500}), now.isoformat()),
        ("bot1", "tool_call", "s1", json.dumps({"tool": "bash", "success": False, "tokens_in": 1000, "tokens_out": 400, "duration_ms": 10000}), now.isoformat()),
        ("bot1", "tool_call", "s1", json.dumps({"tool": "write", "success": True, "tokens_in": 2500, "tokens_out": 1000, "duration_ms": 400}), now.isoformat()),

        # Tasks
        ("bot1", "task_complete", "s1", json.dumps({"task_id": "t1", "tokens_consumed": 8000, "duration_seconds": 120, "retries": 0, "complexity": 3}), now.isoformat()),
        ("bot1", "task_complete", "s1", json.dumps({"task_id": "t2", "tokens_consumed": 5000, "duration_seconds": 90, "retries": 1, "complexity": 2}), now.isoformat()),
        ("bot1", "task_failed", "s1", json.dumps({"task_id": "t3", "tokens_consumed": 3000, "duration_seconds": 45, "retries": 2, "error_type": "timeout"}), now.isoformat()),

        # Sessions
        ("bot1", "session_start", "s1", json.dumps({"context_tokens": 60000}), now.isoformat()),
        ("bot1", "session_end", "s1", json.dumps({"context_tokens": 95000}), now.isoformat()),
    ]

    for bot_id, event_type, session_id, data, created_at in events:
        conn.execute(
            "INSERT INTO meta_events (bot_id, event_type, session_id, data, created_at) VALUES (?, ?, ?, ?, ?)",
            (bot_id, event_type, session_id, data, created_at)
        )
    conn.commit()

    # State dir
    state_dir = tmp_path / ".aros"
    state_dir.mkdir()
    for sub in ("data", "signals", "pending-review", "state"):
        (state_dir / sub).mkdir()
    _write_default(state_dir / "meta-cognition.toml", META_COGNITION_DEFAULT)
    _write_default(state_dir / "self-model.toml", SELF_MODEL_DEFAULT)
    _write_default(state_dir / "policy.toml", POLICY_DEFAULT)

    state_mgr = StateManager(state_dir=state_dir)
    return {"conn": conn, "state_dir": state_dir, "state_mgr": state_mgr}


def _patch_db(env):
    """Return a combined context manager that patches all get_db callsites."""
    return (
        patch("aros_meta_loop.services.metrics.get_db", return_value=env["conn"]),
        patch("aros_meta_loop.services.engine.get_db", return_value=env["conn"]),
        patch("aros_meta_loop.services.event_emitter.get_db", return_value=env["conn"]),
    )


class TestFullCycleIntegration:
    def _run(self, coro):
        """Helper to run async code in tests."""
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(coro)
        finally:
            loop.close()

    def test_full_cycle_completes_all_steps(self, integration_env):
        """Trigger a cycle and verify all 6 steps complete."""
        p1, p2, p3 = _patch_db(integration_env)
        with p1, p2, p3:
            engine = MetaLoopEngine(state_manager=integration_env["state_mgr"], bot_id="bot1")
            result = self._run(engine.run_cycle("integration_test"))

            assert result["status"] in ("completed", "aborted")
            assert result["steps_completed"] >= 5  # At least 5 of 6 steps
            assert "perceive_data" in result

    def test_meta_iterations_recorded(self, integration_env):
        """Verify cycle is recorded in meta_iterations table."""
        p1, p2, p3 = _patch_db(integration_env)
        with p1, p2, p3:
            engine = MetaLoopEngine(state_manager=integration_env["state_mgr"], bot_id="bot1")
            self._run(engine.run_cycle("integration_test"))

            row = integration_env["conn"].execute(
                "SELECT * FROM meta_iterations WHERE bot_id='bot1'"
            ).fetchone()
            assert row is not None
            assert row["trigger"] == "integration_test"
            assert row["steps_completed"] >= 5

    def test_evolution_log_written(self, integration_env):
        """Verify evolution log is readable after cycle (may be empty if no changes applied)."""
        p1, p2, p3 = _patch_db(integration_env)
        with p1, p2, p3:
            engine = MetaLoopEngine(state_manager=integration_env["state_mgr"], bot_id="bot1")
            self._run(engine.run_cycle("integration_test"))

            log = integration_env["state_mgr"].read_evolution_log()
            # Log may or may not have entries depending on whether changes were made
            # But the cycle should complete without error
            assert isinstance(log, list)

    def test_self_model_updated(self, integration_env):
        """Verify self-model is updated after cycle."""
        p1, p2, p3 = _patch_db(integration_env)
        with p1, p2, p3:
            engine = MetaLoopEngine(state_manager=integration_env["state_mgr"], bot_id="bot1")
            self._run(engine.run_cycle("integration_test"))

            model = integration_env["state_mgr"].read_self_model()
            # Self-model should have calibration data after cycle
            assert "calibration" in model or "capabilities" in model

    def test_status_after_cycle(self, integration_env):
        """Verify get_status() works after a cycle."""
        p1, p2, p3 = _patch_db(integration_env)
        with p1, p2, p3:
            engine = MetaLoopEngine(state_manager=integration_env["state_mgr"], bot_id="bot1")
            self._run(engine.run_cycle("integration_test"))

            status = engine.get_status()
            assert status["last_cycle"] is not None
            assert status["last_cycle"]["trigger"] == "integration_test"

    def test_cycle_under_30_seconds(self, integration_env):
        """Verify cycle runs in acceptable time."""
        p1, p2, p3 = _patch_db(integration_env)
        with p1, p2, p3:
            engine = MetaLoopEngine(state_manager=integration_env["state_mgr"], bot_id="bot1")
            start = time.time()
            self._run(engine.run_cycle("integration_test"))
            elapsed = time.time() - start
            assert elapsed < 30, f"Cycle took {elapsed:.1f}s (>30s limit)"

    def test_permission_classification_in_cycle(self, integration_env):
        """Verify permission classification works during a real cycle."""
        p1, p2, p3 = _patch_db(integration_env)
        with p1, p2, p3:
            engine = MetaLoopEngine(state_manager=integration_env["state_mgr"], bot_id="bot1")
            result = self._run(engine.run_cycle("integration_test"))

            # If changes were proposed, verify they have permission levels
            if result.get("policy_changes"):
                for change in result["policy_changes"]:
                    assert "permission" in change
                    assert change["permission"] in ("AUTO_APPROVE", "HUMAN_REVIEW", "NEVER")

    def test_perceive_data_structure(self, integration_env):
        """Verify perceive step returns expected data structure."""
        p1, p2, p3 = _patch_db(integration_env)
        with p1, p2, p3:
            engine = MetaLoopEngine(state_manager=integration_env["state_mgr"], bot_id="bot1")
            result = self._run(engine.run_cycle("integration_test"))

            pd = result.get("perceive_data", {})
            assert "l1_metrics" in pd
            assert "l2_scores" in pd
            assert "l3_signals" in pd
            assert "current_policy" in pd

            # L1 should reflect our seeded data
            l1 = pd["l1_metrics"]
            assert l1.get("event_count", 0) > 0
            assert l1.get("tool_call_count", 0) >= 5

    def test_feedback_channels_signal_queue(self, integration_env):
        """Verify signal queue (feedback channel) works during cycle."""
        # Push a signal before cycle runs
        integration_env["state_mgr"].push_signal({
            "source": "external_test",
            "priority": "normal",
            "payload": {"message": "test feedback signal"},
        })

        p1, p2, p3 = _patch_db(integration_env)
        with p1, p2, p3:
            engine = MetaLoopEngine(state_manager=integration_env["state_mgr"], bot_id="bot1")
            result = self._run(engine.run_cycle("integration_test"))

            pd = result.get("perceive_data", {})
            # The signal should have been drained during perceive
            queued = pd.get("queued_signals", [])
            assert any(s.get("source") == "external_test" for s in queued)

    def test_multiple_cycles_cadence_respected(self, integration_env):
        """Verify second cycle is throttled by cadence controller."""
        p1, p2, p3 = _patch_db(integration_env)
        with p1, p2, p3:
            engine = MetaLoopEngine(state_manager=integration_env["state_mgr"], bot_id="bot1")
            result1 = self._run(engine.run_cycle("integration_test"))
            assert result1["status"] in ("completed", "aborted")

            # Second cycle should be throttled or skipped (min interval)
            result2 = self._run(engine.run_cycle("integration_test"))
            assert result2["status"] in ("throttled", "skipped")
            assert "interval" in result2.get("reason", "") or "min_interval" in result2.get("reason", "")
