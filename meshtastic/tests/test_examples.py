"""Meshtastic test that the examples run as expected.
We assume you have a python virtual environment in current directory.
If not, you need to run: "python3 -m venv venv", "source venv/bin/activate", "pip install .".
"""

import subprocess
import sys

import pytest


@pytest.mark.examples
def test_examples_hello_world_serial_no_arg() -> None:
    """Test hello_world_serial without any args."""
    result = subprocess.run(
        [sys.executable, "examples/hello_world_serial.py"],
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    assert result.returncode == 3


@pytest.mark.examples
def test_examples_hello_world_serial_with_arg() -> None:
    """Test hello_world_serial with arg."""
    result = subprocess.run(
        [sys.executable, "examples/hello_world_serial.py", "hello"],
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    assert result.returncode == 1
    assert "Warning: No Meshtastic devices detected." in result.stdout
