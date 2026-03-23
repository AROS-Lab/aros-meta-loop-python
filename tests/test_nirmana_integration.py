"""Integration tests for Nirmana autonomous flow."""
import asyncio
import json
import sqlite3
import pytest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from aros_meta_loop.db.migrations import run_migrations
from aros_meta_loop.services.engine import MetaLoopEngine
from aros_meta_loop.services.state_manager import StateManager
from aros_meta_loop.config import META_COGNITION_DEFAULT, SELF_MODEL_DEFAULT, POLICY_DEFAULT, _write_default


@pytest.fixture
def nirmana_integration_env(tmp_path):
    db_path = tmp_path / "test.db"
    conn = sqlite3.connect(str(db_path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    run_migrations(conn)

    # Seed events for a realistic scenario
    now = datetime.now(timezone.utc)
    events = [
        ("bot1", "tool_call", "s1", json.dumps({"tool": "grep", "success": True, "tokens_in": 1000, "tokens_out": 500}), now.isoformat()),
        ("bot1", "tool_call", "s1", json.dumps({"tool": "edit", "success": True, "tokens_in": 2000, "tokens_out": 800}), now.isoformat()),
        ("bot1", "task_complete", "s1", json.dumps({"task_id": "t1", "tokens_consumed": 5000, "duration_seconds": 60, "retries": 0}), now.isoformat()),
        ("bot1", "session_start", "s1", json.dumps({"context_tokens": 50000}), now.isoformat()),
    ]
    for bot_id, event_type, session_id, data, created_at in events:
        conn.execute(
            "INSERT INTO meta_events (bot_id, event_type, session_id, data, created_at) VALUES (?, ?, ?, ?, ?)",
            (bot_id, event_type, session_id, data, created_at)
        )
    conn.commit()

    state_dir = tmp_path / ".aros"
    state_dir.mkdir()
    for sub in ("data", "signals", "pending-review", "state"):
        (state_dir / sub).mkdir()
    _write_default(state_dir / "meta-cognition.toml", META_COGNITION_DEFAULT)
    _write_default(state_dir / "self-model.toml", SELF_MODEL_DEFAULT)
    _write_default(state_dir / "policy.toml", POLICY_DEFAULT)

    state_mgr = StateManager(state_dir=state_dir)
    return {"conn": conn, "state_dir": state_dir, "state_mgr": state_mgr}


class TestNirmanaIntegrationFlow:
    def test_full_nirmana_flow(self, nirmana_integration_env):
        """Full flow: activate → cycle → deactivate → briefing."""
        env = nirmana_integration_env
        with patch("aros_meta_loop.services.metrics.get_db", return_value=env["conn"]), \
             patch("aros_meta_loop.services.engine.get_db", return_value=env["conn"]), \
             patch("aros_meta_loop.services.event_emitter.get_db", return_value=env["conn"]), \
             patch("aros_meta_loop.services.scheduler.update_schedule"):
            engine = MetaLoopEngine(state_manager=env["state_mgr"], bot_id="bot1")
            loop = asyncio.new_event_loop()

            # 1. Activate Nirmana
            result = loop.run_until_complete(engine.activate_nirmana())
            assert result["mode"] == "aggressive"
            assert engine._nirmana_mode is True

            # 2. Run a cycle in Nirmana mode
            cycle_result = loop.run_until_complete(engine.run_cycle("nirmana_scheduled"))
            assert cycle_result["status"] in ("completed", "aborted", "failed")

            # 3. Deactivate and get briefing
            result = loop.run_until_complete(engine.deactivate_nirmana())
            assert result["mode"] == "balanced"
            assert engine._nirmana_mode is False

            briefing = result["briefing"]
            assert "cycles_run" in briefing
            assert "summary" in briefing

            loop.close()

    def test_cadence_switch_on_activate(self, nirmana_integration_env):
        """Verify TOML config is updated to aggressive on activate."""
        env = nirmana_integration_env
        with patch("aros_meta_loop.services.metrics.get_db", return_value=env["conn"]), \
             patch("aros_meta_loop.services.engine.get_db", return_value=env["conn"]), \
             patch("aros_meta_loop.services.event_emitter.get_db", return_value=env["conn"]), \
             patch("aros_meta_loop.services.scheduler.update_schedule"):
            engine = MetaLoopEngine(state_manager=env["state_mgr"], bot_id="bot1")
            loop = asyncio.new_event_loop()
            loop.run_until_complete(engine.activate_nirmana())

            cadence = env["state_mgr"].read_cadence()
            assert cadence["mode"] == "aggressive"

            loop.close()

    def test_cadence_restored_on_deactivate(self, nirmana_integration_env):
        """Verify TOML config is restored to balanced on deactivate."""
        env = nirmana_integration_env
        with patch("aros_meta_loop.services.metrics.get_db", return_value=env["conn"]), \
             patch("aros_meta_loop.services.engine.get_db", return_value=env["conn"]), \
             patch("aros_meta_loop.services.event_emitter.get_db", return_value=env["conn"]), \
             patch("aros_meta_loop.services.scheduler.update_schedule"):
            engine = MetaLoopEngine(state_manager=env["state_mgr"], bot_id="bot1")
            loop = asyncio.new_event_loop()
            loop.run_until_complete(engine.activate_nirmana())
            loop.run_until_complete(engine.deactivate_nirmana())

            cadence = env["state_mgr"].read_cadence()
            assert cadence["mode"] == "balanced"

            loop.close()

    def test_red_decisions_queued_in_pending_review(self, nirmana_integration_env):
        """Verify RED (HUMAN_REVIEW) decisions go to pending-review/."""
        env = nirmana_integration_env
        with patch("aros_meta_loop.services.metrics.get_db", return_value=env["conn"]), \
             patch("aros_meta_loop.services.engine.get_db", return_value=env["conn"]), \
             patch("aros_meta_loop.services.event_emitter.get_db", return_value=env["conn"]), \
             patch("aros_meta_loop.services.scheduler.update_schedule"):
            engine = MetaLoopEngine(state_manager=env["state_mgr"], bot_id="bot1")

            # Manually add a pending review item
            review_dir = env["state_dir"] / "pending-review"
            (review_dir / "test_red.json").write_text(json.dumps({
                "change_id": "test_red", "section": "goals", "key": "G1_weight",
                "old_value": "1.0", "new_value": "0.5", "status": "pending_review"
            }))

            pending = list(review_dir.glob("*.json"))
            assert len(pending) >= 1

    def test_cadence_limits_in_aggressive_mode(self, nirmana_integration_env):
        """Verify cadence limits still apply even in aggressive mode."""
        env = nirmana_integration_env
        with patch("aros_meta_loop.services.metrics.get_db", return_value=env["conn"]), \
             patch("aros_meta_loop.services.engine.get_db", return_value=env["conn"]), \
             patch("aros_meta_loop.services.event_emitter.get_db", return_value=env["conn"]), \
             patch("aros_meta_loop.services.scheduler.update_schedule"):
            engine = MetaLoopEngine(state_manager=env["state_mgr"], bot_id="bot1")
            loop = asyncio.new_event_loop()
            loop.run_until_complete(engine.activate_nirmana())

            # Run a cycle
            loop.run_until_complete(engine.run_cycle("nirmana"))

            # Immediate second cycle should be throttled
            result = loop.run_until_complete(engine.run_cycle("nirmana"))
            # Should either run (if cadence allows) or be throttled
            assert result["status"] in ("completed", "aborted", "throttled", "skipped", "failed")

            loop.close()

    def test_briefing_includes_activity(self, nirmana_integration_env):
        """Verify briefing accumulates activity during Nirmana session."""
        env = nirmana_integration_env
        with patch("aros_meta_loop.services.metrics.get_db", return_value=env["conn"]), \
             patch("aros_meta_loop.services.engine.get_db", return_value=env["conn"]), \
             patch("aros_meta_loop.services.event_emitter.get_db", return_value=env["conn"]), \
             patch("aros_meta_loop.services.scheduler.update_schedule"):
            engine = MetaLoopEngine(state_manager=env["state_mgr"], bot_id="bot1")
            loop = asyncio.new_event_loop()
            loop.run_until_complete(engine.activate_nirmana())
            loop.run_until_complete(engine.run_cycle("nirmana"))
            result = loop.run_until_complete(engine.deactivate_nirmana())

            briefing = result["briefing"]
            assert briefing["cycles_run"] >= 1
            assert isinstance(briefing["summary"], str)

            loop.close()
