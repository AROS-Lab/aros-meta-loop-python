"""Meta Loop Engine — implements the 6-step PERCEIVE-UPDATE-CRITIQUE-REVISE-IDENTITY-PERSIST cycle."""
import asyncio
import json
import logging
import uuid
from datetime import datetime, timezone, timedelta
from pathlib import Path

from aros_meta_loop.db.engine import get_db, db_write_lock
from aros_meta_loop.models.signals import (
    CriticAction, CriticOutput, PermissionLevel, PolicyChange, Signal,
)
from aros_meta_loop.services.event_emitter import EventEmitter
from aros_meta_loop.services.metrics import L1Collector, L2Evaluator, L3SignalDeriver
from aros_meta_loop.services.state_manager import StateManager
from aros_meta_loop.services.task_planner import TaskPlanner

logger = logging.getLogger(__name__)


class CadenceController:
    """Controls cycle cadence: rate limits, min intervals, and emergency caps."""

    def __init__(self, state_manager: StateManager):
        self.state = state_manager
        self._cycle_timestamps: list[str] = []
        self._emergency_timestamps: list[str] = []

    def can_run_cycle(self) -> tuple[bool, str]:
        """Check if a cycle can run based on cadence limits."""
        cadence = self.state.read_cadence()
        now = datetime.now(timezone.utc)

        # Clean old timestamps (>24h)
        cutoff_day = (now - timedelta(hours=24)).isoformat()
        cutoff_hour = (now - timedelta(hours=1)).isoformat()
        self._cycle_timestamps = [t for t in self._cycle_timestamps if t > cutoff_day]
        self._emergency_timestamps = [t for t in self._emergency_timestamps if t > cutoff_hour]

        # Check limits
        max_per_hour = cadence.get("max_cycles_per_hour", 4)
        max_per_day = cadence.get("max_cycles_per_day", 20)
        min_interval = cadence.get("min_interval_between_cycles_seconds", 180)

        cycles_last_hour = sum(1 for t in self._cycle_timestamps if t > cutoff_hour)
        cycles_today = len(self._cycle_timestamps)

        if cycles_today >= max_per_day:
            return False, "daily limit reached"
        if cycles_last_hour >= max_per_hour:
            return False, "hourly limit reached"
        if self._cycle_timestamps:
            last = datetime.fromisoformat(self._cycle_timestamps[-1])
            if (now - last).total_seconds() < min_interval:
                return False, "min interval not met"

        return True, "ok"

    def record_cycle(self) -> None:
        """Record that a cycle completed."""
        self._cycle_timestamps.append(datetime.now(timezone.utc).isoformat())

    def can_emergency(self) -> bool:
        """Check if an emergency cycle is allowed."""
        cadence = self.state.read_cadence()
        now = datetime.now(timezone.utc)
        cutoff_hour = (now - timedelta(hours=1)).isoformat()
        self._emergency_timestamps = [t for t in self._emergency_timestamps if t > cutoff_hour]
        max_emergency = cadence.get("max_emergency_per_hour", 2)
        return len(self._emergency_timestamps) < max_emergency

    def record_emergency(self) -> None:
        """Record that an emergency cycle was used."""
        self._emergency_timestamps.append(datetime.now(timezone.utc).isoformat())


class PolicyChangeClassifier:
    """Classifies policy changes into AUTO_APPROVE, HUMAN_REVIEW, or NEVER."""

    # Changes that are NEVER allowed
    NEVER_PATTERNS = {
        ("meta_events", None),    # Modifying L1 event log
        ("critic", "enabled"),     # Disabling critic
        ("permissions", "human_review_required"),  # Removing human-review requirement
        ("adversarial", None),     # Modifying adversarial critique config
    }

    # Changes that always require human review
    HUMAN_REVIEW_SECTIONS = {"goals", "meta_goals"}

    # Capacity parameters: lower = tighter (decreasing is auto-approve)
    CONSTRAINT_KEYS = {
        "retry_limit", "max_chain_depth", "max_batch_size",
        "max_cycles_per_hour", "max_cycles_per_day",
        "max_emergency_per_hour", "shadow_test_window",
    }

    # Threshold parameters: higher = tighter (increasing is auto-approve)
    THRESHOLD_KEYS = {"drift_threshold"}

    # Known safe sections for default auto-approve
    SAFE_SECTIONS = {"harness", "meta_loop", "cadence"}

    @classmethod
    def classify(cls, change: PolicyChange, current_policy: dict | None = None,
                 current_value=None) -> PermissionLevel:
        """Classify a policy change into permission level."""
        section = change.section
        key = change.key

        # NEVER: protected sections
        for pattern_section, pattern_key in cls.NEVER_PATTERNS:
            if section == pattern_section:
                if pattern_key is None or key == pattern_key:
                    return PermissionLevel.NEVER

        # HUMAN_REVIEW: meta-goal changes
        if section in cls.HUMAN_REVIEW_SECTIONS:
            return PermissionLevel.HUMAN_REVIEW

        # Check direction: tightening vs loosening
        old_val = change.old_value
        new_val = change.new_value

        if old_val and new_val:
            try:
                old_num = float(old_val)
                new_num = float(new_val)

                if key in cls.CONSTRAINT_KEYS:
                    # For capacity params: decreasing is tightening (auto), increasing is loosening (review)
                    if new_num <= old_num:
                        return PermissionLevel.AUTO_APPROVE
                    # Check if within +20%
                    if new_num <= old_num * 1.2:
                        return PermissionLevel.AUTO_APPROVE
                    return PermissionLevel.HUMAN_REVIEW
                elif key in cls.THRESHOLD_KEYS:
                    # For thresholds: increasing is tightening (auto), decreasing is loosening (review)
                    if new_num >= old_num:
                        return PermissionLevel.AUTO_APPROVE
                    if new_num >= old_num * 0.8:
                        return PermissionLevel.AUTO_APPROVE
                    return PermissionLevel.HUMAN_REVIEW
                else:
                    # Unknown numeric param: auto-approve if within +/-20%
                    if abs(old_num) > 0.001:
                        if abs(new_num - old_num) / abs(old_num) <= 0.2:
                            return PermissionLevel.AUTO_APPROVE
                    else:
                        # Near-zero old value: auto-approve small absolutes
                        if abs(new_num - old_num) <= 0.2:
                            return PermissionLevel.AUTO_APPROVE
                    return PermissionLevel.HUMAN_REVIEW
            except (ValueError, ZeroDivisionError):
                pass

        # Default: auto-approve for known sections, human review otherwise
        if section in cls.SAFE_SECTIONS:
            return PermissionLevel.AUTO_APPROVE

        return PermissionLevel.HUMAN_REVIEW


