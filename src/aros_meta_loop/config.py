"""Configuration for AROS Meta Loop."""
import os
from pathlib import Path

AROS_STATE_DIR = Path(os.getenv("AROS_STATE_DIR", str(Path.home() / ".aros")))
DATABASE_PATH = AROS_STATE_DIR / "data" / "meta-loop.db"
META_LOOP_PORT = int(os.getenv("META_LOOP_PORT", "8200"))
MINI_CLAUDE_BOT_URL = os.getenv("MINI_CLAUDE_BOT_URL", "http://localhost:8001")

# ---------------------------------------------------------------------------
# Default TOML configs
# ---------------------------------------------------------------------------

META_COGNITION_DEFAULT = """\
[cadence]
max_cycles_per_hour = 4
max_cycles_per_day = 20
max_emergency_per_hour = 2
min_interval_between_cycles_seconds = 180
per_step_timeout_seconds = 60
total_cycle_timeout_seconds = 300
mode = "balanced"

[cadence.cron]
normal_interval_hours = 4
away_interval_minutes = 30

[goals]
# G1-G6 meta-goals (weights and thresholds)
[goals.G1_truthful]
weight = 1.0
threshold = 0.8

[goals.G2_efficient]
weight = 1.0
threshold = 0.7

[goals.G3_reliable]
weight = 1.0
threshold = 0.85

[goals.G4_aligned]
weight = 1.0
threshold = 0.9

[goals.G5_ambitious]
weight = 0.5
threshold = 0.5

[goals.G6_self_know]
weight = 0.8
threshold = 0.6
"""

SELF_MODEL_DEFAULT = """\
[capabilities]
# Auto-populated by meta loop

[calibration]
# confidence_accuracy = 0.0
# last_updated = ""
"""

POLICY_DEFAULT = """\
[harness]
max_chain_depth = 100
max_batch_size = 5
retry_limit = 3

[meta_loop]
shadow_test_window = 5
drift_threshold = 0.3
"""


def init_state_dir():
    """Create ~/.aros/ with default config files if missing."""
    AROS_STATE_DIR.mkdir(parents=True, exist_ok=True)
    (AROS_STATE_DIR / "data").mkdir(exist_ok=True)
    (AROS_STATE_DIR / "signals").mkdir(exist_ok=True)
    (AROS_STATE_DIR / "pending-review").mkdir(exist_ok=True)
    (AROS_STATE_DIR / "state").mkdir(exist_ok=True)

    _write_default(AROS_STATE_DIR / "meta-cognition.toml", META_COGNITION_DEFAULT)
    _write_default(AROS_STATE_DIR / "self-model.toml", SELF_MODEL_DEFAULT)
    _write_default(AROS_STATE_DIR / "policy.toml", POLICY_DEFAULT)


def _write_default(path: Path, content: str):
    """Write content to path only if the file does not already exist."""
    if not path.exists():
        path.write_text(content)
