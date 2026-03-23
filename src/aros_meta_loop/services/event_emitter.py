"""Fire-and-forget event emitter — writes to meta_events DB table."""
import json
import logging
from datetime import datetime, timezone

from aros_meta_loop.db.engine import get_db, db_write_lock

logger = logging.getLogger(__name__)


class EventEmitter:
    VALID_TYPES = {
        "tool_call", "task_complete", "task_failed",
        "human_feedback", "cron_execution",
        "session_start", "session_end", "policy_delta",
    }

    def emit_event(
        self,
        bot_id: str,
        event_type: str,
        session_id: str | None = None,
        data: dict | None = None,
    ) -> None:
        """Write event to meta_events table. Fire-and-forget."""
        try:
            data_json = json.dumps(data) if data else None
            with db_write_lock():
                db = get_db()
                db.execute(
                    """INSERT INTO meta_events (bot_id, event_type, session_id, data, created_at)
                       VALUES (?, ?, ?, ?, ?)""",
                    (bot_id, event_type, session_id, data_json,
                     datetime.now(timezone.utc).isoformat()),
                )
                db.commit()
        except Exception as e:
            logger.warning(f"Failed to emit event {event_type}: {e}")