class MetaLoopEngine:
    """Implements the 7-step meta-cognition cycle."""

    def __init__(self, state_manager: StateManager | None = None, bot_id: str = "default"):
        self.state = state_manager or StateManager()
        self.bot_id = bot_id
        self.collector = L1Collector()
        self.evaluator = L2Evaluator(state_manager=self.state)
        self.deriver = L3SignalDeriver()
        self.emitter = EventEmitter()
        self.cadence = CadenceController(self.state)
        self._lock = asyncio.Lock()
        self._abort_flag = False
        self._abort_reason = ""
        self._running = False
        # Staged changes (populated during cycle, committed at PERSIST)
        self._staged_policy: dict | None = None
        self._staged_self_model: dict | None = None
        self._cycle_log: dict = {}
        # Channel G: Last persist confirmation for next perceive
        self._last_persist_confirmation: dict | None = None
        # Nirmana autonomous mode
        self._nirmana_mode = False
        self._nirmana_briefing: list[dict] = []
        # Concurrency / cadence controls (legacy — kept for backward compat with existing tests)
        self._cycle_count_hour = 0
        self._cycle_count_day = 0
        self._emergency_count_hour = 0
        self._last_cycle_time: datetime | None = None
        self._hour_reset_time = datetime.now(timezone.utc)
        self._day_reset_time = datetime.now(timezone.utc)

    @property
    def is_running(self) -> bool:
        return self._running

    def abort(self, reason: str = "manual") -> None:
        """Set abort flag — current step finishes, then staged changes are discarded."""
        self._abort_flag = True
        self._abort_reason = reason
        logger.warning(f"Abort requested: {reason}")

    async def run_cycle(self, trigger: str = "scheduled") -> dict:
        """
        Execute the full 6-step cycle.
        Returns cycle result dict with step outcomes.
        """
        if self._lock.locked():
            return {"status": "skipped", "reason": "cycle already running"}

        async with self._lock:
            # Cadence limit check (legacy)
            rejection = self._check_cadence_limits(trigger)
            if rejection:
                return {"status": "skipped", "reason": rejection}

            # CadenceController check
            can_run, reason = self.cadence.can_run_cycle()
            if not can_run:
                return {"status": "throttled", "reason": reason}

            self._running = True
            self._staged_policy = None
            self._staged_self_model = None

            cycle_num = self._get_next_cycle_num()
            started_at = datetime.now(timezone.utc).isoformat()
            self._cycle_log = {
                "cycle_num": cycle_num,
                "trigger": trigger,
                "started_at": started_at,
                "steps_completed": 0,
            }

            # Read timeouts from cadence config
            cadence = self.state.read_cadence()
            step_timeout = cadence.get("per_step_timeout_seconds", 60)
            total_timeout = cadence.get("total_cycle_timeout_seconds", 300)

            try:
                # Wrap entire cycle with total timeout
                result = await asyncio.wait_for(
                    self._run_cycle_steps(trigger, step_timeout),
                    timeout=total_timeout,
                )
                # Update cadence counters on completion
                self._cycle_count_hour += 1
                self._cycle_count_day += 1
                if trigger == "emergency":
                    self._emergency_count_hour += 1
                self._last_cycle_time = datetime.now(timezone.utc)
                # Record in CadenceController
                self.cadence.record_cycle()
                if trigger == "emergency":
                    self.cadence.record_emergency()
                return result

            except asyncio.TimeoutError:
                logger.error(f"Cycle timed out after {total_timeout}s")
                self._cycle_log["error"] = f"total_cycle_timeout ({total_timeout}s)"
                return self._finalize_cycle("failed")
            except Exception as e:
                logger.error(f"Cycle failed: {e}", exc_info=True)
                self._cycle_log["error"] = str(e)
                return self._finalize_cycle("failed")
            finally:
                self._running = False
                self._abort_flag = False
                self._abort_reason = ""

    async def _run_cycle_steps(self, trigger: str, step_timeout: float) -> dict:
        """Inner cycle steps, separated for timeout wrapping."""
        # Step 1: PERCEIVE
        perceive_data = self._perceive()
        self._cycle_log["perceive_data"] = perceive_data
        self._cycle_log["steps_completed"] = 1
        if self._phase_gate("perceive"):
            return self._finalize_cycle("aborted")

        # Step 2: SELF-MODEL UPDATE
        self._self_model_update(perceive_data)
        self._cycle_log["steps_completed"] = 2
        if self._phase_gate("self_model_update"):
            return self._finalize_cycle("aborted")

        # Step 3: CRITIQUE
        critic_output = self._critique(perceive_data)
        self._cycle_log["critique_output"] = {
            "action": critic_output.action.value,
            "reason": critic_output.reason,
            "confidence": critic_output.confidence,
        }

        # Channel A: Critique → Self-Model re-query (max 2)
        requery_count = 0
        while (critic_output.action == CriticAction.NO_ACTION
               and perceive_data["l1_metrics"].get("event_count", 0) == 0
               and requery_count < 2):
            requery_count += 1
            perceive_data = self._perceive()
            critic_output = self._critique(perceive_data)
        self._cycle_log["requery_count"] = requery_count

        self._cycle_log["steps_completed"] = 3
        if self._phase_gate("critique"):
            return self._finalize_cycle("aborted")

        # Step 4: POLICY REVISION
        policy_changes = self._policy_revision(critic_output, perceive_data)
        self._cycle_log["policy_changes"] = [
            {"section": pc.section, "key": pc.key,
             "old": pc.old_value, "new": pc.new_value,
             "permission": pc.permission_level.value}
            for pc in policy_changes
        ]

        # Channel C: Policy → Shadow Test
        if policy_changes:
            shadow_result = self._shadow_test(policy_changes, perceive_data)
            self._cycle_log["shadow_test"] = shadow_result
            if not shadow_result["passed"]:
                # Channel D: Shadow → Critique feedback
                critic_output = CriticOutput(
                    action=CriticAction.ALERT,
                    reason=f"Shadow test failed: {shadow_result['reason']}",
                    permission_level=PermissionLevel.HUMAN_REVIEW,
                    confidence=0.4,
                )
                policy_changes = self._policy_revision(critic_output, perceive_data)

        self._cycle_log["steps_completed"] = 4
        if self._phase_gate("policy_revision"):
            return self._finalize_cycle("aborted")

        # Step 5: IDENTITY CHECK
        identity_verdict = self._identity_check(policy_changes, perceive_data)
        self._cycle_log["identity_verdict"] = identity_verdict

        # Channel E: Identity → Perceive (drift restart)
        # If rejected_high_drift, restart cycle from perceive (max 1 restart)
        drift_restart = self._cycle_log.get("drift_restart", False)
        if identity_verdict == "rejected_high_drift" and not drift_restart:
            logger.info("Channel E: drift restart — restarting cycle from perceive")
            self._cycle_log["drift_restart"] = True
            self._staged_policy = None
            self._staged_self_model = None
            # Restart from step 1
            return await self._run_cycle_steps(self._cycle_log.get("trigger", "scheduled"), step_timeout)

        # Channel B: Identity → Policy rejection loop (max 3 rounds)
        identity_rounds = 0
        while identity_verdict.startswith("rejected_") and identity_rounds < 3:
            identity_rounds += 1
            # Constrain changes further
            for c in policy_changes:
                c.permission_level = PermissionLevel.HUMAN_REVIEW
            identity_verdict = self._identity_check(policy_changes, perceive_data)

        if identity_rounds >= 3 and identity_verdict.startswith("rejected_"):
            # Escalate
            escalation_output = CriticOutput(
                action=CriticAction.ALERT,
                reason=f"Identity check rejected after 3 rounds: {identity_verdict}",
                permission_level=PermissionLevel.HUMAN_REVIEW,
            )
            self._queue_alert(escalation_output)

        self._cycle_log["identity_rounds"] = identity_rounds
        self._cycle_log["steps_completed"] = 5
        if self._phase_gate("identity_check"):
            return self._finalize_cycle("aborted")

        # Step 6: PERSIST
        self._persist(policy_changes, identity_verdict)
        self._cycle_log["steps_completed"] = 6

        # Step 7: PLAN (autonomous task generation)
        planned_tasks = self._plan_tasks(perceive_data)
        if planned_tasks:
            self._cycle_log["planned_tasks"] = planned_tasks
            self._cycle_log["steps_completed"] = 7

        return self._finalize_cycle("completed")

    def _perceive(self) -> dict:
        """Step 1: Gather L1/L2/L3 metrics and drain signal queue."""
        l1 = self.collector.collect(self.bot_id)
        l2 = self.evaluator.evaluate(l1)
        self_model = self.state.read_self_model()
        l3 = self.deriver.derive(l1, l2, self_model)
        signals = self.state.drain_signals()

        # Channel G: Read last commit summary from previous persist
        last_commit = self.state.read_last_commit()

        return {
            "l1_metrics": l1,
            "l2_scores": l2,
            "l3_signals": [
                {"source": s.source, "priority": s.priority, "payload": s.payload}
                for s in l3
            ],
            "queued_signals": signals,
            "current_policy": self.state.read_policy(),
            "current_cadence": self.state.read_cadence(),
            "last_commit": last_commit,
        }

    def _self_model_update(self, perceive_data: dict) -> None:
        """Step 2: Update self-model based on perceived data."""
        l1 = perceive_data["l1_metrics"]
        l2 = perceive_data["l2_scores"]

        current_model = self.state.read_self_model()

        # Update calibration based on L2 scores
        calibration = current_model.get("calibration", {})
        calibration["confidence_accuracy"] = l2.get("G6_self_know", 0.0)
        calibration["last_updated"] = datetime.now(timezone.utc).isoformat()

        # Update capabilities summary
        capabilities = current_model.get("capabilities", {})
        capabilities["tool_success_rate"] = l1.get("tool_call_success_rate", 0.0)
        capabilities["avg_tokens_per_task"] = l1.get("tokens_per_task", 0.0)
        capabilities["task_throughput"] = l1.get("task_count", 0)

        self._staged_self_model = {
            "capabilities": capabilities,
            "calibration": calibration,
        }

    def _critique(self, perceive_data: dict) -> CriticOutput:
        """Step 3: Evaluate current state against meta-goals and produce action."""
        l2 = perceive_data["l2_scores"]
        below = l2.get("below_threshold", [])

        if not below:
            return CriticOutput(
                action=CriticAction.NO_ACTION,
                reason="All meta-goals above threshold",
                confidence=0.9,
            )

        # Determine action based on severity
        if len(below) >= 3:
            return CriticOutput(
                action=CriticAction.ALERT,
                reason=f"Multiple goals below threshold: {below}",
                changes=[{"goals_below": below}],
                permission_level=PermissionLevel.HUMAN_REVIEW,
                confidence=0.7,
            )

        # Single or two goals below — suggest policy tuning
        changes = []
        for goal in below:
            if goal == "G2_efficient":
                changes.append({"type": "tune", "section": "harness", "key": "max_batch_size",
                               "direction": "decrease", "reason": "Improve token efficiency"})
            elif goal == "G3_reliable":
                changes.append({"type": "tune", "section": "harness", "key": "retry_limit",
                               "direction": "increase", "reason": "Improve reliability"})

        return CriticOutput(
            action=CriticAction.POLICY_UPDATE,
            reason=f"Goals below threshold: {below}",
            changes=changes,
            permission_level=PermissionLevel.AUTO_APPROVE,
            confidence=0.6,
        )

    def _policy_revision(self, critic_output: CriticOutput, perceive_data: dict) -> list[PolicyChange]:
        """Step 4: Propose specific policy changes based on critique."""
        if critic_output.action == CriticAction.NO_ACTION:
            return []

        policy = perceive_data["current_policy"]
        changes = []

        for change_spec in critic_output.changes:
            if change_spec.get("type") == "tune":
                section = change_spec["section"]
                key = change_spec["key"]
                direction = change_spec["direction"]
                current = policy.get(section, {}).get(key)

                if current is not None:
                    # Apply +/-20% tuning
                    if direction == "increase":
                        new_val = int(current * 1.2) if isinstance(current, int) else round(current * 1.2, 2)
                    else:
                        new_val = max(1, int(current * 0.8)) if isinstance(current, int) else round(current * 0.8, 2)

                    changes.append(PolicyChange(
                        change_id=uuid.uuid4().hex[:8],
                        section=section,
                        key=key,
                        old_value=str(current),
                        new_value=str(new_val),
                        permission_level=PermissionLevel.AUTO_APPROVE,
                        reason=change_spec.get("reason", "tune"),
                    ))

        if critic_output.action == CriticAction.ALERT:
            # Stage changes for human review
            for c in changes:
                c.permission_level = PermissionLevel.HUMAN_REVIEW

        # Classify each change using PolicyChangeClassifier
        for c in changes:
            c.permission_level = PolicyChangeClassifier.classify(c, policy)

        # Stage the new policy
        if changes:
            new_policy = dict(policy)  # shallow copy
            for c in changes:
                if c.section in new_policy:
                    if isinstance(new_policy[c.section], dict):
                        new_policy[c.section] = dict(new_policy[c.section])
                        try:
                            new_policy[c.section][c.key] = type(policy[c.section][c.key])(c.new_value)
                        except (KeyError, ValueError):
                            new_policy[c.section][c.key] = c.new_value
            self._staged_policy = new_policy

        return changes

    def _identity_check(self, policy_changes: list[PolicyChange], perceive_data: dict) -> str:
        """Step 5: Validate that proposed changes maintain coherence."""
        if not policy_changes:
            return "no_changes"

        # Check drift score
        l3_signals = perceive_data.get("l3_signals", [])
        drift_signals = [s for s in l3_signals if s.get("source") == "L3_drift_score"]

        if drift_signals:
            drift_score = drift_signals[0].get("payload", {}).get("drift_score", 0)
            # Channel E: drift threshold 0.3 — triggers restart from PERCEIVE
            policy = self.state.read_policy()
            drift_threshold = policy.get("meta_loop", {}).get("drift_threshold", 0.3)
            if drift_score > drift_threshold:
                self._staged_policy = None  # Discard changes
                return "rejected_high_drift"

        # Check for NEVER-category changes
        for c in policy_changes:
            if c.permission_level == PermissionLevel.NEVER:
                self._staged_policy = None
                return "rejected_never_permission"

        return "approved"

    def _shadow_test(self, policy_changes: list[PolicyChange], perceive_data: dict) -> dict:
        """Channel C: Compare proposed policy against last 5 task outcomes."""
        db = get_db()
        recent_tasks = db.execute(
            """SELECT event_type, data FROM meta_events
               WHERE bot_id = ? AND event_type IN ('task_complete', 'task_failed')
               ORDER BY created_at DESC LIMIT 5""",
            (self.bot_id,)
        ).fetchall()

        if not recent_tasks:
            return {"passed": True, "reason": "no_history", "sample_size": 0}

        # Simple shadow test: if current success rate > 50% and changes don't increase limits excessively, pass
        successes = sum(1 for t in recent_tasks if t["event_type"] == "task_complete")
        total = len(recent_tasks)
        current_success_rate = successes / total if total > 0 else 0.5

        # Check if any change loosens constraints significantly
        loosening = any(
            float(c.new_value) > float(c.old_value) * 1.5
            for c in policy_changes
            if c.old_value and c.old_value.replace('.', '').isdigit()
        )

        if loosening and current_success_rate < 0.6:
            return {"passed": False, "reason": "loosening_with_low_success",
                    "success_rate": current_success_rate, "sample_size": total}

        return {"passed": True, "reason": "acceptable",
                "success_rate": current_success_rate, "sample_size": total}

    def _queue_alert(self, critic_output: CriticOutput) -> None:
        """Queue an alert for human review."""
        self.state.push_signal({
            "source": "engine_alert",
            "priority": "urgent",
            "payload": {"action": critic_output.action.value, "reason": critic_output.reason},
        })

    def _persist(self, policy_changes: list[PolicyChange], identity_verdict: str) -> None:
        """Step 6: Atomically commit staged changes."""
        if identity_verdict != "approved" or not self._staged_policy:
            logger.info(f"Skipping persist: verdict={identity_verdict}")
            return

        # Enforce: NEVER changes are rejected and logged as errors
        never_changes = [c for c in policy_changes if c.permission_level == PermissionLevel.NEVER]
        if never_changes:
            for nc in never_changes:
                logger.error(f"NEVER-category change rejected: {nc.section}.{nc.key} "
                             f"({nc.old_value} -> {nc.new_value}), reason: {nc.reason}")
                nc.status = "rejected"
            policy_changes = [c for c in policy_changes if c.permission_level != PermissionLevel.NEVER]

        # Write policy atomically
        auto_approved = [c for c in policy_changes if c.permission_level == PermissionLevel.AUTO_APPROVE]
        human_review = [c for c in policy_changes if c.permission_level == PermissionLevel.HUMAN_REVIEW]

        if auto_approved and self._staged_policy:
            self.state.write_snapshot("policy.toml", self._staged_policy)
            for c in auto_approved:
                c.status = "applied"

        # Queue human-review changes
        for c in human_review:
            c.status = "pending_review"
            self._queue_for_review(c)

        # Nirmana: log GREEN/RED decisions for briefing
        if self._nirmana_mode:
            now_iso = datetime.now(timezone.utc).isoformat()
            for c in auto_approved:
                self._nirmana_briefing.append({
                    "type": "GREEN",
                    "change": f"{c.section}.{c.key}: {c.old_value} -> {c.new_value}",
                    "time": now_iso,
                })
            for c in human_review:
                self._nirmana_briefing.append({
                    "type": "RED",
                    "change": f"{c.section}.{c.key}: {c.old_value} -> {c.new_value}",
                    "time": now_iso,
                })

        # Write self-model
        if self._staged_self_model:
            self.state.write_snapshot("self-model.toml", self._staged_self_model)

        # Append to evolution log
        self.state.append_evolution({
            "cycle_num": self._cycle_log.get("cycle_num"),
            "trigger": self._cycle_log.get("trigger"),
            "action": self._cycle_log.get("critique_output", {}).get("action", "none"),
            "changes_applied": len(auto_approved),
            "changes_pending_review": len(human_review),
            "identity_verdict": identity_verdict,
        })

        # Channel G: Persist → Perceive (write last commit summary + in-memory confirmation)
        commit_summary = {
            "cycle_num": self._cycle_log.get("cycle_num"),
            "changes_applied": len(auto_approved),
            "changes_pending_review": len(human_review),
            "identity_verdict": identity_verdict,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        self.state.write_last_commit(commit_summary)
        self._last_persist_confirmation = commit_summary

        # Channel F: Schedule delayed evaluation for policy changes
        if auto_approved:
            self._schedule_delayed_eval({
                "cycle_num": self._cycle_log.get("cycle_num"),
                "changes": [
                    {"section": c.section, "key": c.key,
                     "old_value": c.old_value, "new_value": c.new_value}
                    for c in auto_approved
                ],
            })

    def _plan_tasks(self, perceive_data: dict) -> list[dict]:
        """Step 7: Generate improvement tasks if goals are below threshold and in aggressive mode."""
        cadence = self.state.read_cadence()
        mode = cadence.get("mode", "balanced")

        # Only auto-plan in aggressive mode (Nirmana active via /away)
        if mode != "aggressive":
            logger.debug("PLAN step skipped: not in aggressive mode")
            return []

        scores = perceive_data.get("l2_scores", {})
        below = scores.get("below_threshold", [])

        if not below:
            logger.debug("PLAN step skipped: all goals above threshold")
            return []

        planner = TaskPlanner()
        tasks = planner.generate_tasks(scores, below)

        # Split into GREEN and YELLOW
        from aros_meta_loop.services.task_planner import AuthorityLevel
        green_tasks = [t for t in tasks if t.authority_level == AuthorityLevel.GREEN]
        yellow_tasks = [t for t in tasks if t.authority_level == AuthorityLevel.YELLOW]

        # Convert to serializable dicts for logging
        task_dicts = [
            {
                "title": t.title,
                "description": t.description,
                "target_project": t.target_project,
                "authority_level": t.authority_level.value,
                "estimated_minutes": t.estimated_minutes,
                "goal_source": t.goal_source,
            }
            for t in tasks
        ]

        logger.info(
            f"PLAN step: generated {len(task_dicts)} tasks "
            f"({len(green_tasks)} GREEN, {len(yellow_tasks)} YELLOW)"
        )

        # Queue YELLOW tasks for human approval
        cycle_num = self._cycle_log.get("cycle_num")
        n_yellow = 0
        for t in yellow_tasks:
            task_dict = {
                "title": t.title,
                "description": t.description,
                "target_project": t.target_project,
                "authority_level": t.authority_level.value,
                "estimated_minutes": t.estimated_minutes,
                "goal_source": t.goal_source,
            }
            self.state.add_pending_approval({
                "task": task_dict,
                "cycle_num": cycle_num,
            })
            n_yellow += 1
            logger.info(f"YELLOW task queued for approval: {t.title}")

        n_green = len(green_tasks)
        n_skipped = len(tasks) - n_green - n_yellow

        # Log task generation to evolution log
        self.state.log_task_generation(
            cycle_num=cycle_num or 0,
            tasks=task_dicts,
            trigger_results={
                "green_dispatched": n_green,
                "yellow_queued": n_yellow,
                "skipped": n_skipped,
            },
        )

        return task_dicts

    def _check_cadence_limits(self, trigger: str) -> str | None:
        """Check if cadence limits allow a new cycle. Returns rejection reason or None."""
        now = datetime.now(timezone.utc)
        cadence = self.state.read_cadence()

        # Reset hourly counter
        if (now - self._hour_reset_time).total_seconds() > 3600:
            self._cycle_count_hour = 0
            self._emergency_count_hour = 0
            self._hour_reset_time = now

        # Reset daily counter
        if (now - self._day_reset_time).total_seconds() > 86400:
            self._cycle_count_day = 0
            self._day_reset_time = now

        # Check limits
        if self._cycle_count_hour >= cadence.get("max_cycles_per_hour", 4):
            return "max_cycles_per_hour exceeded"
        if self._cycle_count_day >= cadence.get("max_cycles_per_day", 20):
            return "max_cycles_per_day exceeded"

        # Min interval
        if self._last_cycle_time:
            min_interval = cadence.get("min_interval_between_cycles_seconds", 180)
            elapsed = (now - self._last_cycle_time).total_seconds()
            if elapsed < min_interval:
                return f"min_interval not met ({elapsed:.0f}s < {min_interval}s)"

        # Emergency limit
        if trigger == "emergency":
            if self._emergency_count_hour >= cadence.get("max_emergency_per_hour", 2):
                return "max_emergency_per_hour exceeded"

        return None

    def _schedule_delayed_eval(self, policy_change_info: dict) -> None:
        """Channel F: Schedule a delayed evaluation to measure policy change impact."""
        evals_path = self.state.state_dir / "state" / "pending_evals.json"
        evals_path.parent.mkdir(parents=True, exist_ok=True)

        pending = []
        if evals_path.exists():
            try:
                pending = json.loads(evals_path.read_text())
            except Exception:
                pending = []

        cadence = self.state.read_cadence()
        eval_entry = {
            "eval_id": uuid.uuid4().hex[:8],
            "created_at": datetime.now(timezone.utc).isoformat(),
            "policy_change": policy_change_info,
            "sessions_required": cadence.get("delayed_eval_sessions", 5),
            "sessions_at_creation": self._count_session_starts(),
            "status": "pending",
        }
        pending.append(eval_entry)
        evals_path.write_text(json.dumps(pending, indent=2))
        logger.info(f"Scheduled delayed evaluation: {eval_entry['eval_id']}")

    def _delayed_evaluation(self) -> dict:
        """Channel F: Evaluate recent policy changes by comparing pre/post metrics.

        Reads the last N meta_iterations (configurable via policy.toml shadow_test_window,
        default 5) and compares metrics before and after policy changes were applied.

        Returns:
            dict with keys: verdict (CONFIRMED/DEGRADED/INCONCLUSIVE),
            iterations_examined, details.
        """
        policy = self.state.read_policy()
        window = policy.get("meta_loop", {}).get("shadow_test_window", 5)

        try:
            db = get_db()
            rows = db.execute(
                """SELECT cycle_num, perceive_data, policy_changes, status
                   FROM meta_iterations
                   WHERE bot_id = ?
                   ORDER BY id DESC LIMIT ?""",
                (self.bot_id, window),
            ).fetchall()
        except Exception:
            rows = []

        if not rows or len(rows) < 2:
            result = {"verdict": "INCONCLUSIVE", "reason": "insufficient_data",
                      "iterations_examined": len(rows)}
            # Store as policy_delta event
            self.emitter.emit_event(self.bot_id, "policy_delta", "meta_loop", result)
            return result

        # Extract tokens_per_task from perceive_data across the window
        metrics = []
        for row in rows:
            try:
                pdata = json.loads(row["perceive_data"]) if row["perceive_data"] else {}
                l1 = pdata.get("l1_metrics", {})
                tpt = l1.get("tokens_per_task")
                if tpt is not None:
                    metrics.append(tpt)
            except (json.JSONDecodeError, TypeError):
                continue

        if len(metrics) < 2:
            result = {"verdict": "INCONCLUSIVE", "reason": "insufficient_metrics",
                      "iterations_examined": len(rows)}
            self.emitter.emit_event(self.bot_id, "policy_delta", "meta_loop", result)
            return result

        # Compare: first half (older) vs second half (newer)
        mid = len(metrics) // 2
        older_avg = sum(metrics[mid:]) / len(metrics[mid:])   # rows are DESC, so later indices are older
        newer_avg = sum(metrics[:mid]) / mid if mid > 0 else older_avg

        if older_avg == 0:
            verdict = "INCONCLUSIVE"
            reason = "zero_baseline"
        else:
            delta = (newer_avg - older_avg) / older_avg
            if delta < -0.05:
                verdict = "CONFIRMED"   # tokens decreased = improvement
                reason = f"tokens_per_task improved by {abs(delta)*100:.1f}%"
            elif delta > 0.1:
                verdict = "DEGRADED"
                reason = f"tokens_per_task worsened by {delta*100:.1f}%"
            else:
                verdict = "INCONCLUSIVE"
                reason = f"delta {delta*100:.1f}% within noise margin"

        result = {
            "verdict": verdict,
            "reason": reason,
            "iterations_examined": len(rows),
            "older_avg": older_avg,
            "newer_avg": newer_avg,
        }

        # Store as policy_delta event in meta_events
        self.emitter.emit_event(self.bot_id, "policy_delta", "meta_loop", result)

        return result

    def check_delayed_evaluations(self) -> list[dict]:
        """Channel F: Check pending delayed evaluations and produce reports."""
        evals_path = self.state.state_dir / "state" / "pending_evals.json"
        if not evals_path.exists():
            return []

        try:
            pending = json.loads(evals_path.read_text())
        except Exception:
            return []

        current_sessions = self._count_session_starts()
        reports = []
        updated = []

        for ev in pending:
            if ev.get("status") != "pending":
                updated.append(ev)
                continue

            sessions_since = current_sessions - ev.get("sessions_at_creation", 0)
            required = ev.get("sessions_required", 5)

            if sessions_since >= required:
                # Collect L1 metrics for the evaluation window
                try:
                    l1_current = self.collector.collect(self.bot_id)
                    # Compare against baseline (metrics at creation time)
                    baseline_tokens = ev.get("policy_change", {}).get("baseline_tokens_per_task")
                    current_tokens = l1_current.get("tokens_per_task", 0)

                    if baseline_tokens and current_tokens:
                        delta = (current_tokens - baseline_tokens) / baseline_tokens
                        if delta < -0.05:
                            verdict = "CONFIRMED"
                        elif delta > 0.1:
                            verdict = "DEGRADED"
                        else:
                            verdict = "INCONCLUSIVE"
                    else:
                        verdict = "INCONCLUSIVE"
                except Exception:
                    verdict = "INCONCLUSIVE"

                report = {
                    "eval_id": ev["eval_id"],
                    "policy_change": ev["policy_change"],
                    "sessions_elapsed": sessions_since,
                    "verdict": verdict,
                    "completed_at": datetime.now(timezone.utc).isoformat(),
                }
                reports.append(report)

                # Emit as signal
                self.state.push_signal({
                    "source": "policy_delta",
                    "priority": "normal",
                    "payload": report,
                })

                ev["status"] = "completed"
                ev["verdict"] = verdict

            updated.append(ev)

        evals_path.write_text(json.dumps(updated, indent=2))
        return reports

    def _count_session_starts(self) -> int:
        """Count total session_start events for this bot."""
        try:
            db = get_db()
            row = db.execute(
                "SELECT COUNT(*) as cnt FROM meta_events WHERE bot_id = ? AND event_type = 'session_start'",
                (self.bot_id,)
            ).fetchone()
            return row["cnt"] if row else 0
        except Exception:
            return 0

    def _phase_gate(self, step_name: str) -> bool:
        """Check abort flag and urgent signals between steps. Returns True if should abort."""
        if self._abort_flag:
            logger.warning(f"Abort at phase gate after {step_name}: {self._abort_reason}")
            self._staged_policy = None
            self._staged_self_model = None
            return True

        if self.state.has_urgent():
            logger.info(f"Urgent signal detected at phase gate after {step_name}")
            # Don't abort, but log — urgent signals are processed in next PERCEIVE

        return False

    def _queue_for_review(self, change: PolicyChange) -> None:
        """Queue a policy change for human review."""
        review_dir = self.state.state_dir / "pending-review"
        review_dir.mkdir(exist_ok=True)
        path = review_dir / f"{change.change_id}.json"
        path.write_text(json.dumps({
            "change_id": change.change_id,
            "section": change.section,
            "key": change.key,
            "old_value": change.old_value,
            "new_value": change.new_value,
            "reason": change.reason,
            "status": "pending_review",
            "created_at": datetime.now(timezone.utc).isoformat(),
        }))

    def _get_next_cycle_num(self) -> int:
        """Get next cycle number from DB."""
        try:
            db = get_db()
            row = db.execute(
                "SELECT MAX(cycle_num) as max_num FROM meta_iterations WHERE bot_id = ?",
                (self.bot_id,)
            ).fetchone()
            return (row["max_num"] or 0) + 1
        except Exception:
            return 1

    def _finalize_cycle(self, status: str) -> dict:
        """Record cycle in meta_iterations and return result."""
        finished_at = datetime.now(timezone.utc).isoformat()
        self._cycle_log["status"] = status
        self._cycle_log["finished_at"] = finished_at

        try:
            with db_write_lock():
                db = get_db()
                db.execute(
                    """INSERT INTO meta_iterations
                       (bot_id, cycle_num, trigger, started_at, finished_at,
                        steps_completed, perceive_data, critique_output,
                        policy_changes, identity_verdict, status)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        self.bot_id,
                        self._cycle_log.get("cycle_num", 0),
                        self._cycle_log.get("trigger", "unknown"),
                        self._cycle_log.get("started_at", finished_at),
                        finished_at,
                        self._cycle_log.get("steps_completed", 0),
                        json.dumps(self._cycle_log.get("perceive_data")),
                        json.dumps(self._cycle_log.get("critique_output")),
                        json.dumps(self._cycle_log.get("policy_changes")),
                        self._cycle_log.get("identity_verdict"),
                        status,
                    ),
                )
                db.commit()
        except Exception as e:
            logger.error(f"Failed to record cycle: {e}")

        logger.info(f"Cycle {self._cycle_log.get('cycle_num')} finished: {status}")
        return self._cycle_log

    async def activate_nirmana(self) -> dict:
        """Switch to aggressive cadence for Nirmana autonomous mode."""
        self._nirmana_mode = True
        self._nirmana_briefing = []

        # Switch cadence to aggressive
        cadence = self.state.read_cadence()
        cadence["mode"] = "aggressive"

        import tomllib
        config_path = self.state.state_dir / "meta-cognition.toml"
        if config_path.exists():
            with open(config_path, "rb") as f:
                full_config = tomllib.load(f)
        else:
            full_config = {}
        full_config["cadence"] = cadence
        self.state.write_snapshot("meta-cognition.toml", full_config)

        # Update scheduler
        from aros_meta_loop.services.scheduler import update_schedule
        update_schedule("aggressive")

        logger.info("Nirmana mode activated — aggressive cadence")
        return {"status": "activated", "mode": "aggressive"}

    async def deactivate_nirmana(self) -> dict:
        """Restore balanced cadence and generate briefing."""
        self._nirmana_mode = False

        # Switch cadence back to balanced
        cadence = self.state.read_cadence()
        cadence["mode"] = "balanced"

        import tomllib
        config_path = self.state.state_dir / "meta-cognition.toml"
        if config_path.exists():
            with open(config_path, "rb") as f:
                full_config = tomllib.load(f)
        else:
            full_config = {}
        full_config["cadence"] = cadence
        self.state.write_snapshot("meta-cognition.toml", full_config)

        from aros_meta_loop.services.scheduler import update_schedule
        update_schedule("balanced")

        # Generate briefing for Eddie's return
        briefing = self._generate_briefing()

        logger.info("Nirmana mode deactivated — balanced cadence restored")
        return {"status": "deactivated", "mode": "balanced", "briefing": briefing}

    def _generate_briefing(self) -> dict:
        """Generate activity summary for Eddie's return."""
        db = get_db()
        rows = db.execute(
            "SELECT * FROM meta_iterations WHERE bot_id = ? ORDER BY id DESC LIMIT 50",
            (self.bot_id,)
        ).fetchall()

        cycles_run = len(rows)
        completed = sum(1 for r in rows if r["status"] == "completed")

        # Count pending reviews
        pending_dir = self.state.state_dir / "pending-review"
        pending_count = len(list(pending_dir.glob("*.json"))) if pending_dir.exists() else 0

        # Get recent evolution log
        recent_log = self.state.read_evolution_log(limit=10)

        return {
            "cycles_run": cycles_run,
            "cycles_completed": completed,
            "decisions_made": len(self._nirmana_briefing),
            "pending_reviews": pending_count,
            "recent_activity": recent_log[-5:] if recent_log else [],
            "summary": f"Ran {cycles_run} cycles ({completed} completed), {pending_count} items awaiting review.",
        }

    def get_status(self) -> dict:
        """Get current engine status."""
        last_cycle = None
        try:
            db = get_db()
            row = db.execute(
                """SELECT * FROM meta_iterations WHERE bot_id = ?
                   ORDER BY id DESC LIMIT 1""",
                (self.bot_id,)
            ).fetchone()
            if row:
                last_cycle = {
                    "cycle_num": row["cycle_num"],
                    "trigger": row["trigger"],
                    "started_at": row["started_at"],
                    "finished_at": row["finished_at"],
                    "steps_completed": row["steps_completed"],
                    "status": row["status"],
                    "identity_verdict": row["identity_verdict"],
                }
        except Exception:
            pass

        # Get pending reviews
        pending = []
        review_dir = self.state.state_dir / "pending-review"
        if review_dir.exists():
            for f in review_dir.glob("*.json"):
                try:
                    pending.append(json.loads(f.read_text()))
                except Exception:
                    continue

        cadence = self.state.read_cadence()

        return {
            "running": self._running,
            "bot_id": self.bot_id,
            "last_cycle": last_cycle,
            "cadence_mode": cadence.get("mode", "balanced"),
            "pending_approvals": len(pending),
        }
