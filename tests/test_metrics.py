"""Tests for L1 Metrics Collector."""
import json
import sqlite3
import pytest
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import patch, MagicMock

from aros_meta_loop.db.migrations import run_migrations
from aros_meta_loop.services.metrics import L1Collector, L2Evaluator, L3SignalDeriver


@pytest.fixture
def db_with_events(tmp_path):
    """Create a test DB seeded with sample events."""
    db_path = tmp_path / "test.db"
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    run_migrations(conn)

    now = datetime.now(timezone.utc)
    events = [
        # Tool calls
        ("bot1", "tool_call", "sess1",
         json.dumps({"tool": "grep", "success": True, "tokens_in": 1000, "tokens_out": 500, "duration_ms": 200}),
         now.isoformat()),
        ("bot1", "tool_call", "sess1",
         json.dumps({"tool": "read", "success": True, "tokens_in": 800, "tokens_out": 300, "duration_ms": 150}),
         now.isoformat()),
        ("bot1", "tool_call", "sess1",
         json.dumps({"tool": "bash", "success": False, "tokens_in": 500, "tokens_out": 200, "duration_ms": 5000}),
         now.isoformat()),
        # Task complete
        ("bot1", "task_complete", "sess1",
         json.dumps({"task_id": "t1", "tokens_consumed": 5000, "duration_seconds": 120, "retries": 1, "complexity": 3}),
         now.isoformat()),
        ("bot1", "task_complete", "sess1",
         json.dumps({"task_id": "t2", "tokens_consumed": 3000, "duration_seconds": 60, "retries": 0, "complexity": 2}),
         now.isoformat()),
        # Task failed
        ("bot1", "task_failed", "sess1",
         json.dumps({"task_id": "t3", "tokens_consumed": 2000, "duration_seconds": 30, "retries": 2, "error_type": "timeout"}),
         now.isoformat()),
        # Sessions
        ("bot1", "session_start", "sess1",
         json.dumps({"context_tokens": 50000}),
         now.isoformat()),
        ("bot1", "session_end", "sess1",
         json.dumps({"context_tokens": 80000}),
         now.isoformat()),
        # Different bot (should be filtered out)
        ("bot2", "tool_call", "sess2",
         json.dumps({"tool": "test", "success": True, "tokens_in": 9999}),
         now.isoformat()),
    ]

    for bot_id, event_type, session_id, data, created_at in events:
        conn.execute(
            "INSERT INTO meta_events (bot_id, event_type, session_id, data, created_at) VALUES (?, ?, ?, ?, ?)",
            (bot_id, event_type, session_id, data, created_at)
        )
    conn.commit()
    return conn


class TestL1Collector:
    def test_collect_returns_all_keys(self, db_with_events):
        with patch("aros_meta_loop.services.metrics.get_db", return_value=db_with_events):
            collector = L1Collector()
            since = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
            metrics = collector.collect("bot1", since=since)

            expected_keys = {
                "tokens_consumed", "tokens_per_task", "tool_call_count",
                "tool_call_success_rate", "retry_count", "error_count_by_type",
                "context_window_usage", "wall_clock_per_task", "cost_usd",
                "event_count", "task_count",
            }
            assert expected_keys.issubset(set(metrics.keys()))

    def test_tool_call_count(self, db_with_events):
        with patch("aros_meta_loop.services.metrics.get_db", return_value=db_with_events):
            collector = L1Collector()
            since = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
            metrics = collector.collect("bot1", since=since)
            assert metrics["tool_call_count"] == 3

    def test_tool_call_success_rate(self, db_with_events):
        with patch("aros_meta_loop.services.metrics.get_db", return_value=db_with_events):
            collector = L1Collector()
            since = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
            metrics = collector.collect("bot1", since=since)
            # 2 success out of 3
            assert abs(metrics["tool_call_success_rate"] - 0.667) < 0.01

    def test_retry_count(self, db_with_events):
        with patch("aros_meta_loop.services.metrics.get_db", return_value=db_with_events):
            collector = L1Collector()
            since = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
            metrics = collector.collect("bot1", since=since)
            assert metrics["retry_count"] == 3  # 1 + 0 + 2

    def test_error_count_by_type(self, db_with_events):
        with patch("aros_meta_loop.services.metrics.get_db", return_value=db_with_events):
            collector = L1Collector()
            since = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
            metrics = collector.collect("bot1", since=since)
            assert metrics["error_count_by_type"]["timeout"] == 1

    def test_bot_id_filtering(self, db_with_events):
        with patch("aros_meta_loop.services.metrics.get_db", return_value=db_with_events):
            collector = L1Collector()
            since = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
            metrics_bot1 = collector.collect("bot1", since=since)
            metrics_bot2 = collector.collect("bot2", since=since)
            assert metrics_bot1["tool_call_count"] == 3
            assert metrics_bot2["tool_call_count"] == 1

    def test_context_window_usage(self, db_with_events):
        with patch("aros_meta_loop.services.metrics.get_db", return_value=db_with_events):
            collector = L1Collector()
            since = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
            metrics = collector.collect("bot1", since=since)
            assert metrics["context_window_usage"] == 65000.0  # avg of 50000 and 80000

    def test_empty_events(self, tmp_path):
        db_path = tmp_path / "empty.db"
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        run_migrations(conn)
        with patch("aros_meta_loop.services.metrics.get_db", return_value=conn):
            collector = L1Collector()
            metrics = collector.collect("bot1")
            assert metrics["tool_call_count"] == 0
            assert metrics["tool_call_success_rate"] == 1.0
            assert metrics["tokens_consumed"] == 0


