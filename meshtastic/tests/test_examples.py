"""Meshtastic tests that examples run as expected."""

from __future__ import annotations

import logging
import runpy
import sys
from pathlib import Path
from typing import cast

import pytest
import serial.tools.list_ports  # type: ignore[import-untyped]
import yaml

HELLO_WORLD_SERIAL_PATH = (
    Path(__file__).resolve().parents[2] / "examples" / "hello_world_serial.py"
)
EXAMPLE_CONFIG_PATH: Path = Path(__file__).resolve().parents[2] / "example_config.yaml"


def _require_mapping(value: object, *, label: str) -> dict[str, object]:
    """Assert that `value` is a dict-like YAML mapping and return it."""
    assert isinstance(
        value, dict
    ), f"{label} should be a mapping, got {type(value).__name__}"
    return cast(dict[str, object], value)


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
    assert (
        EXAMPLE_CONFIG_PATH.exists()
    ), f"Example config not found: {EXAMPLE_CONFIG_PATH}"
    config = _require_mapping(
        yaml.safe_load(EXAMPLE_CONFIG_PATH.read_text(encoding="utf-8")),
        label="example_config.yaml",
    )

    owner = config.get("owner")
    owner_short = config.get("owner_short")
    channel_url = config.get("channel_url")
    assert isinstance(owner, str), "owner should be a string"
    assert owner.strip(), "owner should not be empty/whitespace"
    assert isinstance(owner_short, str), "owner_short should be a string"
    assert owner_short.strip(), "owner_short should not be empty/whitespace"
    assert isinstance(channel_url, str), "channel_url should be a string"
    assert channel_url.startswith("https://www.meshtastic.org/"), (
        f"channel_url should start with meshtastic.org URL, got: {channel_url}"
    )
    assert "ownerShort" not in config
    assert "channelUrl" not in config

    config_section = _require_mapping(config.get("config"), label="config")
    module_config = _require_mapping(config.get("module_config"), label="module_config")

    bluetooth_cfg = _require_mapping(
        config_section.get("bluetooth"), label="config.bluetooth"
    )
    assert isinstance(bluetooth_cfg.get("enabled"), bool)
    assert "fixedPin" not in bluetooth_cfg

    device_cfg = _require_mapping(config_section.get("device"), label="config.device")
    assert isinstance(device_cfg.get("serial_enabled"), bool)
    assert "serialEnabled" not in device_cfg

    display_cfg = _require_mapping(
        config_section.get("display"), label="config.display"
    )
    assert isinstance(display_cfg.get("screen_on_secs"), int)
    assert "screenOnSecs" not in display_cfg

    lora_cfg = _require_mapping(config_section.get("lora"), label="config.lora")
    region = lora_cfg.get("region")
    assert isinstance(region, str), "region should be a string"
    assert region, "region should not be empty"
    assert isinstance(lora_cfg.get("hop_limit"), int)
    assert isinstance(lora_cfg.get("tx_enabled"), bool)
    assert isinstance(lora_cfg.get("tx_power"), int)
    assert "hopLimit" not in lora_cfg
    assert "txEnabled" not in lora_cfg
    assert "txPower" not in lora_cfg

    network_cfg = _require_mapping(
        config_section.get("network"), label="config.network"
    )
    assert isinstance(network_cfg.get("ntp_server"), str)
    assert "ntpServer" not in network_cfg

    position_cfg = _require_mapping(
        config_section.get("position"), label="config.position"
    )
    assert isinstance(position_cfg.get("gps_enabled"), bool)
    assert isinstance(position_cfg.get("gps_attempt_time"), int)
    assert isinstance(position_cfg.get("gps_update_interval"), int)
    assert isinstance(position_cfg.get("position_broadcast_secs"), int)
    assert isinstance(position_cfg.get("position_flags"), int)
    assert isinstance(position_cfg.get("position_broadcast_smart_enabled"), bool)
    assert "gpsEnabled" not in position_cfg
    assert "gpsAttemptTime" not in position_cfg
    assert "gpsUpdateInterval" not in position_cfg
    assert "positionBroadcastSecs" not in position_cfg
    assert "positionFlags" not in position_cfg
    assert "positionBroadcastSmartEnabled" not in position_cfg

    power_cfg = _require_mapping(config_section.get("power"), label="config.power")
    assert isinstance(power_cfg.get("wait_bluetooth_secs"), int)
    assert isinstance(power_cfg.get("ls_secs"), int)
    assert isinstance(power_cfg.get("min_wake_secs"), int)
    assert isinstance(power_cfg.get("sds_secs"), int)
    assert "waitBluetoothSecs" not in power_cfg
    assert "lsSecs" not in power_cfg
    assert "minWakeSecs" not in power_cfg
    assert "sdsSecs" not in power_cfg

    telemetry_cfg = _require_mapping(
        module_config.get("telemetry"), label="module_config.telemetry"
    )
    assert isinstance(telemetry_cfg.get("device_update_interval"), int)
    assert isinstance(telemetry_cfg.get("environment_update_interval"), int)
    assert "deviceUpdateInterval" not in telemetry_cfg
    assert "environmentUpdateInterval" not in telemetry_cfg
