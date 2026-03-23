"""Tests for Nirmana autonomous driver integration."""
import asyncio
import json
import sqlite3
import pytest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch, MagicMock

from aros_meta_loop.db.migrations import run_migrations
from aros_meta_loop.services.engine import MetaLoopEngine
from aros_meta_loop.services.state_manager import StateManager


@pytest.fixture
def nirmana_env(tmp_path):
    from aros_meta_loop.config import META_COGNITION_DEFAULT, SELF_MODEL_DEFAULT, POLICY_DEFAULT, _write_default

    db_path = tmp_path / "test.db"
    conn = sqlite3.connect(str(db_path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    run_migrations(conn)

    state_dir = tmp_path / ".aros"
    state_dir.mkdir()
    for sub in ("data", "signals", "pending-review", "state"):
        (state_dir / sub).mkdir()
    _write_default(state_dir / "meta-cognition.toml", META_COGNITION_DEFAULT)
    _write_default(state_dir / "self-model.toml", SELF_MODEL_DEFAULT)
    _write_default(state_dir / "policy.toml", POLICY_DEFAULT)

    state_mgr = StateManager(state_dir=state_dir)
    return {"conn": conn, "state_dir": state_dir, "state_mgr": state_mgr}


@pytest.fixture
def nirmana_engine(nirmana_env):
    with patch("aros_meta_loop.services.metrics.get_db", return_value=nirmana_env["conn"]), \
         patch("aros_meta_loop.services.engine.get_db", return_value=nirmana_env["conn"]), \
         patch("aros_meta_loop.services.event_emitter.get_db", return_value=nirmana_env["conn"]):
        eng = MetaLoopEngine(state_manager=nirmana_env["state_mgr"], bot_id="test")
        yield eng


class TestNirmanaDriver:
    def test_activate_switches_to_aggressive(self, nirmana_engine, nirmana_env):
        with patch("aros_meta_loop.services.scheduler.update_schedule"):
            result = asyncio.get_event_loop().run_until_complete(
                nirmana_engine.activate_nirmana()
            )
            assert result["mode"] == "aggressive"
            assert nirmana_engine._nirmana_mode is True

            # Verify TOML updated
            cadence = nirmana_engine.state.read_cadence()
            assert cadence["mode"] == "aggressive"

    def test_deactivate_restores_balanced(self, nirmana_engine, nirmana_env):
        with patch("aros_meta_loop.services.scheduler.update_schedule"):
            asyncio.get_event_loop().run_until_complete(nirmana_engine.activate_nirmana())
            result = asyncio.get_event_loop().run_until_complete(nirmana_engine.deactivate_nirmana())
            assert result["mode"] == "balanced"
            assert nirmana_engine._nirmana_mode is False
            assert "briefing" in result

    def test_briefing_generated(self, nirmana_engine, nirmana_env):
        with patch("aros_meta_loop.services.scheduler.update_schedule"), \
             patch("aros_meta_loop.services.metrics.get_db", return_value=nirmana_env["conn"]), \
             patch("aros_meta_loop.services.engine.get_db", return_value=nirmana_env["conn"]):
            asyncio.get_event_loop().run_until_complete(nirmana_engine.activate_nirmana())
            result = asyncio.get_event_loop().run_until_complete(nirmana_engine.deactivate_nirmana())
            briefing = result["briefing"]
            assert "cycles_run" in briefing
            assert "pending_reviews" in briefing
            assert "summary" in briefing

    def test_nirmana_green_decisions_logged(self, nirmana_engine, nirmana_env):
        with patch("aros_meta_loop.services.scheduler.update_schedule"):
            asyncio.get_event_loop().run_until_complete(nirmana_engine.activate_nirmana())

            # Simulate a GREEN decision in briefing
            nirmana_engine._nirmana_briefing.append({
                "type": "GREEN", "change": "harness.retry_limit: 3 -> 4"
            })

            briefing = nirmana_engine._generate_briefing()
            assert briefing["decisions_made"] == 1

    def test_nirmana_mode_api_endpoint(self, nirmana_env):
        """Test the /nirmana API endpoint."""
        from fastapi.testclient import TestClient
        from fastapi import FastAPI
        from aros_meta_loop.routers.api import router, set_engine

        with patch("aros_meta_loop.services.metrics.get_db", return_value=nirmana_env["conn"]), \
             patch("aros_meta_loop.services.engine.get_db", return_value=nirmana_env["conn"]), \
             patch("aros_meta_loop.services.event_emitter.get_db", return_value=nirmana_env["conn"]), \
             patch("aros_meta_loop.services.scheduler.update_schedule"):
            engine = MetaLoopEngine(state_manager=nirmana_env["state_mgr"], bot_id="test")
            set_engine(engine)

            app = FastAPI()
            app.include_router(router)
            client = TestClient(app)

            resp = client.post("/api/meta-loop/nirmana?activate=true")
            assert resp.status_code == 200
            assert resp.json()["mode"] == "aggressive"

            resp = client.post("/api/meta-loop/nirmana?activate=false")
            assert resp.status_code == 200
            assert resp.json()["mode"] == "balanced"
            assert "briefing" in resp.json()
