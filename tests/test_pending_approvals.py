"""Tests for pending approval queue (YELLOW tasks)."""
import json
import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock

from aros_meta_loop.services.state_manager import StateManager
from aros_meta_loop.services.harness_trigger import HarnessTrigger
from aros_meta_loop.services.task_planner import PlannedTask, AuthorityLevel


@pytest.fixture
def state_mgr(tmp_path):
    """StateManager with temp directory."""
    state_dir = tmp_path / ".aros"
    state_dir.mkdir()
    return StateManager(state_dir=state_dir)


def _make_yellow_task_dict(title: str = "Refactor auth module") -> dict:
    """Helper to create a YELLOW task dict for approval."""
    return {
        "title": title,
        "description": "Refactor the auth module for better separation of concerns",
        "target_project": "~/Projects/aros-kernel",
        "authority_level": "YELLOW",
        "estimated_minutes": 30,
        "goal_source": "G2_efficient",
    }


class TestAddPendingApproval:
    def test_add_pending_approval(self, state_mgr):
        """Add an approval, verify it appears in the list."""
        task_dict = _make_yellow_task_dict()
        approval_id = state_mgr.add_pending_approval({
            "task": task_dict,
            "cycle_num": 5,
        })

        assert approval_id is not None
        assert len(approval_id) == 12

        approvals = state_mgr.get_pending_approvals()
        assert len(approvals) == 1
        assert approvals[0]["id"] == approval_id
        assert approvals[0]["status"] == "pending"
        assert approvals[0]["task"]["title"] == "Refactor auth module"
        assert approvals[0]["cycle_num"] == 5
        assert "created_at" in approvals[0]

    def test_add_multiple_approvals(self, state_mgr):
        """Add multiple approvals, all appear in list."""
        id1 = state_mgr.add_pending_approval({"task": _make_yellow_task_dict("Task A"), "cycle_num": 1})
        id2 = state_mgr.add_pending_approval({"task": _make_yellow_task_dict("Task B"), "cycle_num": 1})

        approvals = state_mgr.get_pending_approvals()
        assert len(approvals) == 2
        assert approvals[0]["id"] == id1
        assert approvals[1]["id"] == id2

    def test_empty_approvals(self, state_mgr):
        """No approvals returns empty list."""
        assert state_mgr.get_pending_approvals() == []


class TestApproveTask:
    def test_approve_task(self, state_mgr):
        """Approve a task, verify status changes."""
        approval_id = state_mgr.add_pending_approval({
            "task": _make_yellow_task_dict(),
            "cycle_num": 3,
        })

        result = state_mgr.approve_task(approval_id)
        assert result is not None
        assert result["status"] == "approved"
        assert "approved_at" in result
        assert result["task"]["title"] == "Refactor auth module"

        # Verify persisted
        approvals = state_mgr.get_pending_approvals()
        assert approvals[0]["status"] == "approved"

    def test_approve_nonexistent_returns_none(self, state_mgr):
        """Approving a nonexistent ID returns None."""
        result = state_mgr.approve_task("nonexistent")
        assert result is None

    def test_approve_already_approved_returns_none(self, state_mgr):
        """Cannot approve an already-approved task."""
        approval_id = state_mgr.add_pending_approval({"task": _make_yellow_task_dict(), "cycle_num": 1})
        state_mgr.approve_task(approval_id)
        result = state_mgr.approve_task(approval_id)
        assert result is None


class TestRejectTask:
    def test_reject_task(self, state_mgr):
        """Reject a task with reason, verify status and reason."""
        approval_id = state_mgr.add_pending_approval({
            "task": _make_yellow_task_dict(),
            "cycle_num": 2,
        })

        result = state_mgr.reject_task(approval_id, reason="Too risky")
        assert result is not None
        assert result["status"] == "rejected"
        assert result["reject_reason"] == "Too risky"
        assert "rejected_at" in result

        # Verify persisted
        approvals = state_mgr.get_pending_approvals()
        assert approvals[0]["status"] == "rejected"

    def test_reject_nonexistent_returns_none(self, state_mgr):
        """Rejecting a nonexistent ID returns None."""
        result = state_mgr.reject_task("nonexistent", "no reason")
        assert result is None

    def test_reject_without_reason(self, state_mgr):
        """Reject without reason defaults to empty string."""
        approval_id = state_mgr.add_pending_approval({"task": _make_yellow_task_dict(), "cycle_num": 1})
        result = state_mgr.reject_task(approval_id)
        assert result["reject_reason"] == ""


