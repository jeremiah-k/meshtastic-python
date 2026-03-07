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
    """example_config.yaml should remain parseable and use canonical renamed keys."""
    assert EXAMPLE_CONFIG_PATH.exists(), f"Example config not found: {EXAMPLE_CONFIG_PATH}"
    config = yaml.safe_load(EXAMPLE_CONFIG_PATH.read_text(encoding="utf-8"))
    assert config is not None, "example_config.yaml is empty or invalid"

    assert config["owner"] == "Bob TBeam"
    assert config["owner_short"] == "BOB"
    assert config["channel_url"].startswith("https://www.meshtastic.org/")
    assert "ownerShort" not in config
    assert "channelUrl" not in config

    assert config["config"]["device"]["serial_enabled"] is True
    assert "serialEnabled" not in config["config"]["device"]
    assert config["config"]["display"]["screen_on_secs"] == 600
    assert "screenOnSecs" not in config["config"]["display"]
    assert config["config"]["lora"]["region"] == "US"
    assert config["config"]["lora"]["hop_limit"] == 3
    assert config["config"]["lora"]["tx_enabled"] is True
    assert config["config"]["lora"]["tx_power"] == 30
    assert "hopLimit" not in config["config"]["lora"]
    assert "txEnabled" not in config["config"]["lora"]
    assert "txPower" not in config["config"]["lora"]
    assert config["config"]["network"]["ntp_server"] == "0.pool.ntp.org"
    assert "ntpServer" not in config["config"]["network"]
    assert config["config"]["position"]["gps_enabled"] is True
    assert config["config"]["position"]["gps_attempt_time"] == 900
    assert config["config"]["position"]["gps_update_interval"] == 120
    assert config["config"]["position"]["position_broadcast_secs"] == 900
    assert config["config"]["position"]["position_flags"] == 3
    assert config["config"]["position"]["position_broadcast_smart_enabled"] is True
    assert "gpsEnabled" not in config["config"]["position"]
    assert "gpsAttemptTime" not in config["config"]["position"]
    assert "gpsUpdateInterval" not in config["config"]["position"]
    assert "positionBroadcastSecs" not in config["config"]["position"]
    assert "positionFlags" not in config["config"]["position"]
    assert "positionBroadcastSmartEnabled" not in config["config"]["position"]
    assert config["config"]["power"]["wait_bluetooth_secs"] == 60
    assert config["config"]["power"]["ls_secs"] == 300
    assert config["config"]["power"]["min_wake_secs"] == 10
    assert config["config"]["power"]["sds_secs"] == 4294967295
    assert config["module_config"]["telemetry"]["device_update_interval"] == 900
    assert config["module_config"]["telemetry"]["environment_update_interval"] == 900
    assert "deviceUpdateInterval" not in config["module_config"]["telemetry"]
