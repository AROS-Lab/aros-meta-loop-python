"""Test fixtures for AROS Meta Loop."""
import pytest
from pathlib import Path


@pytest.fixture
def tmp_state_dir(tmp_path):
    """Provide a temporary AROS state directory."""
    state_dir = tmp_path / ".aros"
    state_dir.mkdir()
    return state_dir
