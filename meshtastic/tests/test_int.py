"""Meshtastic integration tests."""

import re
import subprocess

import pytest


@pytest.mark.int
def test_int_meshtastic_no_args():
    """Test meshtastic without any args."""
    result = subprocess.run(["meshtastic"], capture_output=True, text=True, check=False)
    output = result.stdout + result.stderr
    assert re.match(r"usage: meshtastic", output)
    assert result.returncode == 1


@pytest.mark.int
def test_int_mesh_tunnel_no_args():
    """Test mesh-tunnel without any args."""
    result = subprocess.run(
        ["mesh-tunnel"], capture_output=True, text=True, check=False
    )
    output = result.stdout + result.stderr
    assert re.match(r"usage: mesh-tunnel", output)
    assert result.returncode == 1


@pytest.mark.int
def test_int_version():
    """Test '--version'."""
    result = subprocess.run(
        ["meshtastic", "--version"], capture_output=True, text=True, check=False
    )
    output = result.stdout + result.stderr
    assert re.match(r"[0-9]+\.[0-9]+\.[0-9]", output)
    assert result.returncode == 0


@pytest.mark.int
def test_int_help():
    """Test '--help'."""
    result = subprocess.run(
        ["meshtastic", "--help"], capture_output=True, text=True, check=False
    )
    output = result.stdout + result.stderr
    assert re.match(r"usage: meshtastic ", output)
    assert result.returncode == 0


@pytest.mark.int
def test_int_support():
    """Test '--support'."""
    result = subprocess.run(
        ["meshtastic", "--support"], capture_output=True, text=True, check=False
    )
    output = result.stdout + result.stderr
    assert re.search(r"System", output)
    assert re.search(r"Python", output)
    assert result.returncode == 0
