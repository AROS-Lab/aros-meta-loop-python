"""Task Planner — generates improvement tasks from three sources:
1. night-runner-projects.md backlog (materialized project registry)
2. GitHub issues (AROS-Lab repos + infra repos)
3. Chat history (unfinished requests from Eddie)
"""
from dataclasses import dataclass
from pathlib import Path
from enum import Enum
import json
import logging
import re
import subprocess

logger = logging.getLogger(__name__)

NIGHT_RUNNER_PROJECTS = Path.home() / "eddie-nirmana" / "state" / "night-runner-projects.md"
MAX_TASKS_PER_CYCLE = 5

# Repos to scan for GitHub issues
GITHUB_REPOS = [
    "AROS-Lab/aros-kernel",
    "AROS-Lab/aros-meta-loop",
    "spacelobster88/mini-claude-bot",
    "spacelobster88/telegram-claude-hero",
    "spacelobster88/centurion",
]

# Project path mapping
REPO_TO_PROJECT = {
    "AROS-Lab/aros-kernel": "~/Projects/aros-kernel",
    "AROS-Lab/aros-meta-loop": "~/Projects/aros-meta-loop",
    "spacelobster88/mini-claude-bot": "~/Projects/mini-claude-bot",
    "spacelobster88/telegram-claude-hero": "~/Projects/telegram-claude-hero",
    "spacelobster88/centurion": "~/Projects/centurion",
}


class AuthorityLevel(Enum):
    GREEN = "GREEN"    # Auto-execute: tests, bug fixes, docs
    YELLOW = "YELLOW"  # Execute + log: refactoring, perf, new features


@dataclass
class PlannedTask:
    title: str
    description: str
    target_project: str
    authority_level: AuthorityLevel
    estimated_minutes: int = 15
    goal_source: str = ""
    backlog_item: str = ""
    source: str = ""  # "backlog", "github_issue", "chat_history"


