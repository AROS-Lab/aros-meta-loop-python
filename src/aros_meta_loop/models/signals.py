"""Data models for meta-loop signals and critic output."""
from dataclasses import dataclass, field
from enum import Enum
from datetime import datetime


class CriticAction(str, Enum):
    POLICY_UPDATE = "POLICY_UPDATE"
    MEMORY_WRITE = "MEMORY_WRITE"
    TOOL_ACTION = "TOOL_ACTION"
    ALERT = "ALERT"
    NO_ACTION = "NO_ACTION"


class PermissionLevel(str, Enum):
    AUTO_APPROVE = "AUTO_APPROVE"
    HUMAN_REVIEW = "HUMAN_REVIEW"
    NEVER = "NEVER"


@dataclass
class Signal:
    source: str
    priority: str  # "low", "normal", "high", "urgent"
    timestamp: str  # ISO format
    payload: dict = field(default_factory=dict)
    ttl: int = 3600  # seconds
    validation_status: str = "unvalidated"  # "validated", "unvalidated", "failed"

    def is_expired(self) -> bool:
        created = datetime.fromisoformat(self.timestamp)
        return (datetime.utcnow() - created).total_seconds() > self.ttl


@dataclass
class CriticOutput:
    action: CriticAction
    reason: str
    changes: list[dict] = field(default_factory=list)
    permission_level: PermissionLevel = PermissionLevel.AUTO_APPROVE
    confidence: float = 0.5


@dataclass
class PolicyChange:
    change_id: str
    section: str  # e.g. "harness", "meta_loop", "cadence"
    key: str
    old_value: str | None
    new_value: str
    permission_level: PermissionLevel
    reason: str
    status: str = "pending"  # "pending", "approved", "rejected", "applied"
