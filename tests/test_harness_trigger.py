"""Tests for HarnessTrigger."""
import httpx
import pytest
from unittest.mock import patch, MagicMock

from aros_meta_loop.services.harness_trigger import HarnessTrigger
from aros_meta_loop.services.task_planner import PlannedTask, AuthorityLevel


def _make_task(authority: AuthorityLevel = AuthorityLevel.GREEN, title: str = "Test task") -> PlannedTask:
    """Helper to create a PlannedTask for testing."""
    return PlannedTask(
        title=title,
        description="A test task description",
        target_project="~/Projects/test-project",
        authority_level=authority,
        estimated_minutes=15,
        goal_source="G2_efficient",
    )


@pytest.fixture
def trigger():
    return HarnessTrigger(gateway_url="http://test:8000", chat_id="-123")


class TestTriggerHarnessLoop:
    def test_trigger_with_green_tasks(self, trigger):
        """Mock httpx POST returning 200, verify dispatched."""
        mock_post_resp = MagicMock()
        mock_post_resp.status_code = 200

        # Sessions GET -> no running sessions
        sessions_resp = MagicMock(status_code=200)
        sessions_resp.json.return_value = []

        # Harness status GET -> no existing harness
        harness_resp = MagicMock(status_code=200)
        harness_resp.json.return_value = {"jobs": []}

        mock_client = MagicMock()
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client.get.side_effect = [sessions_resp, harness_resp]
        mock_client.post.return_value = mock_post_resp

        with patch("aros_meta_loop.services.harness_trigger.httpx.Client", return_value=mock_client):
            result = trigger.trigger_harness_loop([_make_task()])

        assert result["status"] == "dispatched"
        assert result["task_count"] == 1
        mock_client.post.assert_called_once()
        call_kwargs = mock_client.post.call_args
        payload = call_kwargs.kwargs.get("json") or call_kwargs[1].get("json")
        assert "bot_token" in payload
        assert payload["bot_token"] == ""

    def test_trigger_skips_when_running(self, trigger):
        """Mock is_harness_running returning True, verify skipped."""
        with patch.object(trigger, "is_harness_running", return_value=True):
            result = trigger.trigger_harness_loop([_make_task()])

        assert result["status"] == "skipped"
        assert result["reason"] == "harness already running"

    def test_trigger_skips_no_tasks(self, trigger):
        """Empty list returns skipped."""
        result = trigger.trigger_harness_loop([])
        assert result["status"] == "skipped"
        assert result["reason"] == "no tasks"

    def test_trigger_skips_yellow_only(self, trigger):
        """Only YELLOW tasks returns skipped (YELLOW goes to approval queue)."""
        yellow_tasks = [
            _make_task(authority=AuthorityLevel.YELLOW, title="Yellow task 1"),
            _make_task(authority=AuthorityLevel.YELLOW, title="Yellow task 2"),
        ]

        with patch.object(trigger, "is_harness_running", return_value=False):
            with patch.object(trigger, "get_harness_state", return_value=None):
                result = trigger.trigger_harness_loop(yellow_tasks)

        assert result["status"] == "skipped"
        assert result["reason"] == "no GREEN tasks to auto-dispatch"

    def test_format_prompt_contains_persona(self, trigger):
        """Verify prompt mentions Nirmana and PERSONA.md."""
        tasks = [_make_task(title="Optimize build")]
        prompt = trigger._format_prompt(tasks)

        assert "Nirmana" in prompt
        assert "PERSONA.md" in prompt
        assert "Optimize build" in prompt
        assert "GREEN" in prompt
        assert "gh issue close" in prompt

    def test_trigger_handles_gateway_error(self, trigger):
        """Mock httpx raising exception, verify error result."""
        with patch.object(trigger, "is_harness_running", return_value=False):
            with patch.object(trigger, "get_harness_state", return_value=None):
                with patch("aros_meta_loop.services.harness_trigger.httpx.Client") as mock_cls:
                    mock_client = MagicMock()
                    mock_client.__enter__ = MagicMock(return_value=mock_client)
                    mock_client.__exit__ = MagicMock(return_value=False)
                    mock_client.post.side_effect = httpx.ConnectError("Connection refused")
                    mock_cls.return_value = mock_client

                    result = trigger.trigger_harness_loop([_make_task()])

        assert result["status"] == "error"
        assert "Connection refused" in result["reason"]


