import json
import pytest
from pathlib import Path
from aros_meta_loop.services.state_manager import StateManager
from aros_meta_loop.services.event_emitter import EventEmitter
from aros_meta_loop.models.signals import Signal, CriticAction, CriticOutput, PermissionLevel, PolicyChange


@pytest.fixture
def state_mgr(tmp_path):
    """StateManager with temp directory, pre-populated with defaults."""
    from aros_meta_loop.config import _write_default, META_COGNITION_DEFAULT, SELF_MODEL_DEFAULT, POLICY_DEFAULT
    state_dir = tmp_path / ".aros"
    state_dir.mkdir()
    (state_dir / "signals").mkdir()
    (state_dir / "pending-review").mkdir()
    (state_dir / "state").mkdir()
    (state_dir / "data").mkdir()
    _write_default(state_dir / "meta-cognition.toml", META_COGNITION_DEFAULT)
    _write_default(state_dir / "self-model.toml", SELF_MODEL_DEFAULT)
    _write_default(state_dir / "policy.toml", POLICY_DEFAULT)
    return StateManager(state_dir=state_dir)


class TestStateManager:
    def test_read_cadence(self, state_mgr):
        cadence = state_mgr.read_cadence()
        assert cadence["max_cycles_per_hour"] == 4
        assert cadence["max_cycles_per_day"] == 20
        assert cadence["mode"] == "balanced"
        assert cadence["cron"]["normal_interval_hours"] == 4
        assert cadence["cron"]["away_interval_minutes"] == 30

    def test_read_policy(self, state_mgr):
        policy = state_mgr.read_policy()
        assert policy["harness"]["max_chain_depth"] == 100
        assert policy["meta_loop"]["shadow_test_window"] == 5
        assert policy["meta_loop"]["drift_threshold"] == 0.3

    def test_read_self_model(self, state_mgr):
        model = state_mgr.read_self_model()
        assert "capabilities" in model

    def test_read_goals(self, state_mgr):
        goals = state_mgr.read_goals()
        assert "G1_truthful" in goals
        assert goals["G1_truthful"]["weight"] == 1.0

    def test_write_snapshot_atomic(self, state_mgr):
        state_mgr.write_snapshot("policy.toml", {
            "harness": {"max_chain_depth": 200, "max_batch_size": 10, "retry_limit": 5},
            "meta_loop": {"shadow_test_window": 10, "drift_threshold": 0.5},
        })
        policy = state_mgr.read_policy()
        assert policy["harness"]["max_chain_depth"] == 200

    def test_append_and_read_evolution(self, state_mgr):
        state_mgr.append_evolution({"cycle": 1, "action": "test"})
        state_mgr.append_evolution({"cycle": 2, "action": "test2"})
        log = state_mgr.read_evolution_log()
        assert len(log) == 2
        assert log[0]["cycle"] == 1
        assert log[1]["cycle"] == 2
        assert "timestamp" in log[0]

    def test_signal_push_drain(self, state_mgr):
        state_mgr.push_signal({"source": "test", "priority": "normal", "payload": {"key": "val"}})
        state_mgr.push_signal({"source": "test2", "priority": "urgent", "payload": {}})

        signals = state_mgr.drain_signals()
        assert len(signals) == 2
        assert signals[0]["source"] == "test"
        assert signals[1]["source"] == "test2"

        # After drain, no more signals
        assert state_mgr.drain_signals() == []

    def test_has_urgent(self, state_mgr):
        assert not state_mgr.has_urgent()
        state_mgr.push_signal({"source": "test", "priority": "normal"})
        assert not state_mgr.has_urgent()
        state_mgr.push_signal({"source": "test", "priority": "urgent"})
        assert state_mgr.has_urgent()

    def test_read_missing_toml(self, state_mgr):
        result = state_mgr._read_toml("nonexistent.toml")
        assert result == {}


class TestEventEmitter:
    def test_emit_event(self, tmp_path):
        """Test event emission to DB."""
        import sqlite3
        from aros_meta_loop.db.migrations import run_migrations
        from unittest.mock import patch

        db_path = tmp_path / "test.db"
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        run_migrations(conn)

        with patch("aros_meta_loop.services.event_emitter.get_db", return_value=conn):
            emitter = EventEmitter()
            emitter.emit_event("bot1", "tool_call", "sess1", {"tool": "grep"})

        row = conn.execute("SELECT * FROM meta_events WHERE bot_id='bot1'").fetchone()
        assert row is not None
        assert row["event_type"] == "tool_call"
        data = json.loads(row["data"])
        assert data["tool"] == "grep"
        conn.close()

    def test_emit_event_fire_and_forget(self, tmp_path):
        """Test that emit doesn't raise on DB errors."""
        from unittest.mock import patch, MagicMock
        mock_db = MagicMock()
        mock_db.execute.side_effect = Exception("DB error")
        with patch("aros_meta_loop.services.event_emitter.get_db", return_value=mock_db):
            emitter = EventEmitter()
            emitter.emit_event("bot1", "tool_call")  # Should not raise


class TestModels:
    def test_signal_dataclass(self):
        s = Signal(source="test", priority="normal", timestamp="2026-01-01T00:00:00")
        assert s.source == "test"
        assert s.ttl == 3600

    def test_critic_output(self):
        c = CriticOutput(action=CriticAction.POLICY_UPDATE, reason="test")
        assert c.action == CriticAction.POLICY_UPDATE
        assert c.confidence == 0.5

    def test_policy_change(self):
        pc = PolicyChange(
            change_id="ch1", section="harness", key="retry_limit",
            old_value="3", new_value="5",
            permission_level=PermissionLevel.AUTO_APPROVE, reason="tune"
        )
        assert pc.status == "pending"

    def test_critic_action_enum(self):
        assert CriticAction.ALERT.value == "ALERT"
        assert CriticAction.NO_ACTION.value == "NO_ACTION"
