"""Tests for TaskPlanner — covers all three sources: backlog, GitHub issues, chat history."""
import json
import subprocess
import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock

from aros_meta_loop.services.task_planner import (
    AuthorityLevel,
    PlannedTask,
    TaskPlanner,
    MAX_TASKS_PER_CYCLE,
)


@pytest.fixture(autouse=True)
def reset_recent_titles():
    """Clear deduplication cache between tests."""
    TaskPlanner._recent_dispatches = []


@pytest.fixture
def planner_no_backlog(tmp_path):
    """TaskPlanner with a non-existent backlog path and no external sources."""
    planner = TaskPlanner(backlog_path=tmp_path / "nonexistent.md")
    # Mock out external sources so tests are deterministic
    planner._issues_cache = []  # No GitHub issues
    return planner


@pytest.fixture
def planner_with_backlog(tmp_path):
    """TaskPlanner with a real backlog file."""
    backlog = tmp_path / "night-runner-projects.md"
    backlog.write_text(
        "# Night Runner Projects\n"
        "\n"
        "## Task Backlog\n"
        "- Implement event bus for aros-kernel\n"
        "- Add memory throttle module\n"
        "- Write integration tests for scheduler\n"
        "\n"
        "## Completed\n"
        "- Initial setup\n"
    )
    planner = TaskPlanner(backlog_path=backlog)
    planner._issues_cache = []  # No GitHub issues for backlog tests
    return planner


@pytest.fixture
def planner_with_issues(tmp_path):
    """TaskPlanner with mocked GitHub issues."""
    planner = TaskPlanner(backlog_path=tmp_path / "nonexistent.md")
    planner._issues_cache = [
        {
            "number": 42,
            "title": "Fix memory leak in scheduler",
            "labels": [{"name": "bug"}],
            "url": "https://github.com/AROS-Lab/aros-kernel/issues/42",
            "repo": "AROS-Lab/aros-kernel",
            "project": "~/Projects/aros-kernel",
        },
        {
            "number": 7,
            "title": "Add /health endpoint",
            "labels": [{"name": "enhancement"}],
            "url": "https://github.com/spacelobster88/mini-claude-bot/issues/7",
            "repo": "spacelobster88/mini-claude-bot",
            "project": "~/Projects/mini-claude-bot",
        },
    ]
    return planner


def test_generate_tasks_with_no_below_threshold(planner_no_backlog):
    """Empty below_threshold list returns no tasks."""
    tasks = planner_no_backlog.generate_tasks(scores={}, below_threshold=[])
    assert tasks == []


def test_generate_tasks_g5_ambitious_from_backlog(planner_with_backlog):
    """G5 below threshold generates tasks from backlog."""
    tasks = planner_with_backlog.generate_tasks(
        scores={"G5_ambitious": 0.1},
        below_threshold=["G5_ambitious"],
    )
    assert len(tasks) >= 1
    assert all(t.goal_source == "G5_ambitious" for t in tasks)
    # Should have parsed backlog items
    backlog_tasks = [t for t in tasks if t.source == "backlog"]
    assert len(backlog_tasks) >= 1
    assert any("event bus" in t.title.lower() for t in backlog_tasks)


def test_generate_tasks_g3_reliable_fallback(planner_no_backlog):
    """G3 below threshold without issues falls back to generic task."""
    tasks = planner_no_backlog.generate_tasks(
        scores={"G3_reliable": 0.2},
        below_threshold=["G3_reliable"],
    )
    assert len(tasks) >= 1
    assert tasks[0].goal_source == "G3_reliable"


def test_generate_tasks_g3_from_issues(planner_with_issues):
    """G3 below threshold with bug issues picks from GitHub."""
    tasks = planner_with_issues.generate_tasks(
        scores={"G3_reliable": 0.2},
        below_threshold=["G3_reliable"],
    )
    assert len(tasks) >= 1
    bug_tasks = [t for t in tasks if t.source == "github_issue"]
    assert len(bug_tasks) >= 1
    assert "42" in bug_tasks[0].title  # Issue #42


