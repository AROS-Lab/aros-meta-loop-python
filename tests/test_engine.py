"""Tests for MetaLoopEngine."""
import asyncio
import json
import sqlite3
import pytest
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import patch, MagicMock

from aros_meta_loop.db.migrations import run_migrations
from aros_meta_loop.services.engine import MetaLoopEngine, PolicyChangeClassifier, CadenceController
from aros_meta_loop.services.state_manager import StateManager
from aros_meta_loop.models.signals import CriticAction, PolicyChange, PermissionLevel


@pytest.fixture
def engine_env(tmp_path):
    """Set up a complete engine test environment."""
    # DB
    db_path = tmp_path / "test.db"
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    run_migrations(conn)

    # Seed some events
    now = datetime.now(timezone.utc)
    events = [
        ("bot1", "tool_call", "s1", json.dumps({"tool": "grep", "success": True, "tokens_in": 1000, "tokens_out": 500}), now.isoformat()),
        ("bot1", "task_complete", "s1", json.dumps({"task_id": "t1", "tokens_consumed": 3000, "duration_seconds": 60, "retries": 0, "complexity": 3}), now.isoformat()),
    ]
    for bot_id, event_type, session_id, data, created_at in events:
        conn.execute("INSERT INTO meta_events (bot_id, event_type, session_id, data, created_at) VALUES (?, ?, ?, ?, ?)",
                     (bot_id, event_type, session_id, data, created_at))
    conn.commit()

    # State dir
    from aros_meta_loop.config import META_COGNITION_DEFAULT, SELF_MODEL_DEFAULT, POLICY_DEFAULT, _write_default
    state_dir = tmp_path / ".aros"
    state_dir.mkdir()
    for sub in ("data", "signals", "pending-review", "state"):
        (state_dir / sub).mkdir()
    _write_default(state_dir / "meta-cognition.toml", META_COGNITION_DEFAULT)
    _write_default(state_dir / "self-model.toml", SELF_MODEL_DEFAULT)
    _write_default(state_dir / "policy.toml", POLICY_DEFAULT)

    state_mgr = StateManager(state_dir=state_dir)

    return {"conn": conn, "state_dir": state_dir, "state_mgr": state_mgr, "db_path": db_path}


@pytest.fixture
def engine(engine_env):
    """Create a MetaLoopEngine with test environment."""
    with patch("aros_meta_loop.services.metrics.get_db", return_value=engine_env["conn"]), \
         patch("aros_meta_loop.services.engine.get_db", return_value=engine_env["conn"]), \
         patch("aros_meta_loop.services.event_emitter.get_db", return_value=engine_env["conn"]):
        eng = MetaLoopEngine(state_manager=engine_env["state_mgr"], bot_id="bot1")
        yield eng


