"""Trigger harness-loop execution via mini-claude-bot gateway API."""
import logging
import httpx

from aros_meta_loop.services.task_planner import PlannedTask, AuthorityLevel

logger = logging.getLogger(__name__)

GATEWAY_URL = "http://localhost:8000"
NIGHT_RUNNER_CHAT_ID = "-1003891385836"


class HarnessTrigger:
    """Triggers harness-loop execution via mini-claude-bot gateway."""

    def __init__(self, gateway_url: str = GATEWAY_URL, chat_id: str = NIGHT_RUNNER_CHAT_ID):
        self.gateway_url = gateway_url
        self.chat_id = chat_id

    def is_harness_running(self) -> bool:
        """Check if a harness-loop is already running."""
        try:
            with httpx.Client(timeout=5.0) as client:
                resp = client.get(f"{self.gateway_url}/api/gateway/sessions")
                if resp.status_code == 200:
                    sessions = resp.json()
                    for s in sessions:
                        chat_id = s.get("chat_id", "")
                        if chat_id.startswith("bg-") and s.get("busy", False):
                            logger.info(f"Harness-loop already running: {chat_id}")
                            return True
            return False
        except Exception as e:
            logger.warning(f"Could not check gateway sessions: {e}")
            return False

    def get_harness_state(self) -> dict | None:
        """Get current harness-loop state from gateway. Returns None if no harness exists."""
        try:
            with httpx.Client(timeout=5.0) as client:
                resp = client.get(
                    f"{self.gateway_url}/api/gateway/harness-status/{self.chat_id}",
                    params={"bot_id": "mini_claude_bot"},
                )
                if resp.status_code == 200:
                    data = resp.json()
                    jobs = data.get("jobs", [])
                    if jobs:
                        return jobs[0]  # Most recent job
            return None
        except Exception as e:
            logger.warning(f"Could not get harness state: {e}")
            return None

    def _is_resumable(self, harness: dict) -> bool:
        """Check if a harness-loop can be resumed."""
        h = harness.get("harness", {})
        phase = h.get("current_phase", "")
        pending = h.get("pending", 0)
        in_progress = h.get("in_progress", 0)
        done = h.get("done", 0)
        total = h.get("total", 0)

        # Resumable if: not complete, has pending or in_progress tasks
        if phase == "complete":
            return False
        if total == 0:
            return False
        if done == total:
            return False
        if pending > 0 or in_progress > 0:
            return True
        return False

    def _cleanup_harness(self) -> dict:
        """Archive a completed/stuck harness-loop via cleanup endpoint."""
        try:
            with httpx.Client(timeout=5.0) as client:
                resp = client.post(
                    f"{self.gateway_url}/api/gateway/cleanup/{self.chat_id}",
                    params={"bot_id": "mini_claude_bot"},
                )
                if resp.status_code == 200:
                    result = resp.json()
                    logger.info(f"Harness cleanup: {result}")
                    return {"status": "cleaned", "detail": result}
                else:
                    logger.warning(f"Cleanup returned {resp.status_code}")
                    return {"status": "cleanup_failed", "reason": f"HTTP {resp.status_code}"}
        except Exception as e:
            logger.warning(f"Cleanup failed: {e}")
            return {"status": "cleanup_failed", "reason": str(e)}

    def _resume_harness(self) -> dict:
        """Resume an existing unfinished harness-loop."""
        resume_prompt = (
            "Resume the harness-loop. The plan was already confirmed in a previous session. "
            "Enter the Execute Loop now. Pick up the next batch of ready tasks and execute them."
        )
        try:
            with httpx.Client(timeout=10.0) as client:
                resp = client.post(
                    f"{self.gateway_url}/api/gateway/send-background",
                    json={
                        "chat_id": self.chat_id,
                        "message": resume_prompt,
                        "bot_id": "mini_claude_bot",
                        "bot_token": "",
                    },
                )
                if resp.status_code == 200:
                    logger.info("Resumed existing harness-loop")
                    return {"status": "resumed"}
                else:
                    logger.error(f"Resume failed: {resp.status_code}")
                    return {"status": "error", "reason": f"resume returned {resp.status_code}"}
        except Exception as e:
            logger.error(f"Resume failed: {e}")
            return {"status": "error", "reason": str(e)}

    def verify_last_dispatch(self) -> dict:
        """Check if the last dispatched background task completed and produced results.

        Non-blocking: just checks current status, doesn't wait.
        Uses a multi-layered approach since the gateway's in-memory bg_task
        state can be lost on restart or after cleanup:
          1. Check background-status API (uses in-memory _bg_tasks)
          2. If idle, fall back to checking gateway sessions for busy bg- sessions
          3. If idle bg- session exists, check harness-status for completion info

        Returns: {status, completed, has_commits, details}
        """
        try:
            with httpx.Client(timeout=5.0) as client:
                resp = client.get(
                    f"{self.gateway_url}/api/gateway/background-status/{self.chat_id}",
                    params={"bot_id": "mini_claude_bot"},
                )
                if resp.status_code != 200:
                    return {"status": "unknown", "completed": False, "details": "API error"}

                bg_status = resp.json()
                status = bg_status.get("status", "unknown")

                # If background-status returned a real status, use it directly
                if status != "idle":
                    return self._build_verification_result(status, bg_status)

                # background-status returned "idle" — this can happen when:
                # - Gateway restarted (in-memory _bg_tasks lost)
                # - Cleanup removed the bg_task entry
                # - No task was ever dispatched
                # Fall back to checking gateway sessions directly
                return self._verify_via_sessions(client)
        except Exception as e:
            return {"status": "error", "completed": False, "details": str(e)}

    def _build_verification_result(self, status: str, bg_status: dict) -> dict:
        """Build a verification result dict from background-status response."""
        result = {
            "status": status,
            "completed": status == "completed",
            "elapsed_seconds": bg_status.get("elapsed_seconds", 0),
            "details": "",
        }

        if status == "completed":
            bg_result = bg_status.get("result", "")
            result["details"] = str(bg_result)[:200] if bg_result else "No output"
            result["has_commits"] = any(
                kw in str(bg_result).lower()
                for kw in ["commit", "push", "fixed", "merged", "close"]
            )
        elif status == "running":
            result["details"] = f"Still running ({bg_status.get('elapsed_seconds', 0):.0f}s)"
        elif status == "idle":
            result["details"] = "No active task"
        else:
            result["details"] = f"Status: {status}"

        return result

    def _verify_via_sessions(self, client: httpx.Client) -> dict:
        """Fallback verification using gateway sessions and harness-status.

        Called when background-status returns "idle" (bg_task entry missing).
        Checks if a bg- session for this chat_id exists and whether it's busy.
        """
        bg_prefix = f"bg-{self.chat_id}-"

        try:
            resp = client.get(f"{self.gateway_url}/api/gateway/sessions")
            if resp.status_code != 200:
                return {"status": "idle", "completed": False, "details": "No active task"}

            sessions = resp.json()
            bg_sessions = [
                s for s in sessions
                if s.get("chat_id", "").startswith(bg_prefix)
            ]

            if not bg_sessions:
                return {"status": "idle", "completed": False, "details": "No active task"}

            # Check if any bg session is still busy (task running)
            for s in bg_sessions:
                if s.get("busy", False):
                    elapsed = s.get("busy_seconds", 0)
                    logger.info(
                        f"verify_last_dispatch: bg session {s['chat_id']} is busy "
                        f"(detected via sessions fallback, {elapsed:.0f}s)"
                    )
                    return {
                        "status": "running",
                        "completed": False,
                        "elapsed_seconds": elapsed,
                        "details": f"Still running ({elapsed:.0f}s) [session: {s['chat_id']}]",
                    }

            # bg session exists but not busy — check harness-status for completion
            return self._verify_via_harness_status(client)
        except Exception as e:
            logger.debug(f"Session fallback check failed: {e}")
            return {"status": "idle", "completed": False, "details": "No active task"}

    def _verify_via_harness_status(self, client: httpx.Client) -> dict:
        """Check harness-status to determine if the last dispatch completed.

        Called when a bg- session exists but is not busy and bg_task entry is gone.
        """
        try:
            resp = client.get(
                f"{self.gateway_url}/api/gateway/harness-status/{self.chat_id}",
                params={"bot_id": "mini_claude_bot"},
            )
            if resp.status_code != 200:
                return {"status": "idle", "completed": False, "details": "No active task"}

            data = resp.json()
            jobs = data.get("jobs", [])
            if not jobs:
                return {"status": "idle", "completed": False, "details": "No active task"}

            latest = jobs[0]
            harness = latest.get("harness") or {}
            phase = harness.get("current_phase", "")
            done = harness.get("done", 0)
            total = harness.get("total", 0)
            project = harness.get("project_name", "unknown")

            if phase == "complete" or (total > 0 and done >= total):
                logger.info(
                    f"verify_last_dispatch: harness '{project}' complete "
                    f"({done}/{total}) [detected via harness-status fallback]"
                )
                return {
                    "status": "completed",
                    "completed": True,
                    "elapsed_seconds": 0,
                    "details": f"Harness '{project}' complete ({done}/{total} tasks)",
                    "has_commits": True,  # Assume commits if harness completed
                }

            if total > 0 and done < total:
                # Harness has remaining work but no active session — likely stuck
                logger.info(
                    f"verify_last_dispatch: harness '{project}' incomplete "
                    f"({done}/{total}) but no active session [harness-status fallback]"
                )
                return {
                    "status": "stalled",
                    "completed": False,
                    "elapsed_seconds": 0,
                    "details": f"Harness '{project}' stalled ({done}/{total} tasks done)",
                }

            return {"status": "idle", "completed": False, "details": "No active task"}
        except Exception as e:
            logger.debug(f"Harness-status fallback check failed: {e}")
            return {"status": "idle", "completed": False, "details": "No active task"}

    def check_and_resume_stuck(self, stale_minutes: int = 30) -> dict | None:
        """Check if a harness-loop is stuck and resume it.

        Returns resume result if a stuck harness was found, None otherwise.
        A harness is 'stuck' if it has pending/in_progress tasks but the
        background session is not busy (i.e., processing stopped).
        """
        harness_state = self.get_harness_state()
        if not harness_state:
            return None

        harness_info = harness_state.get("harness") or {}
        phase = harness_info.get("current_phase", "")
        pending = harness_info.get("pending", 0)
        in_progress = harness_info.get("in_progress", 0)
        done = harness_info.get("done", 0)
        total = harness_info.get("total", 0)
        project = harness_info.get("project_name", "unknown")

        # Not stuck if: complete, or no tasks, or all done
        if phase == "complete" or total == 0 or done == total:
            return None

        # Has remaining work — check if background session is still running
        if self.is_harness_running():
            return None  # Still active, not stuck

        # Has remaining work but no active session → stuck
        if pending > 0 or in_progress > 0:
            logger.warning(
                f"Harness '{project}' appears stuck: "
                f"{done}/{total} done, {pending} pending, {in_progress} in_progress, "
                f"but no active background session. Resuming."
            )
            result = self._resume_harness()
            result["reason"] = "auto_resume_stuck"
            result["project"] = project
            result["progress"] = f"{done}/{total}"
            return result

        return None

    def trigger_harness_loop(self, tasks: list[PlannedTask]) -> dict:
        """Trigger a harness-loop with the given tasks.

        Logic:
        1. If a harness-loop is actively running (busy bg session) -> skip
        2. If an unfinished harness exists and is resumable -> resume it
        3. If an unfinished harness exists but is NOT resumable -> cleanup/archive it
        4. Start a new harness-loop with the given tasks
        """
        if not tasks:
            return {"status": "skipped", "reason": "no tasks"}

        # Step 1: Check if actively running
        if self.is_harness_running():
            return {"status": "skipped", "reason": "harness already running"}

        # Step 2 & 3: Check for existing unfinished harness
        harness_state = self.get_harness_state()
        if harness_state:
            harness_info = harness_state.get("harness", {})
            phase = harness_info.get("current_phase", "")
            done = harness_info.get("done", 0)
            total = harness_info.get("total", 0)
            project = harness_info.get("project_name", "unknown")

            if phase != "complete" and done < total:
                # Unfinished harness exists
                if self._is_resumable(harness_state):
                    logger.info(f"Found resumable harness '{project}' ({done}/{total} done), resuming")
                    return self._resume_harness()
                else:
                    logger.info(f"Found non-resumable harness '{project}', cleaning up")
                    self._cleanup_harness()
                    # Fall through to create new harness

            elif phase == "complete":
                # Previous harness is done, archive it before starting new one
                logger.info(f"Previous harness '{project}' is complete, cleaning up before new dispatch")
                self._cleanup_harness()

        # Step 4: Filter to GREEN-only for auto-dispatch
        green_tasks = [t for t in tasks if t.authority_level == AuthorityLevel.GREEN]
        if not green_tasks:
            return {"status": "skipped", "reason": "no GREEN tasks to auto-dispatch"}

        # Format and dispatch new harness-loop
        prompt = self._format_prompt(green_tasks)
        try:
            with httpx.Client(timeout=10.0) as client:
                resp = client.post(
                    f"{self.gateway_url}/api/gateway/send-background",
                    json={
                        "chat_id": self.chat_id,
                        "message": prompt,
                        "bot_id": "mini_claude_bot",
                        "bot_token": "",
                    },
                )
                if resp.status_code == 200:
                    logger.info(f"Harness-loop triggered with {len(green_tasks)} GREEN tasks")
                    return {"status": "dispatched", "task_count": len(green_tasks)}
                else:
                    logger.error(f"Gateway returned {resp.status_code}: {resp.text}")
                    return {"status": "error", "reason": f"gateway returned {resp.status_code}"}
        except Exception as e:
            logger.error(f"Failed to trigger harness-loop: {e}")
            return {"status": "error", "reason": str(e)}

    def _format_prompt(self, tasks: list[PlannedTask]) -> str:
        """Format tasks into a harness-loop prompt for autonomous execution.

        The prompt tells Claude to:
        1. Spawn a Nirmana subagent to review and confirm the task plan
        2. If Nirmana approves, proceed directly to execution
        3. Skip [HARNESS_EXEC_READY] / /confirm (no Telegram in the loop)
        """
        task_descriptions = []
        for i, t in enumerate(tasks, 1):
            task_descriptions.append(
                f"{i}. **{t.title}** (target: {t.target_project}, est: {t.estimated_minutes}min)\n"
                f"   {t.description}"
            )

        tasks_text = "\n".join(task_descriptions)

        return (
            f"You are executing an autonomous improvement cycle triggered by MetaLoop.\n\n"
            f"The MetaLoop identified the following improvement tasks "
            f"(all GREEN authority - auto-execute):\n\n"
            f"{tasks_text}\n\n"
            f"## GitHub Issue Handling\n"
            f"For any task that references a GitHub issue (format: [repo#N]):\n"
            f"1. Read the issue details: `gh issue view N -R repo`\n"
            f"2. Fix the issue in the target project\n"
            f"3. Commit with message referencing the issue: `fix: description (fixes repo#N)`\n"
            f"4. Push the changes\n"
            f"5. Close the issue: `gh issue close N -R repo -c \"Fixed in commit <hash>\"`\n\n"
            f"Do NOT leave issues open after fixing them. The MetaLoop will re-dispatch\n"
            f"open issues, so closing them is how you signal completion.\n\n"
            f"## Step 1: Nirmana Review (REQUIRED)\n"
            f"Before executing, spawn an Eddie-Nirmana subagent to review this plan.\n"
            f"The subagent should:\n"
            f"- Read ~/eddie-nirmana/PERSONA.md for identity and decision authority\n"
            f"- Validate each task is within GREEN/YELLOW authority bounds\n"
            f"- Check alignment with Eddie's priorities (read ~/eddie-nirmana/state/night-runner-projects.md)\n"
            f"- Clarify any ambiguous requirements and refine task descriptions\n"
            f"- Return: approved (with any refinements) or rejected (with reason)\n\n"
            f"## Step 2: Execute (only if Nirmana approved)\n"
            f"If the Nirmana subagent approves the plan:\n"
            f"- Initialize .harness/ with the refined task DAG\n"
            f"- Enter the Execute Loop directly (skip [HARNESS_EXEC_READY], no /confirm needed)\n"
            f"- Commit and push results when done\n"
            f"- Output [HARNESS_COMPLETE] when finished\n\n"
            f"If Nirmana rejects, log the rejection reason and stop. Do NOT execute rejected tasks.\n\n"
            f"## Nirmana Autonomous Handling\n"
            f"You are running as Eddie-Nirmana (autonomous mode). Handle these scenarios:\n\n"
            f"### Clarification Requests\n"
            f"If the harness-loop asks clarifying questions about requirements:\n"
            f"- Read ~/eddie-nirmana/PERSONA.md for Eddie's decision-making style\n"
            f"- Read ~/eddie-nirmana/state/night-runner-projects.md for project context\n"
            f"- Answer based on Eddie's priorities: reliability > features, simplicity > complexity\n"
            f"- For GREEN tasks (bug fixes, tests): bias toward 'fix it the simplest way'\n"
            f"- For ambiguous scope: choose the minimal viable scope\n"
            f"- Never block on clarification — make a reasonable decision and proceed\n\n"
            f"### Plan Confirmation\n"
            f"If the harness-loop presents a plan and asks for /confirm:\n"
            f"- Review the plan for safety (no destructive operations, no force-pushes)\n"
            f"- If plan looks reasonable: send /confirm immediately\n"
            f"- If plan has RED flags (deleting data, modifying auth, changing APIs): reject and log\n\n"
            f"### Stuck/Stalled Detection\n"
            f"If the harness-loop appears stuck or asks for /resume:\n"
            f"- Send /resume to continue execution\n"
            f"- If a task is blocked, skip it and proceed with remaining tasks\n"
            f"- If all tasks are blocked, output [HARNESS_BLOCKED:all:need_human_input]"
        )
