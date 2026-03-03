"""Meshtastic integration tests."""

import re

import pytest

from .cli_test_utils import run_cli_argv_with_timeout


def _run_and_collect(cmd: list[str]) -> tuple[int, str]:
    """Run CLI argv command and return (returncode, combined output)."""
    result = run_cli_argv_with_timeout(cmd)
    return result.returncode, (result.stdout or "") + (result.stderr or "")


@pytest.mark.int
def test_int_meshtastic_no_args(meshtastic_bin: str) -> None:
    """Test meshtastic without any args."""
    returncode, output = _run_and_collect([meshtastic_bin])
    assert output.startswith("usage: meshtastic")
    assert returncode == 1


@pytest.mark.int
def test_int_mesh_tunnel_no_args(mesh_tunnel_bin: str) -> None:
    """Test mesh-tunnel without any args."""
    returncode, output = _run_and_collect([mesh_tunnel_bin])
    assert output.startswith("usage: mesh-tunnel")
    assert returncode == 1


@pytest.mark.int
def test_int_version(meshtastic_bin: str) -> None:
    """Test '--version'."""
    returncode, output = _run_and_collect([meshtastic_bin, "--version"])
    assert re.search(r"[0-9]+\.[0-9]+\.[0-9]+", output)
    assert returncode == 0


@pytest.mark.int
def test_int_help(meshtastic_bin: str) -> None:
    """Test '--help'."""
    returncode, output = _run_and_collect([meshtastic_bin, "--help"])
    assert output.startswith("usage: meshtastic ")
    assert returncode == 0


@pytest.mark.int
def test_int_support(meshtastic_bin: str) -> None:
    """Test '--support'."""
    returncode, output = _run_and_collect([meshtastic_bin, "--support"])
    assert re.search(r"System", output)
    assert re.search(r"Python", output)
    assert returncode == 0