class TestMetaLoopEngine:
    def test_run_cycle_happy_path(self, engine, engine_env):
        with patch("aros_meta_loop.services.metrics.get_db", return_value=engine_env["conn"]), \
             patch("aros_meta_loop.services.engine.get_db", return_value=engine_env["conn"]):
            result = asyncio.get_event_loop().run_until_complete(engine.run_cycle("test"))
            assert result["status"] in ("completed", "aborted")
            assert result["steps_completed"] >= 1

    def test_run_cycle_records_to_db(self, engine, engine_env):
        with patch("aros_meta_loop.services.metrics.get_db", return_value=engine_env["conn"]), \
             patch("aros_meta_loop.services.engine.get_db", return_value=engine_env["conn"]):
            asyncio.get_event_loop().run_until_complete(engine.run_cycle("test"))
            row = engine_env["conn"].execute("SELECT * FROM meta_iterations WHERE bot_id='bot1'").fetchone()
            assert row is not None
            assert row["trigger"] == "test"

    def test_abort_discards_changes(self, engine, engine_env):
        with patch("aros_meta_loop.services.metrics.get_db", return_value=engine_env["conn"]), \
             patch("aros_meta_loop.services.engine.get_db", return_value=engine_env["conn"]):
            # Set abort before running
            engine.abort("test_abort")
            result = asyncio.get_event_loop().run_until_complete(engine.run_cycle("test"))
            assert result["status"] == "aborted"

    def test_concurrent_cycle_rejected(self, engine, engine_env):
        with patch("aros_meta_loop.services.metrics.get_db", return_value=engine_env["conn"]), \
             patch("aros_meta_loop.services.engine.get_db", return_value=engine_env["conn"]):
            async def run_two():
                # Acquire lock manually
                await engine._lock.acquire()
                try:
                    result = await engine.run_cycle("test")
                    return result
                finally:
                    engine._lock.release()

            result = asyncio.get_event_loop().run_until_complete(run_two())
            assert result["status"] == "skipped"

    def test_get_status(self, engine, engine_env):
        with patch("aros_meta_loop.services.metrics.get_db", return_value=engine_env["conn"]), \
             patch("aros_meta_loop.services.engine.get_db", return_value=engine_env["conn"]):
            status = engine.get_status()
            assert "running" in status
            assert "bot_id" in status
            assert status["bot_id"] == "bot1"

    def test_critique_no_action_when_goals_met(self, engine, engine_env):
        """When all goals are met, critique should return NO_ACTION."""
        perceive_data = {
            "l1_metrics": {"event_count": 10, "error_count_by_type": {},
                          "tokens_per_task": 5000, "task_count": 5,
                          "retry_count": 0, "tool_call_success_rate": 0.95,
                          "tool_call_count": 20},
            "l2_scores": {"G1_truthful": 1.0, "G2_efficient": 0.9,
                         "G3_reliable": 1.0, "G4_aligned": 0.95,
                         "G5_ambitious": 0.5, "G6_self_know": 0.95,
                         "aggregate": 0.9, "below_threshold": []},
            "l3_signals": [],
            "queued_signals": [],
            "current_policy": {"harness": {"max_chain_depth": 100}},
            "current_cadence": {"mode": "balanced"},
        }
        result = engine._critique(perceive_data)
        assert result.action == CriticAction.NO_ACTION

    def test_critique_alert_when_many_goals_below(self, engine, engine_env):
        perceive_data = {
            "l2_scores": {"below_threshold": ["G1_truthful", "G2_efficient", "G3_reliable"]},
        }
        result = engine._critique(perceive_data)
        assert result.action == CriticAction.ALERT
        assert result.permission_level.value == "HUMAN_REVIEW"

    def test_shadow_test_passes_no_history(self, engine, engine_env):
        """Channel C: Shadow test should pass when no task history exists."""
        conn = engine_env["conn"]
        # Clear seeded task events so we get the no_history path
        conn.execute("DELETE FROM meta_events WHERE event_type IN ('task_complete', 'task_failed')")
        conn.commit()

        changes = [PolicyChange(change_id="t1", section="harness", key="retry_limit",
                               old_value="3", new_value="4",
                               permission_level=PermissionLevel.AUTO_APPROVE, reason="tune")]
        with patch("aros_meta_loop.services.engine.get_db", return_value=conn):
            result = engine._shadow_test(changes, {})
            assert result["passed"] is True
            assert result["reason"] == "no_history"
            assert result["sample_size"] == 0

    def test_shadow_test_passes_with_good_history(self, engine, engine_env):
        """Channel C: Shadow test should pass with high success rate."""
        # Seed task_complete events
        conn = engine_env["conn"]
        now = datetime.now(timezone.utc).isoformat()
        for i in range(4):
            conn.execute(
                "INSERT INTO meta_events (bot_id, event_type, session_id, data, created_at) VALUES (?, ?, ?, ?, ?)",
                ("bot1", "task_complete", "s1", json.dumps({"task_id": f"tc{i}"}), now))
        conn.execute(
            "INSERT INTO meta_events (bot_id, event_type, session_id, data, created_at) VALUES (?, ?, ?, ?, ?)",
            ("bot1", "task_failed", "s1", json.dumps({"task_id": "tf1"}), now))
        conn.commit()

        changes = [PolicyChange(change_id="t1", section="harness", key="retry_limit",
                               old_value="3", new_value="4",
                               permission_level=PermissionLevel.AUTO_APPROVE, reason="tune")]
        with patch("aros_meta_loop.services.engine.get_db", return_value=conn):
            result = engine._shadow_test(changes, {})
            assert result["passed"] is True
            assert result["success_rate"] == 0.8

    def test_shadow_test_fails_loosening_low_success(self, engine, engine_env):
        """Channel C: Shadow test should fail when loosening with low success rate."""
        conn = engine_env["conn"]
        now = datetime.now(timezone.utc).isoformat()
        # 1 success, 4 failures => 20% success rate
        conn.execute(
            "INSERT INTO meta_events (bot_id, event_type, session_id, data, created_at) VALUES (?, ?, ?, ?, ?)",
            ("bot1", "task_complete", "s1", json.dumps({"task_id": "tc1"}), now))
        for i in range(4):
            conn.execute(
                "INSERT INTO meta_events (bot_id, event_type, session_id, data, created_at) VALUES (?, ?, ?, ?, ?)",
                ("bot1", "task_failed", "s1", json.dumps({"task_id": f"tf{i}"}), now))
        conn.commit()

        # Loosening: new_value (10) > old_value (3) * 1.5 (4.5)
        changes = [PolicyChange(change_id="t1", section="harness", key="retry_limit",
                               old_value="3", new_value="10",
                               permission_level=PermissionLevel.AUTO_APPROVE, reason="tune")]
        with patch("aros_meta_loop.services.engine.get_db", return_value=conn):
            result = engine._shadow_test(changes, {})
            assert result["passed"] is False
            assert result["reason"] == "loosening_with_low_success"

    def test_channel_a_requery_loop(self, engine, engine_env):
        """Channel A: When event_count is 0 and NO_ACTION, should re-query up to 2 times."""
        call_count = {"perceive": 0, "critique": 0}
        original_perceive = engine._perceive
        original_critique = engine._critique

        def mock_perceive():
            call_count["perceive"] += 1
            data = original_perceive()
            data["l1_metrics"]["event_count"] = 0
            return data

        def mock_critique(perceive_data):
            call_count["critique"] += 1
            return original_critique(perceive_data)

        with patch("aros_meta_loop.services.metrics.get_db", return_value=engine_env["conn"]), \
             patch("aros_meta_loop.services.engine.get_db", return_value=engine_env["conn"]), \
             patch.object(engine, "_perceive", side_effect=mock_perceive), \
             patch.object(engine, "_critique", side_effect=mock_critique):
            result = asyncio.get_event_loop().run_until_complete(engine.run_cycle("test"))
            # Initial call + up to 2 re-queries = max 3 perceive calls
            assert call_count["perceive"] <= 3
            assert result["requery_count"] <= 2

    def test_channel_b_rejection_loop(self, engine, engine_env):
        """Channel B: Identity rejection should loop max 3 times then escalate."""
        changes = [PolicyChange(change_id="t1", section="harness", key="retry_limit",
                               old_value="3", new_value="4",
                               permission_level=PermissionLevel.AUTO_APPROVE, reason="tune")]
        # Simulate high drift — identity_check always rejects
        perceive_data = {
            "l3_signals": [{"source": "L3_drift_score", "payload": {"drift_score": 0.8}}],
        }
        verdict = engine._identity_check(changes, perceive_data)
        assert verdict == "rejected_high_drift"

    def test_queue_alert(self, engine, engine_env):
        """_queue_alert should push an urgent signal."""
        from aros_meta_loop.models.signals import CriticOutput, CriticAction, PermissionLevel
        alert = CriticOutput(
            action=CriticAction.ALERT,
            reason="test escalation",
            permission_level=PermissionLevel.HUMAN_REVIEW,
        )
        engine._queue_alert(alert)
        signals = engine.state.drain_signals()
        assert len(signals) >= 1
        found = any(s.get("source") == "engine_alert" for s in signals)
        assert found

    def test_cadence_limit_hourly(self, engine, engine_env):
        """Should reject when max_cycles_per_hour exceeded."""
        engine._cycle_count_hour = 10
        result = engine._check_cadence_limits("scheduled")
        assert result is not None
        assert "max_cycles_per_hour" in result

    def test_cadence_limit_min_interval(self, engine, engine_env):
        """Should reject when min interval not met."""
        engine._last_cycle_time = datetime.now(timezone.utc)
        result = engine._check_cadence_limits("scheduled")
        assert result is not None
        assert "min_interval" in result

    def test_emergency_limit(self, engine, engine_env):
        """Should reject when max_emergency_per_hour exceeded."""
        engine._emergency_count_hour = 5
        result = engine._check_cadence_limits("emergency")
        assert result is not None
        assert "max_emergency" in result

    def test_delayed_eval_schedule(self, engine, engine_env):
        """Schedule a delayed evaluation."""
        engine._schedule_delayed_eval({"change": "test"})
        evals_path = engine_env["state_dir"] / "state" / "pending_evals.json"
        assert evals_path.exists()

    def test_channel_g_last_commit(self, engine, engine_env):
        """Last commit summary should be readable after persist."""
        engine.state.write_last_commit({"test": True})
        result = engine.state.read_last_commit()
        assert result == {"test": True}


