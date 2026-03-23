"""Tests for MetaLoopEngine Step 7: PLAN (autonomous task generation)."""
import json
import sqlite3
import tomllib
import pytest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch, MagicMock

from aros_meta_loop.db.migrations import run_migrations
from aros_meta_loop.services.engine import MetaLoopEngine
from aros_meta_loop.services.state_manager import StateManager
from aros_meta_loop.services.task_planner import PlannedTask, AuthorityLevel


@pytest.fixture
def engine_env(tmp_path):
    """Set up a complete engine test environment."""
    # DB
    db_path = tmp_path / "test.db"
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    run_migrations(conn)

    # Seed some events
    now = datetime.now(timezone.utc)
    events = [
        ("bot1", "tool_call", "s1", json.dumps({"tool": "grep", "success": True, "tokens_in": 1000, "tokens_out": 500}), now.isoformat()),
        ("bot1", "task_complete", "s1", json.dumps({"task_id": "t1", "tokens_consumed": 3000, "duration_seconds": 60, "retries": 0, "complexity": 3}), now.isoformat()),
    ]
    for bot_id, event_type, session_id, data, created_at in events:
        conn.execute(
            "INSERT INTO meta_events (bot_id, event_type, session_id, data, created_at) VALUES (?, ?, ?, ?, ?)",
            (bot_id, event_type, session_id, data, created_at),
        )
    conn.commit()

    # State dir
    from aros_meta_loop.config import META_COGNITION_DEFAULT, SELF_MODEL_DEFAULT, POLICY_DEFAULT, _write_default
    state_dir = tmp_path / ".aros"
    state_dir.mkdir()
    for sub in ("data", "signals", "pending-review", "state"):
        (state_dir / sub).mkdir()
    _write_default(state_dir / "meta-cognition.toml", META_COGNITION_DEFAULT)
    _write_default(state_dir / "self-model.toml", SELF_MODEL_DEFAULT)
    _write_default(state_dir / "policy.toml", POLICY_DEFAULT)

    state_mgr = StateManager(state_dir=state_dir)

    return {"conn": conn, "state_dir": state_dir, "state_mgr": state_mgr, "db_path": db_path}


def _set_aggressive_mode(engine_env):
    """Helper to set cadence mode to aggressive."""
    config_path = engine_env["state_dir"] / "meta-cognition.toml"
    with open(config_path, "rb") as f:
        full_config = tomllib.load(f)
    full_config["cadence"]["mode"] = "aggressive"
    engine_env["state_mgr"].write_snapshot("meta-cognition.toml", full_config)


@pytest.fixture
def engine(engine_env):
    """Create a MetaLoopEngine with test environment."""
    with patch("aros_meta_loop.services.metrics.get_db", return_value=engine_env["conn"]), \
         patch("aros_meta_loop.services.engine.get_db", return_value=engine_env["conn"]), \
         patch("aros_meta_loop.services.event_emitter.get_db", return_value=engine_env["conn"]):
        eng = MetaLoopEngine(state_manager=engine_env["state_mgr"], bot_id="bot1")
        yield eng


MOCK_TASKS = [
    PlannedTask(
        title="Optimize aros-kernel build times",
        description="Profile cargo build, identify slow compilation units",
        target_project="~/Projects/aros-kernel",
        authority_level=AuthorityLevel.GREEN,
        estimated_minutes=20,
        goal_source="G5_ambitious",
    ),
    PlannedTask(
        title="Add event bus to aros-kernel",
        description="Implement event bus for hardware awareness",
        target_project="~/Projects/aros-kernel",
        authority_level=AuthorityLevel.YELLOW,
        estimated_minutes=30,
        goal_source="G5_ambitious",
    ),
]


class TestPlanStepSkipsInBalancedMode:
    """_plan_tasks should return empty list when cadence mode is balanced (default)."""

    def test_plan_step_skips_in_balanced_mode(self, engine):
        perceive_data = {
            "l2_scores": {
                "G5_ambitious": 0.1,
                "below_threshold": ["G5_ambitious"],
            },
        }
        result = engine._plan_tasks(perceive_data)
        assert result == []


class TestPlanStepGeneratesInAggressiveMode:
    """_plan_tasks should generate tasks when in aggressive mode with low goals."""

    def test_plan_step_generates_in_aggressive_mode(self, engine, engine_env):
        _set_aggressive_mode(engine_env)

        perceive_data = {
            "l2_scores": {
                "G5_ambitious": 0.1,
                "below_threshold": ["G5_ambitious"],
            },
        }

        with patch("aros_meta_loop.services.engine.TaskPlanner") as MockPlanner:
            MockPlanner.return_value.generate_tasks.return_value = MOCK_TASKS
            result = engine._plan_tasks(perceive_data)

        assert len(result) == 2
        # Verify task structure
        task = result[0]
        assert task["title"] == "Optimize aros-kernel build times"
        assert task["description"] == "Profile cargo build, identify slow compilation units"
        assert task["target_project"] == "~/Projects/aros-kernel"
        assert task["authority_level"] == "GREEN"
        assert task["estimated_minutes"] == 20
        assert task["goal_source"] == "G5_ambitious"

        # Verify second task is YELLOW
        assert result[1]["authority_level"] == "YELLOW"
        assert result[1]["title"] == "Add event bus to aros-kernel"


class TestPlanStepSkipsWhenAllAboveThreshold:
    """_plan_tasks should return empty list when no goals are below threshold."""

    def test_plan_step_skips_when_all_above_threshold(self, engine, engine_env):
        _set_aggressive_mode(engine_env)

        perceive_data = {
            "l2_scores": {
                "G1_truthful": 0.9,
                "G5_ambitious": 0.8,
                "below_threshold": [],
            },
        }
        # TaskPlanner should never be instantiated since below_threshold is empty
        with patch("aros_meta_loop.services.engine.TaskPlanner") as MockPlanner:
            result = engine._plan_tasks(perceive_data)
            MockPlanner.assert_not_called()

        assert result == []
