"""Meshtastic tests that examples run as expected."""

from __future__ import annotations

import logging
import runpy
import sys
from pathlib import Path

import pytest
import serial.tools.list_ports  # type: ignore[import-untyped]
import yaml

HELLO_WORLD_SERIAL_PATH = (
    Path(__file__).resolve().parents[2] / "examples" / "hello_world_serial.py"
)
EXAMPLE_CONFIG_PATH = Path(__file__).resolve().parents[2] / "example_config.yaml"


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

    out, err = capsys.readouterr()
    assert exc_info.value.code == 2
    assert out == ""
    assert err.startswith("usage: ")
    assert err.strip().endswith("hello_world_serial.py message")


@pytest.mark.examples
def test_examples_hello_world_serial_with_arg(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Test hello_world_serial with arg in a mocked no-device environment."""
    monkeypatch.setattr(
        serial.tools.list_ports, "comports", lambda *_args, **_kwargs: []
    )
    # Script logs a warning and returns normally when no serial device is found.
    with caplog.at_level(logging.WARNING):
        _run_hello_world_serial(monkeypatch, "hello")

    assert "No serial Meshtastic device detected" in caplog.text


@pytest.mark.examples
def test_examples_example_config_yaml_is_valid() -> None:
    """example_config.yaml should remain parseable and use canonical power keys."""
    config = yaml.safe_load(EXAMPLE_CONFIG_PATH.read_text(encoding="utf-8"))

    assert config["owner"] == "Bob TBeam"
    assert config["config"]["power"]["wait_bluetooth_secs"] == 60
    assert config["config"]["power"]["ls_secs"] == 300
    assert config["config"]["power"]["min_wake_secs"] == 10
    assert config["config"]["power"]["sds_secs"] == 4294967295