class TestIsHarnessRunning:
    def test_returns_true_when_busy_bg_session(self, trigger):
        """Returns True when a busy background session exists."""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = [
            {"chat_id": "bg-123", "busy": True},
        ]

        mock_client = MagicMock()
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client.get.return_value = mock_resp

        with patch("aros_meta_loop.services.harness_trigger.httpx.Client", return_value=mock_client):
            assert trigger.is_harness_running() is True

    def test_returns_false_when_no_sessions(self, trigger):
        """Returns False when no sessions exist."""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = []

        mock_client = MagicMock()
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client.get.return_value = mock_resp

        with patch("aros_meta_loop.services.harness_trigger.httpx.Client", return_value=mock_client):
            assert trigger.is_harness_running() is False

    def test_returns_false_on_connection_error(self, trigger):
        """Returns False when gateway is unreachable."""
        with patch("aros_meta_loop.services.harness_trigger.httpx.Client") as mock_cls:
            mock_client = MagicMock()
            mock_client.__enter__ = MagicMock(return_value=mock_client)
            mock_client.__exit__ = MagicMock(return_value=False)
            mock_client.get.side_effect = Exception("Connection refused")
            mock_cls.return_value = mock_client

            assert trigger.is_harness_running() is False


class TestResumeAndCleanup:
    def test_resumes_unfinished_harness(self, trigger):
        """When an unfinished resumable harness exists, resume it instead of creating new."""
        mock_client = MagicMock()
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)

        # is_harness_running -> False (not actively busy)
        sessions_resp = MagicMock(status_code=200)
        sessions_resp.json.return_value = []

        # get_harness_state -> unfinished harness with pending tasks
        harness_resp = MagicMock(status_code=200)
        harness_resp.json.return_value = {"jobs": [{
            "harness": {
                "project_name": "test-project",
                "current_phase": "engineering",
                "total": 5, "done": 2, "pending": 3, "in_progress": 0, "blocked": 0,
            }
        }]}

        # resume POST -> 200
        resume_resp = MagicMock(status_code=200)

        mock_client.get.side_effect = [sessions_resp, harness_resp]
        mock_client.post.return_value = resume_resp

        with patch("aros_meta_loop.services.harness_trigger.httpx.Client", return_value=mock_client):
            result = trigger.trigger_harness_loop([_make_task()])

        assert result["status"] == "resumed"

    def test_cleans_up_non_resumable_harness(self, trigger):
        """When harness exists but all tasks are blocked/failed, cleanup and start new."""
        mock_client = MagicMock()
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)

        # is_harness_running -> False
        sessions_resp = MagicMock(status_code=200)
        sessions_resp.json.return_value = []

        # get_harness_state -> stuck harness (no pending, no in_progress, not complete)
        harness_resp = MagicMock(status_code=200)
        harness_resp.json.return_value = {"jobs": [{
            "harness": {
                "project_name": "stuck-project",
                "current_phase": "engineering",
                "total": 5, "done": 3, "pending": 0, "in_progress": 0, "blocked": 2,
            }
        }]}

        # cleanup POST -> 200
        cleanup_resp = MagicMock(status_code=200)
        cleanup_resp.json.return_value = {"cleaned": 1}

        # new dispatch POST -> 200
        dispatch_resp = MagicMock(status_code=200)

        mock_client.get.side_effect = [sessions_resp, harness_resp]
        mock_client.post.side_effect = [cleanup_resp, dispatch_resp]

        with patch("aros_meta_loop.services.harness_trigger.httpx.Client", return_value=mock_client):
            result = trigger.trigger_harness_loop([_make_task()])

        assert result["status"] == "dispatched"

    def test_cleans_up_completed_before_new(self, trigger):
        """When previous harness is complete, archive it before starting new."""
        mock_client = MagicMock()
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)

        sessions_resp = MagicMock(status_code=200)
        sessions_resp.json.return_value = []

        harness_resp = MagicMock(status_code=200)
        harness_resp.json.return_value = {"jobs": [{
            "harness": {
                "project_name": "done-project",
                "current_phase": "complete",
                "total": 4, "done": 4, "pending": 0, "in_progress": 0, "blocked": 0,
            }
        }]}

        cleanup_resp = MagicMock(status_code=200)
        cleanup_resp.json.return_value = {"cleaned": 1}
        dispatch_resp = MagicMock(status_code=200)

        mock_client.get.side_effect = [sessions_resp, harness_resp]
        mock_client.post.side_effect = [cleanup_resp, dispatch_resp]

        with patch("aros_meta_loop.services.harness_trigger.httpx.Client", return_value=mock_client):
            result = trigger.trigger_harness_loop([_make_task()])

        assert result["status"] == "dispatched"

    def test_is_resumable_logic(self, trigger):
        """Test _is_resumable with various harness states."""
        # Complete -> not resumable
        assert trigger._is_resumable({"harness": {"current_phase": "complete", "total": 4, "done": 4, "pending": 0, "in_progress": 0}}) is False
        # Has pending -> resumable
        assert trigger._is_resumable({"harness": {"current_phase": "engineering", "total": 4, "done": 2, "pending": 2, "in_progress": 0}}) is True
        # Has in_progress -> resumable
        assert trigger._is_resumable({"harness": {"current_phase": "engineering", "total": 4, "done": 2, "pending": 0, "in_progress": 1}}) is True
        # Empty -> not resumable
        assert trigger._is_resumable({"harness": {"current_phase": "init", "total": 0, "done": 0, "pending": 0, "in_progress": 0}}) is False