def test_generate_tasks_max_5(planner_no_backlog):
    """Even with many goals below threshold, max 5 tasks are returned."""
    all_goals = ["G1_truthful", "G2_efficient", "G3_reliable", "G4_aligned", "G5_ambitious", "G6_self_know"]
    scores = {g: 0.1 for g in all_goals}
    tasks = planner_no_backlog.generate_tasks(scores=scores, below_threshold=all_goals)
    assert len(tasks) <= MAX_TASKS_PER_CYCLE


def test_planned_task_has_authority_level(planner_no_backlog):
    """Each generated task has a valid GREEN or YELLOW authority level."""
    tasks = planner_no_backlog.generate_tasks(
        scores={"G2_efficient": 0.3, "G5_ambitious": 0.1},
        below_threshold=["G2_efficient", "G5_ambitious"],
    )
    assert len(tasks) >= 1
    for task in tasks:
        assert isinstance(task.authority_level, AuthorityLevel)
        assert task.authority_level in (AuthorityLevel.GREEN, AuthorityLevel.YELLOW)


def test_reads_backlog_file(planner_with_backlog):
    """With a temp backlog file, verifies parsing extracts items."""
    tasks = planner_with_backlog.generate_tasks(
        scores={"G5_ambitious": 0.1},
        below_threshold=["G5_ambitious"],
    )
    backlog_tasks = [t for t in tasks if t.source == "backlog"]
    assert len(backlog_tasks) >= 2
    titles = [t.title for t in backlog_tasks]
    assert "Implement event bus for aros-kernel" in titles
    assert "Add memory throttle module" in titles


def test_quarantine_do_not_auto_dispatch_marker_skipped(tmp_path):
    """Tasks with DO_NOT_AUTO_DISPATCH in the description are skipped (Nirmana quarantine)."""
    backlog = tmp_path / "night-runner-projects.md"
    backlog.write_text(
        "# Night Runner Projects\n\n"
        "## Task Backlog\n"
        "| # | Task | Source | Priority |\n"
        "|---|------|--------|----------|\n"
        "| 1 | File provisional patent. `DO_NOT_AUTO_DISPATCH` — requires Eddie. | Eddie | MEDIUM-HIGH |\n"
        "| 2 | Implement event bus for aros-kernel | planning | MEDIUM |\n"
    )
    planner = TaskPlanner(backlog_path=backlog)
    planner._issues_cache = []
    tasks = planner.generate_tasks(
        scores={"G5_ambitious": 0.1}, below_threshold=["G5_ambitious"],
    )
    titles = [t.title for t in tasks if t.source == "backlog"]
    assert not any("patent" in t.lower() for t in titles), \
        f"Quarantined task leaked into dispatch: {titles}"
    assert any("event bus" in t.lower() for t in titles), \
        f"Non-quarantined task was wrongly filtered: {titles}"


def test_quarantine_red_priority_skipped(tmp_path):
    """Tasks with RED priority are skipped (Nirmana Eddie-only tasks)."""
    backlog = tmp_path / "night-runner-projects.md"
    backlog.write_text(
        "# Night Runner Projects\n\n"
        "## Task Backlog\n"
        "| # | Task | Source | Priority |\n"
        "|---|------|--------|----------|\n"
        "| 1 | Pay the USPTO filing fee on my behalf | Eddie | RED |\n"
        "| 2 | Add memory throttle module | planning | HIGH |\n"
    )
    planner = TaskPlanner(backlog_path=backlog)
    planner._issues_cache = []
    tasks = planner.generate_tasks(
        scores={"G5_ambitious": 0.1}, below_threshold=["G5_ambitious"],
    )
    titles = [t.title for t in tasks if t.source == "backlog"]
    assert not any("uspto" in t.lower() or "filing fee" in t.lower() for t in titles), \
        f"RED-priority task leaked into dispatch: {titles}"
    assert any("memory throttle" in t.lower() for t in titles), \
        f"HIGH-priority task was wrongly filtered: {titles}"


