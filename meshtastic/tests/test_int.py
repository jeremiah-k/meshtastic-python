"""Meshtastic integration tests."""

import re
import subprocess

import pytest


@pytest.mark.int
def test_int_meshtastic_no_args(meshtastic_bin: str) -> None:
    """Test meshtastic without any args."""
    result = subprocess.run(
        [meshtastic_bin], capture_output=True, text=True, check=False
    )
    output = result.stdout + result.stderr
    assert re.match(r"usage: meshtastic", output)
    assert result.returncode == 1


@pytest.mark.int
def test_int_mesh_tunnel_no_args(mesh_tunnel_bin: str) -> None:
    """Test mesh-tunnel without any args."""
    result = subprocess.run(
        [mesh_tunnel_bin], capture_output=True, text=True, check=False
    )
    output = result.stdout + result.stderr
    assert re.match(r"usage: mesh-tunnel", output)
    assert result.returncode == 1


@pytest.mark.int
def test_int_version(meshtastic_bin: str) -> None:
    """Test '--version'."""
    result = subprocess.run(
        [meshtastic_bin, "--version"], capture_output=True, text=True, check=False
    )
    output = result.stdout + result.stderr
    assert re.match(r"[0-9]+\.[0-9]+\.[0-9]", output)
    assert result.returncode == 0


@pytest.mark.int
def test_int_help(meshtastic_bin: str) -> None:
    """Test '--help'."""
    result = subprocess.run(
        [meshtastic_bin, "--help"], capture_output=True, text=True, check=False
    )
    output = result.stdout + result.stderr
    assert re.match(r"usage: meshtastic ", output)
    assert result.returncode == 0


@pytest.mark.int
def test_int_support(meshtastic_bin: str) -> None:
    """Test '--support'."""
    result = subprocess.run(
        [meshtastic_bin, "--support"], capture_output=True, text=True, check=False
    )
    output = result.stdout + result.stderr
    assert re.search(r"System", output)
    assert re.search(r"Python", output)
    assert result.returncode == 0
