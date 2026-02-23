"""Meshtastic smoke tests with 2 devices connected via USB."""

import re
import subprocess

import pytest


@pytest.mark.smoke2
def test_smoke2_info(meshtastic_bin: str) -> None:
    """Test --info with 2 devices connected serially."""
    try:
        result = subprocess.run(
            [meshtastic_bin, "--info"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            check=False,
            timeout=30,
        )
    except subprocess.TimeoutExpired as exc:
        pytest.fail(f"meshtastic --info timed out after 30 seconds: {exc}")
    assert re.search(r"Warning: Multiple", result.stdout, re.MULTILINE)
    assert result.returncode == 1


@pytest.mark.smoke2
def test_smoke2_test(meshtastic_bin: str) -> None:
    """Test --test."""
    try:
        result = subprocess.run(
            [meshtastic_bin, "--test"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            check=False,
            timeout=60,
        )
    except subprocess.TimeoutExpired as exc:
        pytest.fail(f"meshtastic --test timed out after 60 seconds: {exc}")
    assert re.search(r"Writing serial debugging", result.stdout, re.MULTILINE)
    assert re.search(r"Ports opened", result.stdout, re.MULTILINE)
    assert re.search(r"Running [1-9]\d* tests", result.stdout, re.MULTILINE)
    assert result.returncode == 0
