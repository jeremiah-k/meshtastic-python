"""Meshtastic integration tests."""

import re

import pytest

from .cli_test_utils import run_cli_argv_with_timeout


@pytest.mark.int
def test_int_meshtastic_no_args(meshtastic_bin: str) -> None:
    """Test meshtastic without any args."""
    result = run_cli_argv_with_timeout([meshtastic_bin])
    output = result.stdout + result.stderr
    assert re.match(r"usage: meshtastic", output)
    assert result.returncode == 1


@pytest.mark.int
def test_int_mesh_tunnel_no_args(mesh_tunnel_bin: str) -> None:
    """Test mesh-tunnel without any args."""
    result = run_cli_argv_with_timeout([mesh_tunnel_bin])
    output = result.stdout + result.stderr
    assert re.match(r"usage: mesh-tunnel", output)
    assert result.returncode == 1


@pytest.mark.int
def test_int_version(meshtastic_bin: str) -> None:
    """Test '--version'."""
    result = run_cli_argv_with_timeout([meshtastic_bin, "--version"])
    output = result.stdout + result.stderr
    assert re.match(r"[0-9]+\.[0-9]+\.[0-9]", output)
    assert result.returncode == 0


@pytest.mark.int
def test_int_help(meshtastic_bin: str) -> None:
    """Test '--help'."""
    result = run_cli_argv_with_timeout([meshtastic_bin, "--help"])
    output = result.stdout + result.stderr
    assert re.match(r"usage: meshtastic ", output)
    assert result.returncode == 0


@pytest.mark.int
def test_int_support(meshtastic_bin: str) -> None:
    """Test '--support'."""
    result = run_cli_argv_with_timeout([meshtastic_bin, "--support"])
    output = result.stdout + result.stderr
    assert re.search(r"System", output)
    assert re.search(r"Python", output)
    assert result.returncode == 0
