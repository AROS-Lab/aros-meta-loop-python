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
            f"If Nirmana rejects, log the rejection reason and stop. Do NOT execute rejected tasks."
        )
