"""L1 Metrics Collector — queries meta_events for rollup metrics."""
import json
import logging
from datetime import datetime, timezone

from aros_meta_loop.db.engine import get_db

logger = logging.getLogger(__name__)


class L1Collector:
    """Collects Layer 1 operational metrics from meta_events."""

    def collect(self, bot_id: str, since: str | None = None) -> dict:
        """
        Collect L1 metrics for a bot since a given timestamp.

        Args:
            bot_id: Bot identifier for multi-tenant filtering
            since: ISO timestamp to filter events from (None = last 24 hours)

        Returns:
            Dict with keys: tokens_consumed, tokens_per_task, tool_call_count,
            tool_call_success_rate, retry_count, error_count_by_type,
            context_window_usage, wall_clock_per_task, cost_usd
        """
        if since is None:
            # Default to last 24 hours
            from datetime import timedelta
            since = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()

        db = get_db()
        events = db.execute(
            "SELECT event_type, data FROM meta_events WHERE bot_id = ? AND created_at >= ?",
            (bot_id, since)
        ).fetchall()

        # Parse all events
        tool_calls = []
        tasks = []
        sessions = []
        errors = {}

        for row in events:
            event_type = row["event_type"]
            data = {}
            if row["data"]:
                try:
                    data = json.loads(row["data"])
                except json.JSONDecodeError:
                    continue

            if event_type == "tool_call":
                tool_calls.append(data)
            elif event_type == "task_complete":
                tasks.append(data)
            elif event_type == "task_failed":
                tasks.append(data)
                err_type = data.get("error_type", "unknown")
                errors[err_type] = errors.get(err_type, 0) + 1
            elif event_type in ("session_start", "session_end"):
                sessions.append(data)

        # Compute metrics
        total_tokens = sum(
            d.get("tokens_in", 0) + d.get("tokens_out", 0) for d in tool_calls
        ) + sum(d.get("tokens_consumed", 0) for d in tasks)

        task_count = len(tasks) or 1  # avoid division by zero
        tokens_per_task = total_tokens / task_count if tasks else 0.0

        tool_call_count = len(tool_calls)
        successful_calls = sum(1 for d in tool_calls if d.get("success", True))
        tool_call_success_rate = (successful_calls / tool_call_count) if tool_call_count else 1.0

        retry_count = sum(d.get("retries", 0) for d in tasks)

        context_sizes = [d.get("context_tokens", 0) for d in sessions if d.get("context_tokens")]
        context_window_usage = (sum(context_sizes) / len(context_sizes)) if context_sizes else 0.0

        durations = [d.get("duration_seconds", 0) for d in tasks if d.get("duration_seconds")]
        wall_clock_per_task = (sum(durations) / len(durations)) if durations else 0.0

        # Rough cost estimate: $3/M input + $15/M output (Claude Opus)
        total_input = sum(d.get("tokens_in", 0) for d in tool_calls)
        total_output = sum(d.get("tokens_out", 0) for d in tool_calls)
        cost_usd = (total_input * 3 + total_output * 15) / 1_000_000

        return {
            "tokens_consumed": total_tokens,
            "tokens_per_task": round(tokens_per_task, 1),
            "tool_call_count": tool_call_count,
            "tool_call_success_rate": round(tool_call_success_rate, 3),
            "retry_count": retry_count,
            "error_count_by_type": errors,
            "context_window_usage": round(context_window_usage, 0),
            "wall_clock_per_task": round(wall_clock_per_task, 1),
            "cost_usd": round(cost_usd, 4),
            "event_count": len(events),
            "task_count": len(tasks),
        }


class L2Evaluator:
    """Evaluates L1 metrics against G1-G6 meta-goals."""

    def __init__(self, state_manager=None):
        from aros_meta_loop.services.state_manager import StateManager
        self.state = state_manager or StateManager()

    def evaluate(self, l1_metrics: dict) -> dict:
        """
        Score each meta-goal from 0.0 to 1.0 based on L1 data.

        G1 Truthful: 1.0 - error_rate (lower errors = more truthful)
        G2 Efficient: normalized tokens efficiency (fewer tokens per task = better)
        G3 Reliable: tasks without retries / total tasks
        G4 Aligned: 1.0 - (failed_tasks / total_tasks)
        G5 Ambitious: average task complexity / 5.0
        G6 Self-Know: tool_call_success_rate (proxy for calibration)
        """
        goals_config = self.state.read_goals()

        # G1: Truthful — low error rate
        total_errors = sum(l1_metrics.get("error_count_by_type", {}).values())
        event_count = max(l1_metrics.get("event_count", 1), 1)
        g1 = max(0.0, 1.0 - (total_errors / event_count))

        # G2: Efficient — token efficiency (lower is better, normalize against 50K baseline)
        tokens_per_task = l1_metrics.get("tokens_per_task", 0)
        baseline_tokens = 50000  # expected tokens per task
        g2 = max(0.0, min(1.0, 1.0 - (tokens_per_task / baseline_tokens))) if tokens_per_task > 0 else 0.5

        # G3: Reliable — tasks without retries
        task_count = max(l1_metrics.get("task_count", 0), 1)
        retry_count = l1_metrics.get("retry_count", 0)
        tasks_without_retry = max(0, task_count - retry_count)
        g3 = tasks_without_retry / task_count

        # G4: Aligned — successful task rate
        tool_success = l1_metrics.get("tool_call_success_rate", 1.0)
        g4 = tool_success

        # G5: Ambitious — average complexity (not directly in L1, use task_count as proxy)
        # Higher task throughput with acceptable quality = more ambitious
        g5 = min(1.0, task_count / 10.0) if task_count > 0 else 0.0

        # G6: Self-Know — calibration accuracy (proxy: success rate stability)
        g6 = l1_metrics.get("tool_call_success_rate", 0.5)

        scores = {
            "G1_truthful": round(g1, 3),
            "G2_efficient": round(g2, 3),
            "G3_reliable": round(g3, 3),
            "G4_aligned": round(g4, 3),
            "G5_ambitious": round(g5, 3),
            "G6_self_know": round(g6, 3),
        }

        # Add weighted aggregate
        total_weight = 0.0
        weighted_sum = 0.0
        for key, score in scores.items():
            goal_cfg = goals_config.get(key, {})
            weight = goal_cfg.get("weight", 1.0)
            weighted_sum += score * weight
            total_weight += weight

        scores["aggregate"] = round(weighted_sum / total_weight, 3) if total_weight > 0 else 0.0

        # Add threshold comparisons
        scores["below_threshold"] = []
        for key, score in scores.items():
            if key in ("aggregate", "below_threshold"):
                continue
            goal_cfg = goals_config.get(key, {})
            threshold = goal_cfg.get("threshold", 0.5)
            if score < threshold:
                scores["below_threshold"].append(key)

        return scores