class TestL2Evaluator:
    def test_evaluate_returns_all_goals(self, db_with_events):
        with patch("aros_meta_loop.services.metrics.get_db", return_value=db_with_events):
            from aros_meta_loop.services.state_manager import StateManager
            collector = L1Collector()
            since = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
            l1 = collector.collect("bot1", since=since)

            # Create evaluator with mock state manager
            state_dir = Path(__file__).parent.parent  # temp, won't be used
            evaluator = L2Evaluator.__new__(L2Evaluator)
            evaluator.state = MagicMock()
            evaluator.state.read_goals.return_value = {
                "G1_truthful": {"weight": 1.0, "threshold": 0.8},
                "G2_efficient": {"weight": 1.0, "threshold": 0.7},
                "G3_reliable": {"weight": 1.0, "threshold": 0.85},
                "G4_aligned": {"weight": 1.0, "threshold": 0.9},
                "G5_ambitious": {"weight": 0.5, "threshold": 0.5},
                "G6_self_know": {"weight": 0.8, "threshold": 0.6},
            }

            scores = evaluator.evaluate(l1)
            assert "G1_truthful" in scores
            assert "G2_efficient" in scores
            assert "G3_reliable" in scores
            assert "G4_aligned" in scores
            assert "G5_ambitious" in scores
            assert "G6_self_know" in scores
            assert "aggregate" in scores
            assert all(0.0 <= scores[k] <= 1.0 for k in scores if k not in ("below_threshold",))

    def test_evaluate_detects_below_threshold(self, db_with_events):
        with patch("aros_meta_loop.services.metrics.get_db", return_value=db_with_events):
            evaluator = L2Evaluator.__new__(L2Evaluator)
            evaluator.state = MagicMock()
            evaluator.state.read_goals.return_value = {
                "G1_truthful": {"weight": 1.0, "threshold": 0.99},  # Very high threshold
                "G2_efficient": {"weight": 1.0, "threshold": 0.99},
                "G3_reliable": {"weight": 1.0, "threshold": 0.99},
                "G4_aligned": {"weight": 1.0, "threshold": 0.99},
                "G5_ambitious": {"weight": 0.5, "threshold": 0.99},
                "G6_self_know": {"weight": 0.8, "threshold": 0.99},
            }

            l1 = {"event_count": 10, "error_count_by_type": {"timeout": 5},
                  "tokens_per_task": 40000, "task_count": 2, "retry_count": 1,
                  "tool_call_success_rate": 0.5, "tool_call_count": 5}
            scores = evaluator.evaluate(l1)
            assert len(scores["below_threshold"]) > 0


class TestL3SignalDeriver:
    def test_derive_returns_5_signals(self, db_with_events):
        with patch("aros_meta_loop.services.metrics.get_db", return_value=db_with_events):
            collector = L1Collector()
            since = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
            l1 = collector.collect("bot1", since=since)

            evaluator = L2Evaluator.__new__(L2Evaluator)
            evaluator.state = MagicMock()
            evaluator.state.read_goals.return_value = {
                "G1_truthful": {"weight": 1.0, "threshold": 0.8},
                "G2_efficient": {"weight": 1.0, "threshold": 0.7},
                "G3_reliable": {"weight": 1.0, "threshold": 0.85},
                "G4_aligned": {"weight": 1.0, "threshold": 0.9},
                "G5_ambitious": {"weight": 0.5, "threshold": 0.5},
                "G6_self_know": {"weight": 0.8, "threshold": 0.6},
            }
            l2 = evaluator.evaluate(l1)

            deriver = L3SignalDeriver()
            signals = deriver.derive(l1, l2, {"capabilities": {}})

            assert len(signals) == 5
            sources = [s.source for s in signals]
            assert "L3_strategy_effectiveness" in sources
            assert "L3_tool_underutilization" in sources
            assert "L3_confidence_calibration" in sources
            assert "L3_drift_score" in sources
            assert "L3_pattern_recurrence" in sources

    def test_derive_signals_have_cross_validation(self, db_with_events):
        with patch("aros_meta_loop.services.metrics.get_db", return_value=db_with_events):
            deriver = L3SignalDeriver()
            l1 = {"retry_count": 0, "task_count": 5, "tool_call_count": 20,
                   "tool_call_success_rate": 0.9, "error_count_by_type": {}}
            l2 = {"G6_self_know": 0.9, "below_threshold": []}
            signals = deriver.derive(l1, l2, {})

            for s in signals:
                assert s.validation_status == "validated"
                assert "cross_validated_with" in s.payload

    def test_derive_drift_urgent_signal(self, db_with_events):
        deriver = L3SignalDeriver()
        l1 = {"retry_count": 5, "task_count": 5, "tool_call_count": 2,
               "tool_call_success_rate": 0.3, "error_count_by_type": {"timeout": 3}}
        l2 = {"G6_self_know": 0.3, "below_threshold": ["G1", "G2", "G3", "G4"]}
        signals = deriver.derive(l1, l2, {})

        drift = [s for s in signals if s.source == "L3_drift_score"][0]
        assert drift.priority == "urgent"  # 4/6 > 0.5
