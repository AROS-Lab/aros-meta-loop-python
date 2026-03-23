"""State manager for AROS — handles TOML config, evolution log, and signal queue."""
import json
import logging
import os
import tomllib
import uuid
from datetime import datetime, timezone
from pathlib import Path

from aros_meta_loop.config import AROS_STATE_DIR

logger = logging.getLogger(__name__)


class StateManager:
    def __init__(self, state_dir: Path | None = None):
        self.state_dir = state_dir or AROS_STATE_DIR

    def read_self_model(self) -> dict:
        return self._read_toml("self-model.toml")

    def read_policy(self) -> dict:
        return self._read_toml("policy.toml")

    def read_cadence(self) -> dict:
        config = self._read_toml("meta-cognition.toml")
        return config.get("cadence", {})

    def read_goals(self) -> dict:
        config = self._read_toml("meta-cognition.toml")
        return config.get("goals", {})

    def write_snapshot(self, filename: str, data: dict) -> None:
        """Atomic write: write to .tmp then os.replace()."""
        target = self.state_dir / filename
        tmp = target.with_suffix(".tmp")
        content = self._dict_to_toml(data)
        tmp.write_text(content)
        os.replace(str(tmp), str(target))
        logger.info(f"Wrote snapshot: {filename}")

    def append_evolution(self, entry: dict) -> None:
        """Append a JSON line to evolution-log.jsonl."""
        path = self.state_dir / "evolution-log.jsonl"
        entry.setdefault("timestamp", datetime.now(timezone.utc).isoformat())
        with open(path, "a") as f:
            f.write(json.dumps(entry) + "\n")

    def read_evolution_log(self, limit: int = 50) -> list[dict]:
        """Read last N entries from evolution log."""
        path = self.state_dir / "evolution-log.jsonl"
        if not path.exists():
            return []
        lines = path.read_text().strip().split("\n")
        entries = []
        for line in lines[-limit:]:
            if line.strip():
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        return entries

    def log_task_generation(self, cycle_num: int, tasks: list[dict], trigger_results: dict) -> None:
        """Log auto-generated tasks to evolution-log.jsonl."""
        entry = {
            "type": "task_generation",
            "cycle_num": cycle_num,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "tasks_generated": tasks,
            "trigger_results": trigger_results,
        }
        self.append_evolution(entry)

    def push_signal(self, signal_dict: dict) -> None:
        """Write signal as timestamped JSON file in signals/."""
        signals_dir = self.state_dir / "signals"
        signals_dir.mkdir(exist_ok=True)
        ts = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S%f")
        uid = uuid.uuid4().hex[:8]
        filename = f"{ts}_{uid}.json"
        target = signals_dir / filename
        tmp = target.with_suffix(".tmp")
        tmp.write_text(json.dumps(signal_dict))
        os.replace(str(tmp), str(target))

    def drain_signals(self) -> list[dict]:
        """Read and delete all signal files, sorted by filename (timestamp order)."""
        signals_dir = self.state_dir / "signals"
        if not signals_dir.exists():
            return []
        signals = []
        for f in sorted(signals_dir.glob("*.json")):
            try:
                data = json.loads(f.read_text())
                signals.append(data)
                f.unlink()
            except (json.JSONDecodeError, OSError) as e:
                logger.warning(f"Failed to read signal {f}: {e}")
        return signals

    def write_last_commit(self, summary: dict) -> None:
        """Write last commit summary for Channel G (Persist → Perceive)."""
        path = self.state_dir / "state" / "last_commit.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(summary))

    def read_last_commit(self) -> dict | None:
        """Read last commit summary for Channel G (Persist → Perceive)."""
        path = self.state_dir / "state" / "last_commit.json"
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text())
        except Exception:
            return None

    # --- Pending approval queue for YELLOW tasks ---

    def _pending_approvals_path(self) -> Path:
        return self.state_dir / "pending-approvals.json"

    def _read_pending_approvals(self) -> list[dict]:
        path = self._pending_approvals_path()
        if not path.exists():
            return []
        try:
            return json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            return []

    def _write_pending_approvals(self, approvals: list[dict]) -> None:
        path = self._pending_approvals_path()
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(approvals, indent=2))
        os.replace(str(tmp), str(path))

    def add_pending_approval(self, approval: dict) -> str:
        """Add a YELLOW task to the pending approval queue. Returns approval_id."""
        approval_id = uuid.uuid4().hex[:12]
        entry = {
            "id": approval_id,
            "task": approval.get("task", approval),
            "cycle_num": approval.get("cycle_num"),
            "created_at": datetime.now(timezone.utc).isoformat(),
            "status": "pending",
        }
        approvals = self._read_pending_approvals()
        approvals.append(entry)
        self._write_pending_approvals(approvals)
        logger.info(f"Added pending approval: {approval_id}")
        return approval_id

    def get_pending_approvals(self) -> list[dict]:
        """Get all pending approvals."""
        return self._read_pending_approvals()

    def approve_task(self, approval_id: str) -> dict | None:
        """Mark approval as approved, return the task details."""
        approvals = self._read_pending_approvals()
        for entry in approvals:
            if entry["id"] == approval_id and entry["status"] == "pending":
                entry["status"] = "approved"
                entry["approved_at"] = datetime.now(timezone.utc).isoformat()
                self._write_pending_approvals(approvals)
                logger.info(f"Approved pending task: {approval_id}")
                return entry
        return None

    def reject_task(self, approval_id: str, reason: str = "") -> dict | None:
        """Mark approval as rejected with reason."""
        approvals = self._read_pending_approvals()
        for entry in approvals:
            if entry["id"] == approval_id and entry["status"] == "pending":
                entry["status"] = "rejected"
                entry["rejected_at"] = datetime.now(timezone.utc).isoformat()
                entry["reject_reason"] = reason
                self._write_pending_approvals(approvals)
                logger.info(f"Rejected pending task: {approval_id} — {reason}")
                return entry
        return None

    def has_urgent(self) -> bool:
        """Check if any signal has priority='urgent' without consuming."""
        signals_dir = self.state_dir / "signals"
        if not signals_dir.exists():
            return False
        for f in signals_dir.glob("*.json"):
            try:
                data = json.loads(f.read_text())
                if data.get("priority") == "urgent":
                    return True
            except (json.JSONDecodeError, OSError):
                continue
        return False

    def _read_toml(self, filename: str) -> dict:
        path = self.state_dir / filename
        if not path.exists():
            return {}
        try:
            with open(path, "rb") as f:
                return tomllib.load(f)
        except Exception as e:
            logger.warning(f"Failed to read {filename}: {e}")
            return {}

    @staticmethod
    def _dict_to_toml(data: dict, prefix: str = "") -> str:
        """Minimal dict-to-TOML serializer (handles flat and nested sections)."""
        lines = []
        nested = {}
        for k, v in data.items():
            if isinstance(v, dict):
                nested[k] = v
            elif isinstance(v, str):
                lines.append(f'{k} = "{v}"')
            elif isinstance(v, bool):
                lines.append(f'{k} = {"true" if v else "false"}')
            elif isinstance(v, (int, float)):
                lines.append(f'{k} = {v}')
            else:
                lines.append(f'{k} = "{v}"')
        for section, values in nested.items():
            full_key = f"{prefix}.{section}" if prefix else section
            lines.append(f"\n[{full_key}]")
            lines.append(StateManager._dict_to_toml(values, full_key).strip())
        return "\n".join(lines) + "\n"
