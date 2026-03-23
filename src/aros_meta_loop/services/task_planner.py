"""Task Planner — generates improvement tasks from meta-goal analysis and project backlog."""
from dataclasses import dataclass, field
from pathlib import Path
from enum import Enum
import logging
import re

logger = logging.getLogger(__name__)

NIGHT_RUNNER_PROJECTS = Path.home() / "eddie-nirmana" / "state" / "night-runner-projects.md"
MAX_TASKS_PER_CYCLE = 5


class AuthorityLevel(Enum):
    GREEN = "GREEN"    # Auto-execute: tests, bug fixes, docs
    YELLOW = "YELLOW"  # Execute + log: refactoring, perf, new features


@dataclass
class PlannedTask:
    title: str
    description: str
    target_project: str        # e.g. "~/Projects/aros-kernel"
    authority_level: AuthorityLevel
    estimated_minutes: int = 15
    goal_source: str = ""      # Which meta-goal triggered this (e.g. "G5_ambitious")
    backlog_item: str = ""     # Reference to night-runner backlog item if applicable


class TaskPlanner:
    """Generates improvement tasks from meta-goal analysis and project backlog."""

    def __init__(self, backlog_path: Path = NIGHT_RUNNER_PROJECTS):
        self.backlog_path = backlog_path
        self._backlog_cache: str | None = None

    def generate_tasks(
        self,
        scores: dict,              # {"G1_truthful": 1.0, "G5_ambitious": 0.1, ...}
        below_threshold: list[str], # ["G2_efficient", "G5_ambitious"]
    ) -> list[PlannedTask]:
        """Generate improvement tasks based on low-scoring goals."""
        if not below_threshold:
            return []

        tasks: list[PlannedTask] = []

        for goal in below_threshold:
            if len(tasks) >= MAX_TASKS_PER_CYCLE:
                break

            new_tasks = self._tasks_for_goal(goal, scores)
            tasks.extend(new_tasks)

        return tasks[:MAX_TASKS_PER_CYCLE]

    def _tasks_for_goal(self, goal: str, scores: dict) -> list[PlannedTask]:
        """Generate tasks for a specific low-scoring goal."""
        if goal == "G2_efficient":
            return self._efficiency_tasks()
        elif goal == "G3_reliable":
            return self._reliability_tasks()
        elif goal == "G5_ambitious":
            return self._ambitious_tasks()
        elif goal == "G1_truthful":
            return self._truthfulness_tasks()
        elif goal == "G4_aligned":
            return self._alignment_tasks()
        elif goal == "G6_self_know":
            return self._self_knowledge_tasks()
        return []

    def _efficiency_tasks(self) -> list[PlannedTask]:
        """G2 low: optimize code, reduce token usage."""
        return [
            PlannedTask(
                title="Optimize aros-kernel build times",
                description="Profile cargo build, identify slow compilation units, optimize Cargo.toml features and dependencies",
                target_project="~/Projects/aros-kernel",
                authority_level=AuthorityLevel.GREEN,
                estimated_minutes=20,
                goal_source="G2_efficient",
            ),
        ]

    def _reliability_tasks(self) -> list[PlannedTask]:
        """G3 low: add tests, fix flaky tests."""
        return [
            PlannedTask(
                title="Add missing test coverage for aros-kernel",
                description="Run cargo test, identify modules with low coverage, add unit tests for untested functions",
                target_project="~/Projects/aros-kernel",
                authority_level=AuthorityLevel.GREEN,
                estimated_minutes=30,
                goal_source="G3_reliable",
            ),
        ]

    def _ambitious_tasks(self) -> list[PlannedTask]:
        """G5 low: pick from night-runner backlog."""
        backlog = self._read_backlog()
        tasks = []

        if not backlog:
            # Fallback: generic improvement task
            tasks.append(PlannedTask(
                title="Improve aros-kernel hardware awareness",
                description="Add event bus, throttle, or memory audit to aros-kernel based on gap analysis from Python Centurion",
                target_project="~/Projects/aros-kernel",
                authority_level=AuthorityLevel.YELLOW,
                estimated_minutes=30,
                goal_source="G5_ambitious",
            ))
            return tasks

        # Parse backlog for actionable items
        backlog_items = self._parse_backlog_items(backlog)
        for item in backlog_items[:2]:  # Max 2 from backlog
            tasks.append(PlannedTask(
                title=item["title"],
                description=item["description"],
                target_project=item.get("project", "~/Projects/aros-kernel"),
                authority_level=AuthorityLevel.YELLOW if item.get("is_new_feature") else AuthorityLevel.GREEN,
                estimated_minutes=item.get("est_minutes", 20),
                goal_source="G5_ambitious",
                backlog_item=item["title"],
            ))

        return tasks

    def _truthfulness_tasks(self) -> list[PlannedTask]:
        return [PlannedTask(
            title="Improve error reporting accuracy",
            description="Review error messages and logging in aros-meta-loop for accuracy and clarity",
            target_project="~/Projects/aros-meta-loop",
            authority_level=AuthorityLevel.GREEN,
            estimated_minutes=15,
            goal_source="G1_truthful",
        )]

    def _alignment_tasks(self) -> list[PlannedTask]:
        return [PlannedTask(
            title="Review tool call patterns for alignment",
            description="Analyze recent tool call failures and improve error handling",
            target_project="~/Projects/mini-claude-bot",
            authority_level=AuthorityLevel.GREEN,
            estimated_minutes=15,
            goal_source="G4_aligned",
        )]

    def _self_knowledge_tasks(self) -> list[PlannedTask]:
        return [PlannedTask(
            title="Improve self-model calibration",
            description="Review self-model.toml accuracy against actual performance data",
            target_project="~/Projects/aros-meta-loop",
            authority_level=AuthorityLevel.GREEN,
            estimated_minutes=15,
            goal_source="G6_self_know",
        )]

    def _read_backlog(self) -> str:
        """Read the night-runner-projects.md backlog file."""
        if self._backlog_cache is not None:
            return self._backlog_cache
        try:
            if self.backlog_path.exists():
                self._backlog_cache = self.backlog_path.read_text()
                return self._backlog_cache
        except Exception as e:
            logger.warning(f"Could not read backlog: {e}")
        self._backlog_cache = ""
        return ""

    def _parse_backlog_items(self, backlog: str) -> list[dict]:
        """Parse the night-runner-projects.md into actionable items."""
        items = []
        # Look for "Task Backlog" section and extract numbered items
        in_backlog = False
        for line in backlog.split("\n"):
            if "Task Backlog" in line or "Backlog" in line:
                in_backlog = True
                continue
            if in_backlog and line.startswith("##"):
                break  # Next section
            if in_backlog and (line.startswith("- ") or re.match(r"^\d+\.", line)):
                # Extract task title and description
                text = re.sub(r"^[-\d.]+\s*", "", line).strip()
                if text:
                    items.append({
                        "title": text[:80],
                        "description": text,
                        "project": "~/Projects/aros-kernel",
                        "is_new_feature": True,
                        "est_minutes": 20,
                    })
        return items
