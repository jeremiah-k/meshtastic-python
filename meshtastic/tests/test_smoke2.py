"""Meshtastic smoke tests with 2 devices connected via USB."""

import re
import shutil
import subprocess

import pytest

# Resolve meshtastic binary path to avoid Ruff S607 (partial executable path)
_MESHTASTIC_BIN = shutil.which("meshtastic") or "meshtastic"


@pytest.mark.smoke2
def test_smoke2_info() -> None:
    """Test --info with 2 devices connected serially."""
    result = subprocess.run(
        [_MESHTASTIC_BIN, "--info"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        check=False,
        timeout=30,
    )
    assert re.search(r"Warning: Multiple", result.stdout, re.MULTILINE)
    assert result.returncode == 1


@pytest.mark.smoke2
def test_smoke2_test() -> None:
    """Test --test."""
    result = subprocess.run(
        [_MESHTASTIC_BIN, "--test"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        check=False,
        timeout=60,
    )
    assert re.search(r"Writing serial debugging", result.stdout, re.MULTILINE)
    assert re.search(r"Ports opened", result.stdout, re.MULTILINE)
    assert re.search(r"Running [1-9]\d* tests", result.stdout, re.MULTILINE)
    assert result.returncode == 0
