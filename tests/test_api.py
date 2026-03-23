"""Tests for Meta Loop API endpoints."""
import json
import sqlite3
import pytest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch, MagicMock, AsyncMock

from fastapi.testclient import TestClient


@pytest.fixture
def api_env(tmp_path):
    """Set up API test environment."""
    from aros_meta_loop.db.migrations import run_migrations
    from aros_meta_loop.config import META_COGNITION_DEFAULT, SELF_MODEL_DEFAULT, POLICY_DEFAULT, _write_default
    from aros_meta_loop.services.state_manager import StateManager
    from aros_meta_loop.services.engine import MetaLoopEngine
    from aros_meta_loop.routers.api import set_engine

    # DB
    db_path = tmp_path / "test.db"
    conn = sqlite3.connect(str(db_path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    run_migrations(conn)

    # State dir
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
         patch("aros_meta_loop.services.event_emitter.get_db", return_value=conn):
        engine = MetaLoopEngine(state_manager=state_mgr, bot_id="test-bot")
        set_engine(engine)
        yield {"conn": conn, "state_dir": state_dir, "engine": engine}


@pytest.fixture
def client(api_env):
    """Create test client without lifespan (engine already set up)."""
    from fastapi import FastAPI
    from aros_meta_loop.routers.api import router

    test_app = FastAPI()
    test_app.include_router(router)
    return TestClient(test_app)


class TestMetaLoopAPI:
    def test_get_status(self, client, api_env):
        with patch("aros_meta_loop.services.metrics.get_db", return_value=api_env["conn"]):
            resp = client.get("/api/meta-loop/status")
            assert resp.status_code == 200
            data = resp.json()
            assert "running" in data
            assert "bot_id" in data

    def test_trigger_cycle(self, client, api_env):
        with patch("aros_meta_loop.services.metrics.get_db", return_value=api_env["conn"]), \
             patch("aros_meta_loop.services.engine.get_db", return_value=api_env["conn"]):
            resp = client.post("/api/meta-loop/trigger", json={"trigger": "test"})
            assert resp.status_code == 200
            data = resp.json()
            assert data["status"] in ("started", "skipped")

    def test_inject_signal(self, client, api_env):
        resp = client.post("/api/meta-loop/signal", json={
            "source": "test", "priority": "normal", "payload": {"key": "val"}
        })
        assert resp.status_code == 200
        assert resp.json()["status"] == "queued"

    def test_evolution_log_empty(self, client, api_env):
        resp = client.get("/api/meta-loop/evolution-log")
        assert resp.status_code == 200
        assert resp.json()["count"] == 0

    def test_pending_approvals_empty(self, client, api_env):
        resp = client.get("/api/meta-loop/pending-approvals")
        assert resp.status_code == 200
        assert resp.json()["count"] == 0

    def test_approve_nonexistent(self, client, api_env):
        resp = client.post("/api/meta-loop/approve/nonexistent")
        assert resp.status_code == 404

    def test_receive_event(self, client, api_env):
        with patch("aros_meta_loop.services.event_emitter.get_db", return_value=api_env["conn"]):
            resp = client.post("/api/meta-loop/event", json={
                "bot_id": "test-bot",
                "event_type": "tool_call",
                "session_id": "s1",
                "data": {"tool": "grep"}
            })
            assert resp.status_code == 200
            assert resp.json()["status"] == "recorded"

    def test_nirmana_mode_switch(self, client, api_env):
        with patch("aros_meta_loop.services.scheduler.update_schedule"), \
             patch("aros_meta_loop.services.engine.get_db", return_value=api_env["conn"]):
            resp = client.post("/api/meta-loop/nirmana?activate=true")
            assert resp.status_code == 200
            assert resp.json()["mode"] == "aggressive"

            resp = client.post("/api/meta-loop/nirmana?activate=false")
            assert resp.status_code == 200
            assert resp.json()["mode"] == "balanced"
            assert "briefing" in resp.json()