class TestPolicyChangeClassifier:
    def test_never_meta_events(self):
        pc = PolicyChange(change_id="t1", section="meta_events", key="anything",
                         old_value=None, new_value="test",
                         permission_level=PermissionLevel.AUTO_APPROVE, reason="test")
        assert PolicyChangeClassifier.classify(pc, {}) == PermissionLevel.NEVER

    def test_never_disable_critic(self):
        pc = PolicyChange(change_id="t1", section="critic", key="enabled",
                         old_value="true", new_value="false",
                         permission_level=PermissionLevel.AUTO_APPROVE, reason="disable")
        assert PolicyChangeClassifier.classify(pc, {}) == PermissionLevel.NEVER

    def test_never_remove_human_review_required(self):
        pc = PolicyChange(change_id="t1", section="permissions", key="human_review_required",
                         old_value="true", new_value="false",
                         permission_level=PermissionLevel.AUTO_APPROVE, reason="test")
        assert PolicyChangeClassifier.classify(pc, {}) == PermissionLevel.NEVER

    def test_never_adversarial_section(self):
        pc = PolicyChange(change_id="t1", section="adversarial", key="anything",
                         old_value=None, new_value="off",
                         permission_level=PermissionLevel.AUTO_APPROVE, reason="test")
        assert PolicyChangeClassifier.classify(pc, {}) == PermissionLevel.NEVER

    def test_human_review_goals(self):
        pc = PolicyChange(change_id="t1", section="goals", key="G1_weight",
                         old_value="1.0", new_value="0.5",
                         permission_level=PermissionLevel.AUTO_APPROVE, reason="test")
        assert PolicyChangeClassifier.classify(pc, {}) == PermissionLevel.HUMAN_REVIEW

    def test_human_review_meta_goals(self):
        pc = PolicyChange(change_id="t1", section="meta_goals", key="alignment",
                         old_value="strict", new_value="relaxed",
                         permission_level=PermissionLevel.AUTO_APPROVE, reason="test")
        assert PolicyChangeClassifier.classify(pc, {}) == PermissionLevel.HUMAN_REVIEW

    def test_auto_approve_tightening(self):
        """Decreasing a constraint key (retry_limit) = tightening = auto-approve."""
        pc = PolicyChange(change_id="t1", section="harness", key="retry_limit",
                         old_value="5", new_value="4",
                         permission_level=PermissionLevel.AUTO_APPROVE, reason="tune")
        assert PolicyChangeClassifier.classify(pc, {}) == PermissionLevel.AUTO_APPROVE

    def test_auto_approve_constraint_within_20_pct(self):
        """Increasing a constraint key within 20% = auto-approve."""
        pc = PolicyChange(change_id="t1", section="harness", key="retry_limit",
                         old_value="10", new_value="12",  # 20% increase exactly
                         permission_level=PermissionLevel.AUTO_APPROVE, reason="tune")
        assert PolicyChangeClassifier.classify(pc, {}) == PermissionLevel.AUTO_APPROVE

    def test_human_review_constraint_over_20_pct(self):
        """Increasing a constraint key over 20% = human review."""
        pc = PolicyChange(change_id="t1", section="harness", key="retry_limit",
                         old_value="3", new_value="4",  # 33% increase > 20%
                         permission_level=PermissionLevel.AUTO_APPROVE, reason="tune")
        assert PolicyChangeClassifier.classify(pc, {}) == PermissionLevel.HUMAN_REVIEW

    def test_tightening_max_batch_size(self):
        pc = PolicyChange(change_id="t1", section="harness", key="max_batch_size",
                         old_value="5", new_value="3",
                         permission_level=PermissionLevel.AUTO_APPROVE, reason="tighten")
        assert PolicyChangeClassifier.classify(pc, {}) == PermissionLevel.AUTO_APPROVE

    def test_loosening_max_batch_size(self):
        pc = PolicyChange(change_id="t1", section="harness", key="max_batch_size",
                         old_value="5", new_value="10",
                         permission_level=PermissionLevel.AUTO_APPROVE, reason="loosen")
        assert PolicyChangeClassifier.classify(pc, {}) == PermissionLevel.HUMAN_REVIEW

    def test_tightening_threshold_auto_approved(self):
        """Increasing drift_threshold = tightening = auto-approve."""
        pc = PolicyChange(change_id="t1", section="meta_loop", key="drift_threshold",
                         old_value="0.3", new_value="0.5",
                         permission_level=PermissionLevel.AUTO_APPROVE, reason="tighten")
        assert PolicyChangeClassifier.classify(pc, {}) == PermissionLevel.AUTO_APPROVE

    def test_loosening_threshold_requires_review(self):
        """Decreasing drift_threshold beyond 20% = loosening = human review."""
        pc = PolicyChange(change_id="t1", section="meta_loop", key="drift_threshold",
                         old_value="0.5", new_value="0.3",  # 40% decrease
                         permission_level=PermissionLevel.AUTO_APPROVE, reason="loosen")
        assert PolicyChangeClassifier.classify(pc, {}) == PermissionLevel.HUMAN_REVIEW

    def test_default_auto_approve_known_section(self):
        """Non-numeric change in known safe section = auto-approve."""
        pc = PolicyChange(change_id="t1", section="harness", key="some_setting",
                         old_value=None, new_value="value",
                         permission_level=PermissionLevel.AUTO_APPROVE, reason="test")
        assert PolicyChangeClassifier.classify(pc, {}) == PermissionLevel.AUTO_APPROVE

    def test_default_human_review_unknown_section(self):
        """Non-numeric change in unknown section = human review."""
        pc = PolicyChange(change_id="t1", section="unknown_section", key="some_key",
                         old_value=None, new_value="value",
                         permission_level=PermissionLevel.AUTO_APPROVE, reason="test")
        assert PolicyChangeClassifier.classify(pc, {}) == PermissionLevel.HUMAN_REVIEW

    def test_human_review_large_loosening_max_chain_depth(self):
        """100% increase in max_chain_depth = human review."""
        pc = PolicyChange(change_id="t1", section="harness", key="max_chain_depth",
                         old_value="100", new_value="200",
                         permission_level=PermissionLevel.AUTO_APPROVE, reason="tune")
        assert PolicyChangeClassifier.classify(pc, {}) == PermissionLevel.HUMAN_REVIEW

    def test_never_modify_meta_events_retention(self):
        """Any modification to meta_events section = NEVER."""
        pc = PolicyChange(change_id="t1", section="meta_events", key="retention",
                         old_value="365", new_value="30",
                         permission_level=PermissionLevel.AUTO_APPROVE, reason="reduce")
        assert PolicyChangeClassifier.classify(pc, {}) == PermissionLevel.NEVER

    def test_human_review_goal_reweight(self):
        pc = PolicyChange(change_id="t1", section="goals", key="G1_weight",
                         old_value="1.0", new_value="0.5",
                         permission_level=PermissionLevel.AUTO_APPROVE, reason="reweight")
        assert PolicyChangeClassifier.classify(pc, {}) == PermissionLevel.HUMAN_REVIEW

    def test_unknown_numeric_within_20_pct(self):
        """Unknown numeric param within 20% = auto-approve."""
        pc = PolicyChange(change_id="t1", section="cadence", key="some_number",
                         old_value="10", new_value="11",
                         permission_level=PermissionLevel.AUTO_APPROVE, reason="tune")
        assert PolicyChangeClassifier.classify(pc, {}) == PermissionLevel.AUTO_APPROVE

    def test_unknown_numeric_over_20_pct(self):
        """Unknown numeric param over 20% = human review."""
        pc = PolicyChange(change_id="t1", section="cadence", key="some_number",
                         old_value="10", new_value="15",
                         permission_level=PermissionLevel.AUTO_APPROVE, reason="tune")
        assert PolicyChangeClassifier.classify(pc, {}) == PermissionLevel.HUMAN_REVIEW