class TestYellowTasksQueuedNotDispatched:
    def test_yellow_tasks_queued_not_dispatched(self, state_mgr):
        """YELLOW tasks from engine._plan_tasks go to approval queue, not to HarnessTrigger."""
        from aros_meta_loop.services.task_planner import TaskPlanner

        yellow_task = PlannedTask(
            title="Refactor module X",
            description="Improve code structure",
            target_project="~/Projects/test",
            authority_level=AuthorityLevel.YELLOW,
            estimated_minutes=20,
            goal_source="G3_helpful",
        )

        green_task = PlannedTask(
            title="Fix typo in docs",
            description="Simple typo fix",
            target_project="~/Projects/test",
            authority_level=AuthorityLevel.GREEN,
            estimated_minutes=5,
            goal_source="G1_truthful",
        )

        # Mock TaskPlanner.generate_tasks to return mixed tasks
        with patch.object(TaskPlanner, "generate_tasks", return_value=[green_task, yellow_task]):
            # Import engine internals
            from aros_meta_loop.services.engine import MetaLoopEngine

            # Create a minimal engine with mocked dependencies
            with patch("aros_meta_loop.services.engine.get_db"), \
                 patch("aros_meta_loop.services.engine.L1Collector"), \
                 patch("aros_meta_loop.services.engine.L2Evaluator"), \
                 patch("aros_meta_loop.services.engine.L3SignalDeriver"):
                engine = MetaLoopEngine.__new__(MetaLoopEngine)
                engine.state = state_mgr
                engine._cycle_log = {"cycle_num": 10}

                # Mock cadence to be aggressive
                with patch.object(state_mgr, "read_cadence", return_value={"mode": "aggressive"}):
                    perceive_data = {"l2_scores": {"below_threshold": ["G3_helpful"]}}
                    task_dicts = engine._plan_tasks(perceive_data)

        # Verify: YELLOW task was queued for approval
        approvals = state_mgr.get_pending_approvals()
        assert len(approvals) == 1
        assert approvals[0]["task"]["title"] == "Refactor module X"
        assert approvals[0]["task"]["authority_level"] == "YELLOW"
        assert approvals[0]["cycle_num"] == 10
        assert approvals[0]["status"] == "pending"

        # Verify: both tasks are in the returned list (for logging)
        assert len(task_dicts) == 2


class TestApproveTriggers:
    def test_approve_triggers_harness(self, state_mgr):
        """Approving a task should allow triggering via HarnessTrigger."""
        task_dict = _make_yellow_task_dict("Optimize query performance")
        approval_id = state_mgr.add_pending_approval({
            "task": task_dict,
            "cycle_num": 7,
        })

        # Approve the task
        result = state_mgr.approve_task(approval_id)
        assert result is not None
        assert result["status"] == "approved"

        # Verify the approved task data can be used to create a PlannedTask for HarnessTrigger
        approved_task = result["task"]
        planned_task = PlannedTask(
            title=approved_task["title"],
            description=approved_task["description"],
            target_project=approved_task["target_project"],
            authority_level=AuthorityLevel.GREEN,  # Approved YELLOW → dispatch as GREEN
            estimated_minutes=approved_task["estimated_minutes"],
            goal_source=approved_task["goal_source"],
        )

        trigger = HarnessTrigger(gateway_url="http://test:8000", chat_id="-123")

        # Mock the HTTP call to gateway
        with patch.object(trigger, "is_harness_running", return_value=False):
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_client = MagicMock()
            mock_client.__enter__ = MagicMock(return_value=mock_client)
            mock_client.__exit__ = MagicMock(return_value=False)
            mock_client.post.return_value = mock_resp

            with patch("aros_meta_loop.services.harness_trigger.httpx.Client", return_value=mock_client):
                dispatch_result = trigger.trigger_harness_loop([planned_task])

        assert dispatch_result["status"] == "dispatched"
        assert dispatch_result["task_count"] == 1
        mock_client.post.assert_called_once()
