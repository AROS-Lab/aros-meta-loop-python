"""Configuration for AROS Meta Loop."""
import os
from pathlib import Path

AROS_STATE_DIR = Path(os.getenv("AROS_STATE_DIR", str(Path.home() / ".aros")))
DATABASE_PATH = AROS_STATE_DIR / "data" / "meta-loop.db"
META_LOOP_PORT = int(os.getenv("META_LOOP_PORT", "8200"))
MINI_CLAUDE_BOT_URL = os.getenv("MINI_CLAUDE_BOT_URL", "http://localhost:8001")
