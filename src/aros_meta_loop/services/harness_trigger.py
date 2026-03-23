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
                    # Check for any busy background sessions
                    for s in sessions:
                        chat_id = s.get("chat_id", "")
                        if chat_id.startswith("bg-") and s.get("busy", False):
                            logger.info(f"Harness-loop already running: {chat_id}")
                            return True
            return False
        except Exception as e:
            logger.warning(f"Could not check gateway sessions: {e}")
            return False  # Assume not running if can't check

    def trigger_harness_loop(self, tasks: list[PlannedTask]) -> dict:
        """Trigger a harness-loop with the given tasks."""
        if not tasks:
            return {"status": "skipped", "reason": "no tasks"}

        if self.is_harness_running():
            return {"status": "skipped", "reason": "harness already running"}

        # Filter to GREEN-only for auto-dispatch
        green_tasks = [t for t in tasks if t.authority_level == AuthorityLevel.GREEN]
        if not green_tasks:
            return {"status": "skipped", "reason": "no GREEN tasks to auto-dispatch"}

        # Format the harness-loop prompt
        prompt = self._format_prompt(green_tasks)

        # Fire-and-forget: POST to gateway
        try:
            with httpx.Client(timeout=10.0) as client:
                resp = client.post(
                    f"{self.gateway_url}/api/gateway/send-background",
                    json={
                        "chat_id": self.chat_id,
                        "message": prompt,
                        "bot_id": "mini_claude_bot",
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
        """Format tasks into a harness-loop prompt with Nirmana persona."""
        task_descriptions = []
        for i, t in enumerate(tasks, 1):
            task_descriptions.append(
                f"{i}. **{t.title}** (target: {t.target_project}, est: {t.estimated_minutes}min)\n"
                f"   {t.description}"
            )

        tasks_text = "\n".join(task_descriptions)

        return (
            f"You are Eddie-Nirmana executing an autonomous improvement cycle. "
            f"Read ~/eddie-nirmana/PERSONA.md for your identity.\n\n"
            f"The MetaLoop identified the following improvement tasks (all GREEN authority - auto-execute):\n\n"
            f"{tasks_text}\n\n"
            f"Execute these tasks using the harness-loop skill. "
            f"Create a .harness/ project, decompose into engineering tasks, and execute. "
            f"Commit and push results when done."
        )