class TaskPlanner:
    """Generates improvement tasks from meta-goal analysis, GitHub issues, and chat history."""

    # Track recently dispatched tasks with timestamps for time-based expiry
    _recent_dispatches: list[tuple[str, float]] = []  # (title, timestamp)
    _DEDUP_WINDOW_SECONDS = 3600  # 1 hour - after this, same task can be re-generated

    def __init__(self, backlog_path: Path = NIGHT_RUNNER_PROJECTS):
        self.backlog_path = backlog_path
        self._backlog_cache: str | None = None
        self._issues_cache: list[dict] | None = None

    def generate_tasks(
        self,
        scores: dict,
        below_threshold: list[str],
    ) -> list[PlannedTask]:
        """Generate improvement tasks based on low-scoring goals."""
        if not below_threshold:
            return []

        # Expire old dedup entries
        import time
        now = time.time()
        TaskPlanner._recent_dispatches = [
            (title, ts) for title, ts in TaskPlanner._recent_dispatches
            if now - ts < TaskPlanner._DEDUP_WINDOW_SECONDS
        ]
        recent_titles = {title for title, _ in TaskPlanner._recent_dispatches}

        tasks: list[PlannedTask] = []

        for goal in below_threshold:
            if len(tasks) >= MAX_TASKS_PER_CYCLE:
                break
            new_tasks = self._tasks_for_goal(goal, scores)
            # Deduplicate with source-aware logic
            for t in new_tasks:
                if t.source == "github_issue" and t.backlog_item:
                    # For GitHub issues: check if still open instead of time-based dedup
                    if self._is_issue_still_open(t.backlog_item):
                        tasks.append(t)
                        recent_titles.add(t.title)
                    # If closed, skip silently
                elif t.title not in recent_titles:
                    # For non-GitHub tasks: time-based dedup
                    tasks.append(t)
                    recent_titles.add(t.title)

        # Record dispatched titles with timestamp
        for t in tasks:
            TaskPlanner._recent_dispatches.append((t.title, now))

        return tasks[:MAX_TASKS_PER_CYCLE]

    def _is_issue_still_open(self, backlog_item: str) -> bool:
        """Check if a GitHub issue is still open. backlog_item format: 'owner/repo#number'"""
        try:
            parts = backlog_item.split("#")
            if len(parts) != 2:
                return True  # Can't parse, assume open
            repo, number = parts[0], parts[1]
            result = subprocess.run(
                ["gh", "issue", "view", number, "-R", repo, "--json", "state"],
                capture_output=True, text=True, timeout=5,
            )
            if result.returncode == 0:
                state = json.loads(result.stdout).get("state", "OPEN")
                return state == "OPEN"
        except Exception:
            pass
        return True  # On error, assume open

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

    # ── Source 1: Night Runner Backlog ──────────────────────────────

    def _read_backlog(self) -> str:
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
        """Parse night-runner-projects.md for actionable backlog items.

        Supports both bullet lists (- item) and markdown tables (| # | Task | Source | Priority |).
        """
        items = []
        in_backlog = False

        for line in backlog.split("\n"):
            if "Task Backlog" in line or "Backlog" in line:
                in_backlog = True
                continue
            if in_backlog and line.startswith("## "):
                break
            if not in_backlog:
                continue

            # Skip table header/separator rows
            if line.startswith("| #") or line.startswith("|---"):
                continue

            text = ""
            priority = "MEDIUM"

            # Parse markdown table: | N | Task | Source | Priority |
            if line.startswith("|") and line.count("|") >= 4:
                cells = [c.strip() for c in line.split("|")]
                cells = [c for c in cells if c]
                if len(cells) >= 2 and not cells[0].startswith("#"):
                    text = cells[1].strip()
                    if len(cells) >= 4:
                        priority = cells[3].strip().upper()

            # Parse bullet list: - item or 1. item
            elif line.startswith("- ") or re.match(r"^\d+\.", line):
                text = re.sub(r"^[-\d.]+\s*", "", line).strip()

            # Clean up
            text = re.sub(r"\*\*([^*]+)\*\*", r"\1", text)
            text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)

            if not text or len(text) < 10 or "DONE" in priority:
                continue

            # Detect project from task text
            project = "~/Projects/aros-kernel"
            tl = text.lower()
            if "mini-claude-bot" in tl or "gateway" in tl:
                project = "~/Projects/mini-claude-bot"
            elif "centurion" in tl:
                project = "~/Projects/centurion"
            elif "telegram" in tl:
                project = "~/Projects/telegram-claude-hero"
            elif "meta-loop" in tl or "metaloop" in tl:
                project = "~/Projects/aros-meta-loop"
            elif "nirmana" in tl or "/away" in tl or "/back" in tl:
                project = "~/Projects/mini-claude-bot"

            is_new = any(kw in tl for kw in [
                "new feature", "add ", "create ", "implement ", "build ", "design ",
            ])
            items.append({
                "title": text[:80],
                "description": text,
                "project": project,
                "is_new_feature": is_new,
                "priority": priority,
                "est_minutes": 20,
                "source": "backlog",
            })
        return items

    # ── Source 2: GitHub Issues ─────────────────────────────────────

    def _fetch_github_issues(self) -> list[dict]:
        """Fetch open issues from tracked repos via gh CLI."""
        if self._issues_cache is not None:
            return self._issues_cache

        all_issues = []
        for repo in GITHUB_REPOS:
            try:
                result = subprocess.run(
                    ["gh", "issue", "list", "-R", repo, "--json",
                     "number,title,labels,url", "--limit", "5", "--state", "open"],
                    capture_output=True, text=True, timeout=10,
                )
                if result.returncode == 0 and result.stdout.strip():
                    issues = json.loads(result.stdout)
                    for issue in issues:
                        issue["repo"] = repo
                        issue["project"] = REPO_TO_PROJECT.get(repo, f"~/Projects/{repo.split('/')[-1]}")
                    all_issues.extend(issues)
            except (subprocess.TimeoutExpired, FileNotFoundError, json.JSONDecodeError) as e:
                logger.debug(f"Could not fetch issues from {repo}: {e}")

        self._issues_cache = all_issues
        return all_issues

    def _issues_to_tasks(self, issues: list[dict], goal_source: str) -> list[PlannedTask]:
        """Convert GitHub issues to PlannedTasks."""
        tasks = []
        for issue in issues[:3]:  # Max 3 from issues
            labels = [l.get("name", "") for l in issue.get("labels", [])]
            # Default GREEN — bug fixes and improvements are routine ops
            # Only explicitly feature/architecture labels get YELLOW
            is_feature = any(l in ("feature", "enhancement", "architecture", "design") for l in labels)
            authority = AuthorityLevel.YELLOW if is_feature else AuthorityLevel.GREEN

            tasks.append(PlannedTask(
                title=f"[{issue['repo'].split('/')[-1]}#{issue['number']}] {issue['title'][:60]}",
                description=f"Fix GitHub issue {issue['repo']}#{issue['number']}: {issue['title']}. URL: {issue.get('url', '')}",
                target_project=issue["project"],
                authority_level=authority,
                estimated_minutes=20,
                goal_source=goal_source,
                backlog_item=f"{issue['repo']}#{issue['number']}",
                source="github_issue",
            ))
        return tasks

    # ── Source 3: Chat History (vector search via mini-claude-bot API) ─

    def _search_chat_history(self, query: str) -> list[dict]:
        """Search chat history via mini-claude-bot's vector search API.

        Uses nomic-embed-text embeddings (768-dim) for semantic search
        across all chat history. Much more efficient than scanning raw files.
        """
        try:
            import httpx
            with httpx.Client(timeout=5.0) as client:
                resp = client.get(
                    "http://localhost:8000/api/chat/search",
                    params={"q": query, "limit": 5, "bot_id": "mini_claude_bot"},
                )
                if resp.status_code == 200:
                    results = resp.json()
                    if isinstance(results, list):
                        return results
                    return results.get("result", results.get("results", []))
        except Exception as e:
            logger.debug(f"Chat history vector search failed: {e}")
        return []

    def _chat_to_tasks(self, results: list[dict], goal_source: str) -> list[PlannedTask]:
        """Convert chat history results to PlannedTasks if they contain unfinished work."""
        tasks = []
        seen_titles = set()
        for r in results:
            content = r.get("content", "")
            # Look for actionable patterns in user messages
            if r.get("role") != "user":
                continue
            # Skip short messages
            if len(content) < 20:
                continue
            # Extract actionable phrases
            title = content[:80].strip()
            if title in seen_titles:
                continue
            seen_titles.add(title)

            # Detect project context
            project = "~/Projects/aros-kernel"
            if "mini-claude-bot" in content.lower():
                project = "~/Projects/mini-claude-bot"
            elif "telegram" in content.lower():
                project = "~/Projects/telegram-claude-hero"
            elif "centurion" in content.lower():
                project = "~/Projects/centurion"
            elif "meta-loop" in content.lower() or "metaloop" in content.lower():
                project = "~/Projects/aros-meta-loop"

            tasks.append(PlannedTask(
                title=title,
                description=content[:200],
                target_project=project,
                authority_level=AuthorityLevel.YELLOW,
                estimated_minutes=20,
                goal_source=goal_source,
                source="chat_history",
            ))
        return tasks[:2]  # Max 2 from chat

    # ── Goal-specific task generators ──────────────────────────────

    def _efficiency_tasks(self) -> list[PlannedTask]:
        """G2 low: find efficiency improvements from issues or backlog."""
        # Try GitHub issues — any open issue is work that improves efficiency
        issues = self._fetch_github_issues()
        if issues:
            # Prefer perf-labeled, but fall through to any issue
            perf_issues = [i for i in issues if any(
                l.get("name", "") in ("performance", "optimization", "efficiency")
                for l in i.get("labels", [])
            )]
            target = perf_issues if perf_issues else issues
            return self._issues_to_tasks(target[:2], "G2_efficient")

        # Fallback to backlog
        backlog = self._read_backlog()
        if backlog:
            items = self._parse_backlog_items(backlog)
            if items:
                return [PlannedTask(
                    title=items[0]["title"],
                    description=items[0]["description"],
                    target_project=items[0]["project"],
                    authority_level=AuthorityLevel.GREEN,
                    estimated_minutes=20,
                    goal_source="G2_efficient",
                    source="backlog",
                )]

        # Final fallback
        return [PlannedTask(
            title="Run and fix linting issues across all AROS projects",
            description="Run cargo clippy on aros-kernel, pytest on aros-meta-loop, go vet on telegram-claude-hero. Fix any warnings.",
            target_project="~/Projects/aros-kernel",
            authority_level=AuthorityLevel.GREEN,
            estimated_minutes=20,
            goal_source="G2_efficient",
            source="fallback",
        )]

    def _reliability_tasks(self) -> list[PlannedTask]:
        """G3 low: find bugs and test gaps from issues."""
        issues = self._fetch_github_issues()
        bug_issues = [i for i in issues if any(
            l.get("name", "") in ("bug", "fix", "test", "flaky")
            for l in i.get("labels", [])
        )]
        if bug_issues:
            return self._issues_to_tasks(bug_issues, "G3_reliable")

        return [PlannedTask(
            title="Add missing test coverage",
            description="Scan all AROS projects for modules with low test coverage, add tests for critical paths",
            target_project="~/Projects/aros-kernel",
            authority_level=AuthorityLevel.GREEN,
            estimated_minutes=30,
            goal_source="G3_reliable",
            source="fallback",
        )]

    def _ambitious_tasks(self) -> list[PlannedTask]:
        """G5 low: discover work from all three sources."""
        tasks: list[PlannedTask] = []

        # Source 1: GitHub issues (highest priority - concrete, trackable)
        issues = self._fetch_github_issues()
        if issues:
            tasks.extend(self._issues_to_tasks(issues[:2], "G5_ambitious"))

        # Source 2: Night-runner backlog
        if len(tasks) < MAX_TASKS_PER_CYCLE:
            backlog = self._read_backlog()
            if backlog:
                items = self._parse_backlog_items(backlog)
                for item in items[:2]:
                    if len(tasks) >= MAX_TASKS_PER_CYCLE:
                        break
                    tasks.append(PlannedTask(
                        title=item["title"],
                        description=item["description"],
                        target_project=item.get("project", "~/Projects/aros-kernel"),
                        authority_level=AuthorityLevel.YELLOW if item.get("is_new_feature") else AuthorityLevel.GREEN,
                        estimated_minutes=item.get("est_minutes", 20),
                        goal_source="G5_ambitious",
                        backlog_item=item["title"],
                        source="backlog",
                    ))

        # Source 3: Chat history (lowest priority - may be stale)
        if len(tasks) < MAX_TASKS_PER_CYCLE:
            chat_results = self._search_chat_history("unfinished TODO improve fix implement")
            if chat_results:
                tasks.extend(self._chat_to_tasks(chat_results, "G5_ambitious"))

        # Fallback if nothing found
        if not tasks:
            tasks.append(PlannedTask(
                title="Improve aros-kernel hardware awareness",
                description="Add event bus, throttle, or memory audit to aros-kernel based on gap analysis",
                target_project="~/Projects/aros-kernel",
                authority_level=AuthorityLevel.YELLOW,
                estimated_minutes=30,
                goal_source="G5_ambitious",
                source="fallback",
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
            source="fallback",
        )]

    def _alignment_tasks(self) -> list[PlannedTask]:
        return [PlannedTask(
            title="Review tool call patterns for alignment",
            description="Analyze recent tool call failures and improve error handling",
            target_project="~/Projects/mini-claude-bot",
            authority_level=AuthorityLevel.GREEN,
            estimated_minutes=15,
            goal_source="G4_aligned",
            source="fallback",
        )]

    def _self_knowledge_tasks(self) -> list[PlannedTask]:
        return [PlannedTask(
            title="Improve self-model calibration",
            description="Review self-model.toml accuracy against actual performance data",
            target_project="~/Projects/aros-meta-loop",
            authority_level=AuthorityLevel.GREEN,
            estimated_minutes=15,
            goal_source="G6_self_know",
            source="fallback",
        )]
