"""Meshtastic tests that examples run as expected."""

from __future__ import annotations

import logging
import runpy
import sys
from pathlib import Path

import pytest
import serial.tools.list_ports  # type: ignore[import-untyped]

HELLO_WORLD_SERIAL_PATH = (
    Path(__file__).resolve().parents[2] / "examples" / "hello_world_serial.py"
)


def _run_hello_world_serial(monkeypatch: pytest.MonkeyPatch, *args: str) -> None:
    """Execute the hello_world_serial example in-process with controlled argv."""
    monkeypatch.setattr(sys, "argv", ["examples/hello_world_serial.py", *args])
    runpy.run_path(
        str(HELLO_WORLD_SERIAL_PATH),
        run_name="__main__",
    )


@pytest.mark.examples
def test_examples_hello_world_serial_no_arg(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Test hello_world_serial without any args."""
    with pytest.raises(SystemExit) as exc_info:
        _run_hello_world_serial(monkeypatch)

    out, _err = capsys.readouterr()
    assert exc_info.value.code == 3
    assert out.startswith("usage: ")
    assert out.strip().endswith("hello_world_serial.py message")


@pytest.mark.examples
def test_examples_hello_world_serial_with_arg(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Test hello_world_serial with arg in a mocked no-device environment."""
    monkeypatch.setattr(
        serial.tools.list_ports, "comports", lambda *_args, **_kwargs: []
    )
    with caplog.at_level(logging.WARNING):
        _run_hello_world_serial(monkeypatch, "hello")

    assert "No serial Meshtastic device detected" in caplog.text