def test_missing_backlog_graceful(planner_no_backlog):
    """Missing backlog file doesn't crash, falls back to generic task."""
    with patch.object(planner_no_backlog, "_search_chat_history", return_value=[]):
        tasks = planner_no_backlog.generate_tasks(
            scores={"G5_ambitious": 0.1},
            below_threshold=["G5_ambitious"],
        )
    assert len(tasks) >= 1
    assert tasks[0].goal_source == "G5_ambitious"


def test_github_issues_to_tasks(planner_with_issues):
    """GitHub issues are converted to PlannedTasks correctly."""
    tasks = planner_with_issues.generate_tasks(
        scores={"G5_ambitious": 0.1},
        below_threshold=["G5_ambitious"],
    )
    issue_tasks = [t for t in tasks if t.source == "github_issue"]
    assert len(issue_tasks) >= 1
    # Bug issues should be GREEN
    bug_task = next((t for t in issue_tasks if "memory leak" in t.title.lower()), None)
    if bug_task:
        assert bug_task.authority_level == AuthorityLevel.GREEN
    # Enhancement issues should be YELLOW
    enh_task = next((t for t in issue_tasks if "health" in t.title.lower()), None)
    if enh_task:
        assert enh_task.authority_level == AuthorityLevel.YELLOW


def test_task_has_source_field(planner_with_backlog):
    """Each task has a source field indicating where it came from."""
    tasks = planner_with_backlog.generate_tasks(
        scores={"G5_ambitious": 0.1},
        below_threshold=["G5_ambitious"],
    )
    for task in tasks:
        assert task.source in ("backlog", "github_issue", "chat_history", "fallback")


def test_chat_history_search(planner_no_backlog):
    """Chat history search returns tasks when results found."""
    mock_results = [
        {"role": "user", "content": "Add GPU detection to aros-kernel hardware probe"},
        {"role": "assistant", "content": "I'll implement that..."},
    ]
    with patch.object(planner_no_backlog, "_search_chat_history", return_value=mock_results):
        tasks = planner_no_backlog.generate_tasks(
            scores={"G5_ambitious": 0.1},
            below_threshold=["G5_ambitious"],
        )
    chat_tasks = [t for t in tasks if t.source == "chat_history"]
    assert len(chat_tasks) >= 1
    assert "GPU" in chat_tasks[0].title


# ── Smart dedup: GitHub issue state checks ─────────────────────

class TestIsIssueStillOpen:
    """Tests for _is_issue_still_open helper method."""

    def test_open_issue_returns_true(self, planner_no_backlog):
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = json.dumps({"state": "OPEN"})
        with patch("subprocess.run", return_value=mock_result) as mock_run:
            assert planner_no_backlog._is_issue_still_open("AROS-Lab/aros-kernel#42") is True
            mock_run.assert_called_once_with(
                ["gh", "issue", "view", "42", "-R", "AROS-Lab/aros-kernel", "--json", "state"],
                capture_output=True, text=True, timeout=5,
            )

    def test_closed_issue_returns_false(self, planner_no_backlog):
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = json.dumps({"state": "CLOSED"})
        with patch("subprocess.run", return_value=mock_result):
            assert planner_no_backlog._is_issue_still_open("AROS-Lab/aros-kernel#42") is False

    def test_unparseable_backlog_item_assumes_open(self, planner_no_backlog):
        assert planner_no_backlog._is_issue_still_open("no-hash-here") is True

    def test_gh_command_failure_assumes_open(self, planner_no_backlog):
        mock_result = MagicMock()
        mock_result.returncode = 1
        mock_result.stdout = ""
        with patch("subprocess.run", return_value=mock_result):
            assert planner_no_backlog._is_issue_still_open("AROS-Lab/aros-kernel#42") is True

    def test_timeout_assumes_open(self, planner_no_backlog):
        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired("gh", 5)):
            assert planner_no_backlog._is_issue_still_open("AROS-Lab/aros-kernel#42") is True