class TestVerifyLastDispatch:
    def test_verify_completed_dispatch(self, trigger):
        """Completed dispatch returns completed=True with details and has_commits detection."""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "status": "completed",
            "elapsed_seconds": 120,
            "result": "Fixed the bug and pushed commit abc123",
        }

        mock_client = MagicMock()
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client.get.return_value = mock_resp

        with patch("aros_meta_loop.services.harness_trigger.httpx.Client", return_value=mock_client):
            result = trigger.verify_last_dispatch()

        assert result["status"] == "completed"
        assert result["completed"] is True
        assert result["has_commits"] is True
        assert "commit" in result["details"].lower()

    def test_verify_completed_no_commits(self, trigger):
        """Completed dispatch without commit keywords sets has_commits=False."""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "status": "completed",
            "elapsed_seconds": 60,
            "result": "Analysis complete, no changes needed",
        }

        mock_client = MagicMock()
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client.get.return_value = mock_resp

        with patch("aros_meta_loop.services.harness_trigger.httpx.Client", return_value=mock_client):
            result = trigger.verify_last_dispatch()

        assert result["status"] == "completed"
        assert result["completed"] is True
        assert result["has_commits"] is False

    def test_verify_running_dispatch(self, trigger):
        """Running dispatch returns completed=False with elapsed time in details."""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "status": "running",
            "elapsed_seconds": 45,
        }

        mock_client = MagicMock()
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client.get.return_value = mock_resp

        with patch("aros_meta_loop.services.harness_trigger.httpx.Client", return_value=mock_client):
            result = trigger.verify_last_dispatch()

        assert result["status"] == "running"
        assert result["completed"] is False
        assert "45" in result["details"]

    def test_verify_idle(self, trigger):
        """Idle status returns completed=False with 'No active task' details."""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "status": "idle",
            "elapsed_seconds": 0,
        }

        mock_client = MagicMock()
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client.get.return_value = mock_resp

        with patch("aros_meta_loop.services.harness_trigger.httpx.Client", return_value=mock_client):
            result = trigger.verify_last_dispatch()

        assert result["status"] == "idle"
        assert result["completed"] is False
        assert result["details"] == "No active task"

    def test_verify_api_error(self, trigger):
        """Non-200 API response returns unknown status."""
        mock_resp = MagicMock()
        mock_resp.status_code = 500

        mock_client = MagicMock()
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client.get.return_value = mock_resp

        with patch("aros_meta_loop.services.harness_trigger.httpx.Client", return_value=mock_client):
            result = trigger.verify_last_dispatch()

        assert result["status"] == "unknown"
        assert result["completed"] is False
        assert "API error" in result["details"]

    def test_verify_connection_error(self, trigger):
        """Connection error returns error status with exception details."""
        with patch("aros_meta_loop.services.harness_trigger.httpx.Client") as mock_cls:
            mock_client = MagicMock()
            mock_client.__enter__ = MagicMock(return_value=mock_client)
            mock_client.__exit__ = MagicMock(return_value=False)
            mock_client.get.side_effect = httpx.ConnectError("Connection refused")
            mock_cls.return_value = mock_client

            result = trigger.verify_last_dispatch()

        assert result["status"] == "error"
        assert result["completed"] is False
        assert "Connection refused" in result["details"]
