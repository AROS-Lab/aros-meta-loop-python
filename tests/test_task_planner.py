"""Tests for TaskPlanner."""
import pytest
from pathlib import Path

from aros_meta_loop.services.task_planner import (
    AuthorityLevel,
    PlannedTask,
    TaskPlanner,
    MAX_TASKS_PER_CYCLE,
)


@pytest.fixture
def planner_no_backlog(tmp_path):
    """TaskPlanner with a non-existent backlog path."""
    return TaskPlanner(backlog_path=tmp_path / "nonexistent.md")


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
    return TaskPlanner(backlog_path=backlog)


def test_generate_tasks_with_no_below_threshold(planner_no_backlog):
    """Empty below_threshold list returns no tasks."""
    tasks = planner_no_backlog.generate_tasks(scores={}, below_threshold=[])
    assert tasks == []


def test_generate_tasks_g5_ambitious(planner_with_backlog):
    """G5 below threshold generates tasks from backlog."""
    tasks = planner_with_backlog.generate_tasks(
        scores={"G5_ambitious": 0.1},
        below_threshold=["G5_ambitious"],
    )
    assert len(tasks) >= 1
    assert all(t.goal_source == "G5_ambitious" for t in tasks)
    # Should have parsed backlog items
    assert any("event bus" in t.title.lower() for t in tasks)


def test_generate_tasks_g3_reliable(planner_no_backlog):
    """G3 below threshold generates test improvement tasks."""
    tasks = planner_no_backlog.generate_tasks(
        scores={"G3_reliable": 0.2},
        below_threshold=["G3_reliable"],
    )
    assert len(tasks) == 1
    assert tasks[0].goal_source == "G3_reliable"
    assert "test" in tasks[0].title.lower() or "coverage" in tasks[0].title.lower()


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
    # Should have parsed 2 items from the 3-item backlog (max 2 from backlog)
    assert len(tasks) == 2
    titles = [t.title for t in tasks]
    assert "Implement event bus for aros-kernel" in titles
    assert "Add memory throttle module" in titles
    # Backlog items should be YELLOW (new features)
    assert all(t.authority_level == AuthorityLevel.YELLOW for t in tasks)


def test_missing_backlog_graceful(planner_no_backlog):
    """Missing backlog file doesn't crash, falls back to generic task."""
    tasks = planner_no_backlog.generate_tasks(
        scores={"G5_ambitious": 0.1},
        below_threshold=["G5_ambitious"],
    )
    assert len(tasks) == 1
    assert tasks[0].goal_source == "G5_ambitious"
    # Fallback task
    assert "hardware" in tasks[0].title.lower() or "aros-kernel" in tasks[0].target_project