class TestSmartDedup:
    """Tests for source-aware dedup in generate_tasks."""

    def test_open_github_issue_not_deduped(self, planner_with_issues):
        """Open GitHub issues should be re-dispatched even if recently seen."""
        with patch.object(TaskPlanner, "_is_issue_still_open", return_value=True):
            tasks1 = planner_with_issues.generate_tasks(
                scores={"G3_reliable": 0.2},
                below_threshold=["G3_reliable"],
            )
            issue_tasks1 = [t for t in tasks1 if t.source == "github_issue"]
            assert len(issue_tasks1) >= 1

            # Second call — open issues should still come through
            tasks2 = planner_with_issues.generate_tasks(
                scores={"G3_reliable": 0.2},
                below_threshold=["G3_reliable"],
            )
            issue_tasks2 = [t for t in tasks2 if t.source == "github_issue"]
            assert len(issue_tasks2) >= 1

    def test_closed_github_issue_filtered_out(self, planner_with_issues):
        """Closed GitHub issues should be filtered out."""
        with patch.object(TaskPlanner, "_is_issue_still_open", return_value=False):
            tasks = planner_with_issues.generate_tasks(
                scores={"G3_reliable": 0.2},
                below_threshold=["G3_reliable"],
            )
            issue_tasks = [t for t in tasks if t.source == "github_issue"]
            assert len(issue_tasks) == 0

    def test_non_github_tasks_still_time_deduped(self, planner_no_backlog):
        """Non-GitHub tasks should still use time-based dedup."""
        # First call generates fallback tasks
        tasks1 = planner_no_backlog.generate_tasks(
            scores={"G1_truthful": 0.2},
            below_threshold=["G1_truthful"],
        )
        assert len(tasks1) >= 1
        assert tasks1[0].source == "fallback"

        # Second call — same fallback should be deduped
        tasks2 = planner_no_backlog.generate_tasks(
            scores={"G1_truthful": 0.2},
            below_threshold=["G1_truthful"],
        )
        assert len(tasks2) == 0


class TestDispatchStatusTracking:
    """Tests for mark_dispatched / mark_dispatch_failed dedup behavior."""

    def setup_method(self):
        TaskPlanner._recent_dispatches = []

    def test_mark_dispatched_prevents_regen(self, planner_no_backlog):
        """Tasks marked as dispatched are deduped for 24h."""
        tasks1 = planner_no_backlog.generate_tasks(
            scores={"G1_truthful": 0.2},
            below_threshold=["G1_truthful"],
        )
        assert len(tasks1) >= 1

        # Mark as dispatched
        TaskPlanner.mark_dispatched([t.title for t in tasks1])

        # Second call — dispatched tasks still deduped
        tasks2 = planner_no_backlog.generate_tasks(
            scores={"G1_truthful": 0.2},
            below_threshold=["G1_truthful"],
        )
        assert len(tasks2) == 0

    def test_mark_failed_allows_retry(self, planner_no_backlog):
        """Tasks marked as failed dispatch are retried on next cycle."""
        tasks1 = planner_no_backlog.generate_tasks(
            scores={"G1_truthful": 0.2},
            below_threshold=["G1_truthful"],
        )
        assert len(tasks1) >= 1

        # Mark as failed — should allow retry
        TaskPlanner.mark_dispatch_failed([t.title for t in tasks1])

        # Second call — failed tasks are regenerated
        tasks2 = planner_no_backlog.generate_tasks(
            scores={"G1_truthful": 0.2},
            below_threshold=["G1_truthful"],
        )
        assert len(tasks2) >= 1

    def test_dedup_window_extended_to_24h(self):
        """Dedup window should be 24 hours (86400 seconds)."""
        assert TaskPlanner._DEDUP_WINDOW_SECONDS == 86400
