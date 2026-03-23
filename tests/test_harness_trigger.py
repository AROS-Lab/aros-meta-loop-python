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

        mock_get_resp = MagicMock()
        mock_get_resp.status_code = 200
        mock_get_resp.json.return_value = []  # No running sessions

        mock_client = MagicMock()
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client.get.return_value = mock_get_resp
        mock_client.post.return_value = mock_post_resp

        with patch("aros_meta_loop.services.harness_trigger.httpx.Client", return_value=mock_client):
            result = trigger.trigger_harness_loop([_make_task()])

        assert result["status"] == "dispatched"
        assert result["task_count"] == 1
        mock_client.post.assert_called_once()

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

    def test_trigger_handles_gateway_error(self, trigger):
        """Mock httpx raising exception, verify error result."""
        with patch.object(trigger, "is_harness_running", return_value=False):
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
