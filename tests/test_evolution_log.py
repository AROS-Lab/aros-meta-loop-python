"""Tests for evolution log integration and review endpoints."""
import json
import sqlite3
import pytest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch, MagicMock

from fastapi.testclient import TestClient

from aros_meta_loop.services.state_manager import StateManager


@pytest.fixture
def state_mgr(tmp_path):
    """StateManager with temp directory."""
    from aros_meta_loop.config import (
        META_COGNITION_DEFAULT, SELF_MODEL_DEFAULT, POLICY_DEFAULT, _write_default,
    )
    state_dir = tmp_path / ".aros"
    state_dir.mkdir()
    for sub in ("data", "signals", "pending-review", "state"):
        (state_dir / sub).mkdir()
    _write_default(state_dir / "meta-cognition.toml", META_COGNITION_DEFAULT)
    _write_default(state_dir / "self-model.toml", SELF_MODEL_DEFAULT)
    _write_default(state_dir / "policy.toml", POLICY_DEFAULT)
    return StateManager(state_dir=state_dir)


@pytest.fixture
def api_env(tmp_path):
    """Set up API test environment."""
    from aros_meta_loop.db.migrations import run_migrations
    from aros_meta_loop.config import (
        META_COGNITION_DEFAULT, SELF_MODEL_DEFAULT, POLICY_DEFAULT, _write_default,
    )
    from aros_meta_loop.services.engine import MetaLoopEngine
    from aros_meta_loop.routers.api import set_engine

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

    with patch("aros_meta_loop.services.metrics.get_db", return_value=conn), \
         patch("aros_meta_loop.services.engine.get_db", return_value=conn), \
         patch("aros_meta_loop.services.event_emitter.get_db", return_value=conn):
        engine = MetaLoopEngine(state_manager=state_mgr, bot_id="test-bot")
        set_engine(engine)
        yield {"conn": conn, "state_dir": state_dir, "engine": engine, "state_mgr": state_mgr}


@pytest.fixture
def client(api_env):
    """Create test client without lifespan."""
    from fastapi import FastAPI
    from aros_meta_loop.routers.api import router

    test_app = FastAPI()
    test_app.include_router(router)
    return TestClient(test_app)


def _sample_tasks(n=3):
    """Generate sample task dicts."""
    tasks = []
    for i in range(n):
        level = "GREEN" if i % 2 == 0 else "YELLOW"
        tasks.append({
            "title": f"Task {i}",
            "description": f"Description {i}",
            "target_project": "aros-meta-loop",
            "authority_level": level,
            "estimated_minutes": 15,
            "goal_source": "G2_efficient",
        })
    return tasks


class TestLogTaskGeneration:
    def test_log_task_generation(self, state_mgr):
        """Log entry, read back, verify fields."""
        tasks = _sample_tasks(2)
        trigger_results = {"green_dispatched": 1, "yellow_queued": 1, "skipped": 0}

        state_mgr.log_task_generation(cycle_num=5, tasks=tasks, trigger_results=trigger_results)

        entries = state_mgr.read_evolution_log(limit=10)
        assert len(entries) == 1

        entry = entries[0]
        assert entry["type"] == "task_generation"
        assert entry["cycle_num"] == 5
        assert "timestamp" in entry
        assert len(entry["tasks_generated"]) == 2
        assert entry["trigger_results"]["green_dispatched"] == 1
        assert entry["trigger_results"]["yellow_queued"] == 1

    def test_read_evolution_log_limit(self, state_mgr):
        """Write 30 entries, read 20, verify only last 20 returned."""
        for i in range(30):
            state_mgr.log_task_generation(
                cycle_num=i,
                tasks=[{"title": f"Task-{i}", "authority_level": "GREEN"}],
                trigger_results={"green_dispatched": 1, "yellow_queued": 0, "skipped": 0},
            )

        entries = state_mgr.read_evolution_log(limit=20)
        assert len(entries) == 20
        # Should be the last 20 (cycle_num 10-29)
        assert entries[0]["cycle_num"] == 10
        assert entries[-1]["cycle_num"] == 29

    def test_empty_evolution_log(self, state_mgr):
        """No log file, verify empty result."""
        entries = state_mgr.read_evolution_log(limit=10)
        assert entries == []


class TestEvolutionLogEndpoint:
    def test_evolution_log_endpoint(self, client, api_env):
        """Call GET /api/meta-loop/evolution-log, verify response."""
        state_mgr = api_env["state_mgr"]
        tasks = _sample_tasks(2)
        state_mgr.log_task_generation(
            cycle_num=1,
            tasks=tasks,
            trigger_results={"green_dispatched": 1, "yellow_queued": 1, "skipped": 0},
        )

        resp = client.get("/api/meta-loop/evolution-log")
        assert resp.status_code == 200

        data = resp.json()
        assert data["count"] == 1
        assert len(data["entries"]) == 1
        assert data["entries"][0]["type"] == "task_generation"

    def test_evolution_log_endpoint_empty(self, client, api_env):
        """No log entries, verify empty response."""
        resp = client.get("/api/meta-loop/evolution-log")
        assert resp.status_code == 200
        data = resp.json()
        assert data["count"] == 0
        assert data["entries"] == []

    def test_evolution_log_summary(self, client, api_env):
        """Write sample entries, call summary, verify counts."""
        state_mgr = api_env["state_mgr"]

        # Cycle 1: 2 GREEN, 1 YELLOW
        state_mgr.log_task_generation(
            cycle_num=1,
            tasks=[
                {"title": "A", "authority_level": "GREEN"},
                {"title": "B", "authority_level": "GREEN"},
                {"title": "C", "authority_level": "YELLOW"},
            ],
            trigger_results={"green_dispatched": 2, "yellow_queued": 1, "skipped": 0},
        )

        # Cycle 2: 1 GREEN
        state_mgr.log_task_generation(
            cycle_num=2,
            tasks=[
                {"title": "D", "authority_level": "GREEN"},
            ],
            trigger_results={"green_dispatched": 1, "yellow_queued": 0, "skipped": 0},
        )

        resp = client.get("/api/meta-loop/evolution-log/summary")
        assert resp.status_code == 200

        data = resp.json()
        assert data["total_cycles_with_tasks"] == 2
        assert data["total_tasks_generated"] == 4
        assert data["total_dispatched"] == 3
        assert data["total_queued"] == 1
        assert data["by_authority"]["GREEN"] == 3
        assert data["by_authority"]["YELLOW"] == 1

    def test_evolution_log_summary_empty(self, client, api_env):
        """No entries, summary returns zeros."""
        resp = client.get("/api/meta-loop/evolution-log/summary")
        assert resp.status_code == 200

        data = resp.json()
        assert data["total_cycles_with_tasks"] == 0
        assert data["total_tasks_generated"] == 0
        assert data["total_dispatched"] == 0
        assert data["total_queued"] == 0
        assert data["by_authority"]["GREEN"] == 0
        assert data["by_authority"]["YELLOW"] == 0
