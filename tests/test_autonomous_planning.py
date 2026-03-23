"""Integration tests for MetaLoop autonomous task generation — full cycle flow.

Tests cover:
1. Full cycle in aggressive mode generates planned tasks
2. GREEN tasks dispatched via HarnessTrigger
3. YELLOW tasks queued to pending approvals (not dispatched)
4. Approving a pending YELLOW task triggers harness dispatch
5. Evolution log records task generation entries
6. Full cycle in balanced mode skips task planning entirely
"""
import json
import sqlite3
import tomllib
import pytest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch, MagicMock

from aros_meta_loop.config import (
    META_COGNITION_DEFAULT, SELF_MODEL_DEFAULT, POLICY_DEFAULT, _write_default,
)
from aros_meta_loop.db.migrations import run_migrations
from aros_meta_loop.services.engine import MetaLoopEngine
from aros_meta_loop.services.harness_trigger import HarnessTrigger
from aros_meta_loop.services.state_manager import StateManager
from aros_meta_loop.services.task_planner import (
    AuthorityLevel,
    PlannedTask,
    TaskPlanner,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def full_env(tmp_path):
    """Complete test environment: DB with seeded events, state dir with configs."""
    # Database
    db_path = tmp_path / "test.db"
    conn = sqlite3.connect(str(db_path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    run_migrations(conn)

    now = datetime.now(timezone.utc)
    events = [
        ("bot1", "tool_call", "s1",
         json.dumps({"tool": "grep", "success": True, "tokens_in": 1000, "tokens_out": 500}),
         now.isoformat()),
        ("bot1", "task_complete", "s1",
         json.dumps({"task_id": "t1", "tokens_consumed": 3000,
                      "duration_seconds": 60, "retries": 0, "complexity": 3}),
         now.isoformat()),
    ]
    for bot_id, event_type, session_id, data, created_at in events:
        conn.execute(
            "INSERT INTO meta_events (bot_id, event_type, session_id, data, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (bot_id, event_type, session_id, data, created_at),
        )
    conn.commit()

    # State directory
    state_dir = tmp_path / ".aros"
    state_dir.mkdir()
    for sub in ("data", "signals", "pending-review", "state"):
        (state_dir / sub).mkdir()
    _write_default(state_dir / "meta-cognition.toml", META_COGNITION_DEFAULT)
    _write_default(state_dir / "self-model.toml", SELF_MODEL_DEFAULT)
    _write_default(state_dir / "policy.toml", POLICY_DEFAULT)

    state_mgr = StateManager(state_dir=state_dir)

    return {
        "conn": conn,
        "db_path": db_path,
        "state_dir": state_dir,
        "state_mgr": state_mgr,
    }


def _set_cadence_mode(full_env, mode: str):
    """Helper: set cadence.mode in meta-cognition.toml."""
    config_path = full_env["state_dir"] / "meta-cognition.toml"
    with open(config_path, "rb") as f:
        full_config = tomllib.load(f)
    full_config["cadence"]["mode"] = mode
    full_env["state_mgr"].write_snapshot("meta-cognition.toml", full_config)


def _make_engine(full_env) -> MetaLoopEngine:
    """Create a MetaLoopEngine patched to use the test DB."""
    with patch("aros_meta_loop.services.metrics.get_db", return_value=full_env["conn"]), \
         patch("aros_meta_loop.services.engine.get_db", return_value=full_env["conn"]), \
         patch("aros_meta_loop.services.event_emitter.get_db", return_value=full_env["conn"]):
        return MetaLoopEngine(state_manager=full_env["state_mgr"], bot_id="bot1")


# Reusable mock tasks (mixed GREEN + YELLOW)
MOCK_GREEN_TASK = PlannedTask(
    title="Optimize aros-kernel build times",
    description="Profile cargo build, identify slow compilation units",
    target_project="~/Projects/aros-kernel",
    authority_level=AuthorityLevel.GREEN,
    estimated_minutes=20,
    goal_source="G5_ambitious",
)

MOCK_YELLOW_TASK = PlannedTask(
    title="Improve aros-kernel hardware awareness",
    description="Add event bus, throttle, or memory audit based on gap analysis",
    target_project="~/Projects/aros-kernel",
    authority_level=AuthorityLevel.YELLOW,
    estimated_minutes=30,
    goal_source="G5_ambitious",
)

MOCK_MIXED_TASKS = [MOCK_GREEN_TASK, MOCK_YELLOW_TASK]


# ---------------------------------------------------------------------------
# Test 1: Full cycle generates tasks in aggressive mode
# ---------------------------------------------------------------------------

class TestFullCycleGeneratesTasksInAggressiveMode:
    """Running a full engine cycle in aggressive mode with low G5 should
    populate cycle_log with planned_tasks and reach step 7."""

    @pytest.mark.asyncio
    async def test_full_cycle_generates_tasks_in_aggressive_mode(self, full_env):
        _set_cadence_mode(full_env, "aggressive")

        engine = _make_engine(full_env)

        # Mock TaskPlanner to return known tasks
        with patch("aros_meta_loop.services.engine.TaskPlanner") as MockPlanner:
            MockPlanner.return_value.generate_tasks.return_value = MOCK_MIXED_TASKS

            # Mock verify_last_dispatch to avoid hitting live gateway
            with patch.object(
                HarnessTrigger, "verify_last_dispatch",
                return_value={"status": "idle", "completed": False, "details": "No active task"},
            ):
                # Mock L2 evaluator to report G5 below threshold
                with patch.object(
                    engine.evaluator, "evaluate",
                    return_value={
                        "G1_truthful": 0.9,
                        "G5_ambitious": 0.1,
                        "below_threshold": ["G5_ambitious"],
                    },
                ):
                    result = await engine.run_cycle("scheduled")

        assert result["status"] == "completed"
        assert result.get("steps_completed", 0) >= 7
        planned = result.get("planned_tasks", [])
        assert len(planned) >= 1
        # Verify structure
        titles = [t["title"] for t in planned]
        assert "Optimize aros-kernel build times" in titles


# ---------------------------------------------------------------------------
# Test 2: GREEN tasks dispatched via HarnessTrigger
# ---------------------------------------------------------------------------

class TestGreenTasksDispatchedViaTrigger:
    """TaskPlanner generates tasks, GREEN ones are dispatched via
    HarnessTrigger with the correct Nirmana-persona prompt."""

    def test_green_tasks_dispatched_via_trigger(self):
        planner = TaskPlanner(backlog_path=Path("/nonexistent/backlog.md"))
        tasks = planner.generate_tasks(
            scores={"G5_ambitious": 0.1},
            below_threshold=["G5_ambitious"],
        )
        # Fallback (no backlog) produces a YELLOW task; add an explicit GREEN
        green_tasks = [t for t in tasks if t.authority_level == AuthorityLevel.GREEN]
        if not green_tasks:
            green_tasks = [MOCK_GREEN_TASK]

        trigger = HarnessTrigger(gateway_url="http://test:8000", chat_id="-999")

        # Mock httpx
        mock_post_resp = MagicMock()
        mock_post_resp.status_code = 200
        mock_get_resp = MagicMock()
        mock_get_resp.status_code = 200
        mock_get_resp.json.return_value = []  # no running sessions

        mock_client = MagicMock()
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client.get.return_value = mock_get_resp
        mock_client.post.return_value = mock_post_resp

        with patch("aros_meta_loop.services.harness_trigger.httpx.Client",
                    return_value=mock_client):
            result = trigger.trigger_harness_loop(green_tasks)

        assert result["status"] == "dispatched"
        assert result["task_count"] == len(green_tasks)

        # Verify the POST payload contains the Nirmana persona reference
        call_args = mock_client.post.call_args
        payload = call_args.kwargs.get("json") or call_args[1].get("json")
        assert "Nirmana" in payload["message"]
        assert "PERSONA.md" in payload["message"]


# ---------------------------------------------------------------------------
# Test 3: YELLOW tasks queued to approvals — NOT dispatched
# ---------------------------------------------------------------------------

class TestYellowTasksQueuedToApprovals:
    """YELLOW tasks are added to pending approvals via StateManager.
    They must NOT be auto-dispatched to the gateway."""

    def test_yellow_tasks_queued_to_approvals(self, full_env):
        state_mgr = full_env["state_mgr"]
        # Create explicit YELLOW tasks (feature-labeled issues would be YELLOW)
        yellow_tasks = [
            PlannedTask(
                title="Add new dashboard feature",
                description="Design and implement a metrics dashboard",
                target_project="~/Projects/aros-kernel",
                authority_level=AuthorityLevel.YELLOW,
                estimated_minutes=30,
                goal_source="G5_ambitious",
                source="test",
            ),
        ]

        # Queue each YELLOW task
        approval_ids = []
        for t in yellow_tasks:
            task_dict = {
                "title": t.title,
                "description": t.description,
                "target_project": t.target_project,
                "authority_level": t.authority_level.value,
                "estimated_minutes": t.estimated_minutes,
                "goal_source": t.goal_source,
            }
            aid = state_mgr.add_pending_approval({
                "task": task_dict,
                "cycle_num": 42,
            })
            approval_ids.append(aid)

        # Verify they appear in pending approvals
        approvals = state_mgr.get_pending_approvals()
        assert len(approvals) == len(yellow_tasks)
        for a in approvals:
            assert a["status"] == "pending"
            assert a["task"]["authority_level"] == "YELLOW"

        # Verify HarnessTrigger refuses to dispatch YELLOW-only list
        trigger = HarnessTrigger(gateway_url="http://test:8000", chat_id="-999")
        with patch.object(trigger, "is_harness_running", return_value=False):
            dispatch_result = trigger.trigger_harness_loop(yellow_tasks)
        assert dispatch_result["status"] == "skipped"
        assert dispatch_result["reason"] == "no GREEN tasks to auto-dispatch"


# ---------------------------------------------------------------------------
# Test 4: Approve pending YELLOW task triggers harness dispatch
# ---------------------------------------------------------------------------

class TestApprovePendingTriggersHarness:
    """Approving a YELLOW task should make it dispatchable via HarnessTrigger
    (re-classified as GREEN after approval)."""

    def test_approve_pending_triggers_harness(self, full_env):
        state_mgr = full_env["state_mgr"]

        task_dict = {
            "title": "Refactor event bus",
            "description": "Improve event bus architecture",
            "target_project": "~/Projects/aros-kernel",
            "authority_level": "YELLOW",
            "estimated_minutes": 25,
            "goal_source": "G5_ambitious",
        }
        approval_id = state_mgr.add_pending_approval({
            "task": task_dict,
            "cycle_num": 7,
        })

        # Approve
        result = state_mgr.approve_task(approval_id)
        assert result is not None
        assert result["status"] == "approved"

        # Convert approved task to PlannedTask (as the API endpoint does)
        approved = result["task"]
        planned = PlannedTask(
            title=approved["title"],
            description=approved["description"],
            target_project=approved["target_project"],
            authority_level=AuthorityLevel.GREEN,  # Approved → GREEN
            estimated_minutes=approved["estimated_minutes"],
            goal_source=approved["goal_source"],
        )

        # Mock gateway POST
        trigger = HarnessTrigger(gateway_url="http://test:8000", chat_id="-999")
        mock_post_resp = MagicMock()
        mock_post_resp.status_code = 200
        mock_get_resp = MagicMock()
        mock_get_resp.status_code = 200
        mock_get_resp.json.return_value = []

        mock_client = MagicMock()
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client.get.return_value = mock_get_resp
        mock_client.post.return_value = mock_post_resp

        with patch("aros_meta_loop.services.harness_trigger.httpx.Client",
                    return_value=mock_client):
            dispatch_result = trigger.trigger_harness_loop([planned])

        assert dispatch_result["status"] == "dispatched"
        assert dispatch_result["task_count"] == 1
        mock_client.post.assert_called_once()


# ---------------------------------------------------------------------------
# Test 5: Evolution log records task generation
# ---------------------------------------------------------------------------

class TestEvolutionLogRecordsGeneration:
    """state_manager.log_task_generation() writes an entry to evolution-log.jsonl
    with the correct structure."""

    def test_evolution_log_records_generation(self, full_env):
        state_mgr = full_env["state_mgr"]

        sample_tasks = [
            {
                "title": "Optimize build",
                "authority_level": "GREEN",
                "goal_source": "G2_efficient",
            },
            {
                "title": "Add event bus",
                "authority_level": "YELLOW",
                "goal_source": "G5_ambitious",
            },
        ]
        trigger_results = {
            "green_dispatched": 1,
            "yellow_queued": 1,
            "skipped": 0,
        }

        state_mgr.log_task_generation(
            cycle_num=15,
            tasks=sample_tasks,
            trigger_results=trigger_results,
        )

        entries = state_mgr.read_evolution_log(limit=100)
        task_gen_entries = [e for e in entries if e.get("type") == "task_generation"]

        assert len(task_gen_entries) >= 1
        entry = task_gen_entries[-1]
        assert entry["cycle_num"] == 15
        assert len(entry["tasks_generated"]) == 2
        assert entry["tasks_generated"][0]["title"] == "Optimize build"
        assert entry["tasks_generated"][1]["authority_level"] == "YELLOW"
        assert entry["trigger_results"]["green_dispatched"] == 1
        assert entry["trigger_results"]["yellow_queued"] == 1
        assert "timestamp" in entry


# ---------------------------------------------------------------------------
# Test 6: Full cycle skips planning in balanced mode
# ---------------------------------------------------------------------------

class TestFullCycleSkipsInBalancedMode:
    """In balanced (default) mode, the PLAN step should be skipped entirely —
    no planned_tasks in cycle_log."""

    @pytest.mark.asyncio
    async def test_full_cycle_skips_in_balanced_mode(self, full_env):
        # balanced is the default — no need to set it explicitly
        engine = _make_engine(full_env)

        # Mock L2 with low G5 — but balanced mode should still skip planning
        with patch.object(
            engine.evaluator, "evaluate",
            return_value={
                "G1_truthful": 0.9,
                "G5_ambitious": 0.1,
                "below_threshold": ["G5_ambitious"],
            },
        ):
            result = await engine.run_cycle("scheduled")

        assert result["status"] == "completed"
        # No planned_tasks should be present
        assert "planned_tasks" not in result or result.get("planned_tasks") == []
