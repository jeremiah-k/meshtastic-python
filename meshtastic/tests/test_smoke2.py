"""Meshtastic smoke tests with 2 devices connected via USB."""

import re
import subprocess

import pytest


@pytest.mark.smoke2
def test_smoke2_info():
    """Test --info with 2 devices connected serially."""
    result = subprocess.run(
        ["meshtastic", "--info"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert re.search(r"Warning: Multiple", result.stdout, re.MULTILINE)
    assert result.returncode == 1


@pytest.mark.smoke2
def test_smoke2_test():
    """Test --test."""
    result = subprocess.run(
        ["meshtastic", "--test"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert re.search(r"Writing serial debugging", result.stdout, re.MULTILINE)
    assert re.search(r"Ports opened", result.stdout, re.MULTILINE)
    assert re.search(r"Running [1-9]\d* tests", result.stdout, re.MULTILINE)
    assert result.returncode == 0