class TestCadenceController:
    def test_can_run_initially(self, engine, engine_env):
        assert engine.cadence.can_run_cycle() == (True, "ok")

    def test_daily_limit(self, engine, engine_env):
        for _ in range(20):
            engine.cadence.record_cycle()
        can_run, reason = engine.cadence.can_run_cycle()
        assert not can_run
        assert "daily" in reason

    def test_min_interval(self, engine, engine_env):
        engine.cadence.record_cycle()
        can_run, reason = engine.cadence.can_run_cycle()
        assert not can_run
        assert "interval" in reason

    def test_emergency_limit(self, engine, engine_env):
        engine.cadence.record_emergency()
        engine.cadence.record_emergency()
        assert not engine.cadence.can_emergency()


class TestDelayedEvaluation:
    def test_delayed_eval_inconclusive_no_data(self, engine, engine_env):
        with patch("aros_meta_loop.services.engine.get_db", return_value=engine_env["conn"]), \
             patch("aros_meta_loop.services.event_emitter.get_db", return_value=engine_env["conn"]):
            result = engine._delayed_evaluation()
            assert result["verdict"] == "INCONCLUSIVE"


class TestChannelE:
    def test_drift_restart(self, engine, engine_env):
        """Test that high drift in identity check leads to rejection."""
        changes = [PolicyChange(change_id="t1", section="harness", key="retry_limit",
                               old_value="3", new_value="4",
                               permission_level=PermissionLevel.AUTO_APPROVE, reason="tune")]
        perceive_data = {
            "l3_signals": [{"source": "L3_drift_score", "payload": {"drift_score": 0.4}}],
        }
        verdict = engine._identity_check(changes, perceive_data)
        # drift_score 0.4 > threshold 0.3 should trigger rejection
        assert "drift" in verdict.lower() or "rejected" in verdict.lower()
