"""End-to-end tests against the live MetaLoop service.

These tests require:
- MetaLoop running at localhost:8200
- mini-claude-bot gateway running at localhost:8000

Run with: pytest tests/test_e2e.py -v
Skip if services are down: tests auto-skip via the fixture.
"""
import httpx
import pytest

META_LOOP_URL = "http://localhost:8200"
GATEWAY_URL = "http://localhost:8000"


@pytest.fixture(scope="module")
def live_services():
    """Check that both services are running, skip if not."""
    try:
        with httpx.Client(timeout=3.0) as client:
            ml = client.get(f"{META_LOOP_URL}/health")
            gw = client.get(f"{GATEWAY_URL}/api/health")
            if ml.status_code != 200 or gw.status_code != 200:
                pytest.skip("MetaLoop or gateway not healthy")
    except httpx.ConnectError:
        pytest.skip("MetaLoop or gateway not running")
    return {"meta_loop": META_LOOP_URL, "gateway": GATEWAY_URL}


class TestE2EDryRun:
    """Dry run cycles — no side effects, safe to run anytime."""

    def test_dry_run_completes_all_steps(self, live_services):
        """Full dry-run cycle completes at least 6 steps."""
        with httpx.Client(timeout=30.0) as client:
            resp = client.post(
                f"{META_LOOP_URL}/api/meta-loop/trigger/adhoc",
                json={"dry_run": True, "skip_cadence": True},
            )
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] in ("completed", "completed_partial", "aborted")
        assert data["steps_completed"] >= 6
        assert data["dry_run"] is True
        assert data.get("persist_skipped") == "dry_run"
        assert data.get("plan_skipped") == "dry_run"

    def test_dry_run_returns_perceive_data(self, live_services):
        """Dry run includes full perceive data with L1/L2/L3 metrics."""
        with httpx.Client(timeout=30.0) as client:
            resp = client.post(
                f"{META_LOOP_URL}/api/meta-loop/trigger/adhoc",
                json={"dry_run": True, "skip_cadence": True},
            )
        data = resp.json()
        pd = data.get("perceive_data", {})
        assert "l1_metrics" in pd
        assert "l2_scores" in pd
        assert "l3_signals" in pd
        assert "current_cadence" in pd
        assert "current_policy" in pd

    def test_dry_run_returns_critique(self, live_services):
        """Dry run includes critique output."""
        with httpx.Client(timeout=30.0) as client:
            resp = client.post(
                f"{META_LOOP_URL}/api/meta-loop/trigger/adhoc",
                json={"dry_run": True, "skip_cadence": True},
            )
        data = resp.json()
        crit = data.get("critique_output", {})
        assert "action" in crit
        assert "reason" in crit
        assert "confidence" in crit


class TestE2EStopAfterStep:
    """Step-limited runs to test individual pipeline stages."""

    def test_stop_after_perceive(self, live_services):
        """stop_after_step=1 runs only PERCEIVE and returns partial."""
        with httpx.Client(timeout=15.0) as client:
            resp = client.post(
                f"{META_LOOP_URL}/api/meta-loop/trigger/adhoc",
                json={"stop_after_step": 1, "skip_cadence": True},
            )
        data = resp.json()
        assert data["status"] == "completed_partial"
        assert data["steps_completed"] == 1
        assert "perceive_data" in data
        assert "critique_output" not in data

    def test_stop_after_critique(self, live_services):
        """stop_after_step=3 runs through CRITIQUE."""
        with httpx.Client(timeout=15.0) as client:
            resp = client.post(
                f"{META_LOOP_URL}/api/meta-loop/trigger/adhoc",
                json={"stop_after_step": 3, "skip_cadence": True},
            )
        data = resp.json()
        assert data["status"] == "completed_partial"
        assert data["steps_completed"] == 3
        assert "critique_output" in data


class TestE2ENirmanaSync:
    """Nirmana state synchronization between gateway and meta-loop."""

    def test_nirmana_activation_syncs_cadence(self, live_services):
        """Activating nirmana at gateway should sync cadence to aggressive."""
        with httpx.Client(timeout=10.0) as client:
            # Activate nirmana at gateway
            client.post(
                f"{GATEWAY_URL}/api/gateway/nirmana",
                json={"chat_id": "-1003891385836", "bot_id": "mini_claude_bot", "action": "away"},
            )

            try:
                # Run perceive — sync should detect and correct
                resp = client.post(
                    f"{META_LOOP_URL}/api/meta-loop/trigger/adhoc",
                    json={"stop_after_step": 1, "skip_cadence": True},
                )
                data = resp.json()
                cadence = data.get("perceive_data", {}).get("current_cadence", {})
                assert cadence.get("mode") == "aggressive", (
                    f"Expected aggressive after nirmana sync, got {cadence.get('mode')}"
                )
            finally:
                # Always deactivate nirmana after test
                client.post(
                    f"{GATEWAY_URL}/api/gateway/nirmana",
                    json={"chat_id": "-1003891385836", "bot_id": "mini_claude_bot", "action": "back"},
                )
                # Reset cadence back to balanced
                client.post(
                    f"{META_LOOP_URL}/api/meta-loop/nirmana",
                    params={"activate": "false"},
                )


class TestE2EStatus:
    """Status and health endpoints."""

    def test_health_endpoint(self, live_services):
        with httpx.Client(timeout=5.0) as client:
            resp = client.get(f"{META_LOOP_URL}/health")
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"

    def test_status_includes_meta_goals(self, live_services):
        with httpx.Client(timeout=5.0) as client:
            resp = client.get(f"{META_LOOP_URL}/api/meta-loop/status")
        data = resp.json()
        assert "meta_goal_scores" in data
        scores = data["meta_goal_scores"]
        assert "G1_truthful" in scores
        assert "G5_ambitious" in scores
        assert "below_threshold" in scores

    def test_evolution_log_accessible(self, live_services):
        with httpx.Client(timeout=5.0) as client:
            resp = client.get(f"{META_LOOP_URL}/api/meta-loop/evolution-log?limit=5")
        assert resp.status_code == 200
        data = resp.json()
        assert "entries" in data
        assert "count" in data

    def test_skip_cadence_allows_rapid_cycles(self, live_services):
        """Multiple rapid adhoc triggers should not be throttled."""
        with httpx.Client(timeout=30.0) as client:
            for _ in range(2):
                resp = client.post(
                    f"{META_LOOP_URL}/api/meta-loop/trigger/adhoc",
                    json={"dry_run": True, "skip_cadence": True},
                )
                assert resp.status_code == 200
                assert resp.json()["status"] != "throttled"
