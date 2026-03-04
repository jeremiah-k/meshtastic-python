"""Meshtastic smoke tests with 2 devices connected via USB."""

import re
import subprocess

import pytest

INFO_TIMEOUT_SECONDS = 30
TEST_TIMEOUT_SECONDS = 60


def _run_meshtastic(
    cmd: list[str],
    timeout_seconds: int,
) -> subprocess.CompletedProcess[str]:
    """Run a meshtastic CLI command with a timeout and fail cleanly on timeout."""
    try:
        return subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            check=False,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired as exc:
        pytest.fail(f"{' '.join(cmd)} timed out after {timeout_seconds} seconds: {exc}")


@pytest.mark.smoke2
def test_smoke2_info(meshtastic_bin: str) -> None:
    """Test --info with 2 devices connected serially."""
    result = _run_meshtastic([meshtastic_bin, "--info"], INFO_TIMEOUT_SECONDS)
    assert re.search(r"Warning: Multiple", result.stdout, re.MULTILINE)
    assert result.returncode == 1


@pytest.mark.smoke2
def test_smoke2_test(meshtastic_bin: str) -> None:
    """Test --test."""
    result = _run_meshtastic([meshtastic_bin, "--test"], TEST_TIMEOUT_SECONDS)
    assert re.search(r"Writing serial debugging", result.stdout, re.MULTILINE)
    assert re.search(r"Ports opened", result.stdout, re.MULTILINE)
    assert re.search(r"Running [1-9]\d* tests", result.stdout, re.MULTILINE)
    assert result.returncode == 0
