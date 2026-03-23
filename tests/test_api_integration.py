"""Integration tests for API endpoints and error handling."""
import asyncio
import json
import sqlite3
import pytest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch, MagicMock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from aros_meta_loop.db.migrations import run_migrations
from aros_meta_loop.services.engine import MetaLoopEngine
from aros_meta_loop.services.state_manager import StateManager
from aros_meta_loop.routers.api import router, set_engine
from aros_meta_loop.config import META_COGNITION_DEFAULT, SELF_MODEL_DEFAULT, POLICY_DEFAULT, _write_default


@pytest.fixture
def api_integration_env(tmp_path):
    db_path = tmp_path / "test.db"
    conn = sqlite3.connect(str(db_path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    run_migrations(conn)

    # Seed events
    now = datetime.now(timezone.utc)
    for i in range(5):
        conn.execute(
            "INSERT INTO meta_events (bot_id, event_type, session_id, data, created_at) VALUES (?, ?, ?, ?, ?)",
            ("test-bot", "tool_call", "s1", json.dumps({"tool": f"tool{i}", "success": True, "tokens_in": 1000, "tokens_out": 500}), now.isoformat())
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

    with patch("aros_meta_loop.services.metrics.get_db", return_value=conn), \
         patch("aros_meta_loop.services.engine.get_db", return_value=conn), \
         patch("aros_meta_loop.services.event_emitter.get_db", return_value=conn), \
         patch("aros_meta_loop.services.scheduler.update_schedule"):
        engine = MetaLoopEngine(state_manager=state_mgr, bot_id="test-bot")
        set_engine(engine)

        app = FastAPI()
        app.include_router(router)
        client = TestClient(app)

        yield {"client": client, "conn": conn, "state_dir": state_dir, "engine": engine, "state_mgr": state_mgr}


class TestAPIIntegration:
    def test_status_returns_meta_goal_scores(self, api_integration_env):
        resp = api_integration_env["client"].get("/api/meta-loop/status")
        assert resp.status_code == 200
        data = resp.json()
        assert "meta_goal_scores" in data

    def test_trigger_and_check_status(self, api_integration_env):
        env = api_integration_env
        resp = env["client"].post("/api/meta-loop/trigger", json={"trigger": "test"})
        assert resp.status_code == 200
        assert resp.json()["status"] in ("started", "skipped")

    def test_signal_inject_and_drain(self, api_integration_env):
        env = api_integration_env
        # Inject signal
        resp = env["client"].post("/api/meta-loop/signal", json={
            "source": "test", "priority": "urgent", "payload": {"msg": "hello"}
        })
        assert resp.status_code == 200

        # Verify signal exists
        assert env["state_mgr"].has_urgent()

    def test_event_webhook(self, api_integration_env):
        env = api_integration_env
        resp = env["client"].post("/api/meta-loop/event", json={
            "bot_id": "test-bot", "event_type": "human_feedback",
            "session_id": "s1", "data": {"correction": "use X instead"}
        })
        assert resp.status_code == 200

        # Verify event in DB
        row = env["conn"].execute(
            "SELECT * FROM meta_events WHERE event_type='human_feedback'"
        ).fetchone()
        assert row is not None

    def test_approve_nonexistent_change(self, api_integration_env):
        resp = api_integration_env["client"].post("/api/meta-loop/approve/nonexistent123")
        assert resp.status_code == 404

    def test_approve_pending_change(self, api_integration_env):
        env = api_integration_env
        # Create a pending change
        review_dir = env["state_dir"] / "pending-review"
        change_data = {
            "change_id": "test123",
            "section": "harness",
            "key": "retry_limit",
            "old_value": "3",
            "new_value": "4",
            "reason": "test",
            "status": "pending_review",
        }
        (review_dir / "test123.json").write_text(json.dumps(change_data))

        # Approve it
        resp = env["client"].post("/api/meta-loop/approve/test123")
        assert resp.status_code == 200
        assert resp.json()["status"] == "approved"

        # Verify file removed
        assert not (review_dir / "test123.json").exists()

        # Verify policy updated
        policy = env["state_mgr"].read_policy()
        assert policy["harness"]["retry_limit"] == 4

    def test_pending_approvals_list(self, api_integration_env):
        env = api_integration_env
        review_dir = env["state_dir"] / "pending-review"
        for i in range(3):
            (review_dir / f"ch{i}.json").write_text(json.dumps({
                "change_id": f"ch{i}", "section": "harness", "key": f"key{i}",
                "status": "pending_review"
            }))

        resp = env["client"].get("/api/meta-loop/pending-approvals")
        assert resp.status_code == 200
        assert resp.json()["count"] == 3

    def test_evolution_log_after_cycle(self, api_integration_env):
        env = api_integration_env
        # Add some evolution entries
        env["state_mgr"].append_evolution({"cycle": 1, "action": "POLICY_UPDATE"})
        env["state_mgr"].append_evolution({"cycle": 2, "action": "NO_ACTION"})

        resp = env["client"].get("/api/meta-loop/evolution-log?limit=10")
        assert resp.status_code == 200
        assert resp.json()["count"] == 2

    def test_cadence_switch_via_nirmana(self, api_integration_env):
        env = api_integration_env
        # Activate aggressive
        resp = env["client"].post("/api/meta-loop/nirmana?activate=true")
        assert resp.status_code == 200
        assert resp.json()["mode"] == "aggressive"

        # Verify config changed
        cadence = env["state_mgr"].read_cadence()
        assert cadence["mode"] == "aggressive"

        # Deactivate
        resp = env["client"].post("/api/meta-loop/nirmana?activate=false")
        assert resp.status_code == 200
        assert resp.json()["mode"] == "balanced"

    def test_invalid_signal_payload(self, api_integration_env):
        """Signal with missing required fields should fail."""
        resp = api_integration_env["client"].post("/api/meta-loop/signal", json={})
        assert resp.status_code == 422  # Pydantic validation error
