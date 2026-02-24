"""Meshtastic integration tests."""

import re
import subprocess

import pytest


def _run_cli_with_timeout(cmd: list[str]) -> subprocess.CompletedProcess[str]:
    """Run a CLI command with a bounded timeout and fail clearly on timeout."""
    try:
        return subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
    except subprocess.TimeoutExpired as e:
        pytest.fail(f"meshtastic command timed out: {e}")
        raise  # pragma: no cover - pytest.fail always raises


@pytest.mark.int
def test_int_meshtastic_no_args(meshtastic_bin: str) -> None:
    """Test meshtastic without any args."""
    result = _run_cli_with_timeout([meshtastic_bin])
    output = result.stdout + result.stderr
    assert re.match(r"usage: meshtastic", output)
    assert result.returncode == 1


@pytest.mark.int
def test_int_mesh_tunnel_no_args(mesh_tunnel_bin: str) -> None:
    """Test mesh-tunnel without any args."""
    result = _run_cli_with_timeout([mesh_tunnel_bin])
    output = result.stdout + result.stderr
    assert re.match(r"usage: mesh-tunnel", output)
    assert result.returncode == 1


@pytest.mark.int
def test_int_version(meshtastic_bin: str) -> None:
    """Test '--version'."""
    result = _run_cli_with_timeout([meshtastic_bin, "--version"])
    output = result.stdout + result.stderr
    assert re.match(r"[0-9]+\.[0-9]+\.[0-9]", output)
    assert result.returncode == 0


@pytest.mark.int
def test_int_help(meshtastic_bin: str) -> None:
    """Test '--help'."""
    result = _run_cli_with_timeout([meshtastic_bin, "--help"])
    output = result.stdout + result.stderr
    assert re.match(r"usage: meshtastic ", output)
    assert result.returncode == 0


@pytest.mark.int
def test_int_support(meshtastic_bin: str) -> None:
    """Test '--support'."""
    result = _run_cli_with_timeout([meshtastic_bin, "--support"])
    output = result.stdout + result.stderr
    assert re.search(r"System", output)
    assert re.search(r"Python", output)
    assert result.returncode == 0
