"""Tests for TaskPlanner — covers all three sources: backlog, GitHub issues, chat history."""
import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock

from aros_meta_loop.services.task_planner import (
    AuthorityLevel,
    PlannedTask,
    TaskPlanner,
    MAX_TASKS_PER_CYCLE,
)


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
