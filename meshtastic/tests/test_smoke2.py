"""Meshtastic smoke tests with 2 devices connected via USB."""

import re
import subprocess

import pytest

INFO_TIMEOUT_SECONDS = 30
TEST_TIMEOUT_SECONDS = 60


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
            timeout=INFO_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as exc:
        pytest.fail(
            f"meshtastic --info timed out after {INFO_TIMEOUT_SECONDS} seconds: {exc}"
        )
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
            timeout=TEST_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as exc:
        pytest.fail(
            f"meshtastic --test timed out after {TEST_TIMEOUT_SECONDS} seconds: {exc}"
        )
    assert re.search(r"Writing serial debugging", result.stdout, re.MULTILINE)
    assert re.search(r"Ports opened", result.stdout, re.MULTILINE)
    assert re.search(r"Running [1-9]\d* tests", result.stdout, re.MULTILINE)
    assert result.returncode == 0