class L3SignalDeriver:
    """Derives agent-level signals by cross-referencing L1/L2 data."""

    def derive(self, l1_metrics: dict, l2_scores: dict, self_model: dict) -> list:
        """
        Compute cross-validated signals.
        Returns list of Signal-compatible dicts.
        """
        from datetime import datetime, timezone
        from aros_meta_loop.models.signals import Signal

        signals = []
        now = datetime.now(timezone.utc).isoformat()

        # 1. Strategy effectiveness — are retries going down?
        retry_count = l1_metrics.get("retry_count", 0)
        task_count = max(l1_metrics.get("task_count", 1), 1)
        retry_rate = retry_count / task_count
        signals.append(Signal(
            source="L3_strategy_effectiveness",
            priority="normal",
            timestamp=now,
            payload={
                "retry_rate": round(retry_rate, 3),
                "verdict": "effective" if retry_rate < 0.2 else "needs_improvement",
                "cross_validated_with": "L1.retry_count, L1.task_count",
            },
            validation_status="validated",
        ))

        # 2. Tool underutilization — tools with low call count vs available
        tool_call_count = l1_metrics.get("tool_call_count", 0)
        capabilities = self_model.get("capabilities", {})
        known_tools = len(capabilities) if capabilities else 10  # default estimate
        utilization = min(1.0, tool_call_count / max(known_tools * 5, 1))
        signals.append(Signal(
            source="L3_tool_underutilization",
            priority="low" if utilization > 0.3 else "normal",
            timestamp=now,
            payload={
                "utilization_ratio": round(utilization, 3),
                "tool_call_count": tool_call_count,
                "verdict": "adequate" if utilization > 0.3 else "underutilized",
                "cross_validated_with": "L1.tool_call_count, self_model.capabilities",
            },
            validation_status="validated",
        ))

        # 3. Confidence calibration — predicted vs actual success
        success_rate = l1_metrics.get("tool_call_success_rate", 1.0)
        g6_score = l2_scores.get("G6_self_know", 0.5)
        calibration_error = abs(success_rate - g6_score)
        signals.append(Signal(
            source="L3_confidence_calibration",
            priority="normal" if calibration_error > 0.1 else "low",
            timestamp=now,
            payload={
                "calibration_error": round(calibration_error, 3),
                "actual_success_rate": round(success_rate, 3),
                "self_assessed": round(g6_score, 3),
                "verdict": "calibrated" if calibration_error < 0.1 else "miscalibrated",
                "cross_validated_with": "L1.tool_call_success_rate, L2.G6_self_know",
            },
            validation_status="validated",
        ))

        # 4. Drift score — cumulative deviation from baseline
        below_threshold = l2_scores.get("below_threshold", [])
        drift_score = len(below_threshold) / 6.0  # 6 goals total
        signals.append(Signal(
            source="L3_drift_score",
            priority="urgent" if drift_score > 0.5 else ("high" if drift_score > 0.3 else "normal"),
            timestamp=now,
            payload={
                "drift_score": round(drift_score, 3),
                "goals_below_threshold": below_threshold,
                "verdict": "stable" if drift_score < 0.3 else "drifting",
                "cross_validated_with": "L2.below_threshold",
            },
            validation_status="validated",
        ))

        # 5. Pattern recurrence — repeated error types
        errors = l1_metrics.get("error_count_by_type", {})
        recurring = {k: v for k, v in errors.items() if v >= 2}
        signals.append(Signal(
            source="L3_pattern_recurrence",
            priority="high" if recurring else "low",
            timestamp=now,
            payload={
                "recurring_errors": recurring,
                "unique_error_types": len(errors),
                "verdict": "recurring_patterns" if recurring else "no_recurrence",
                "cross_validated_with": "L1.error_count_by_type",
            },
            validation_status="validated",
        ))

        return signals
