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
# Shared by example-config structure tests and external test helpers.
EXAMPLE_CONFIG_PATH: Path = Path(__file__).resolve().parents[2] / "example_config.yaml"


def _require_mapping(value: object, *, label: str) -> dict[str, object]:
    """Assert that `value` is a dict-like YAML mapping and return it."""
    assert isinstance(
        value, dict
    ), f"{label} should be a mapping, got {type(value).__name__}"
    return cast(dict[str, object], value)


def _require_int_strict(value: object, *, label: str) -> int:
    """Assert that `value` is an int (excluding bool) and return it."""
    assert isinstance(
        value, int
    ), f"{label} should be an int, got {type(value).__name__}"
    assert not isinstance(value, bool), f"{label} must not be a bool"
    return cast(int, value)


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
    assert channel_url.startswith(
        "https://meshtastic.org/e/#"
    ), f"channel_url should start with the Meshtastic share URL, got: {channel_url}"
    assert "ownerShort" not in config
    assert "channelUrl" not in config

    config_section = _require_mapping(config.get("config"), label="config")
    module_config = _require_mapping(config.get("module_config"), label="module_config")

    bluetooth_cfg = _require_mapping(
        config_section.get("bluetooth"), label="config.bluetooth"
    )
    assert isinstance(bluetooth_cfg.get("enabled"), bool)
    assert "fixed_pin" not in bluetooth_cfg
    assert "fixedPin" not in bluetooth_cfg

    device_cfg = _require_mapping(config_section.get("device"), label="config.device")
    assert isinstance(device_cfg.get("serial_enabled"), bool)
    assert "serialEnabled" not in device_cfg

    display_cfg = _require_mapping(
        config_section.get("display"), label="config.display"
    )
    _require_int_strict(display_cfg.get("screen_on_secs"), label="screen_on_secs")
    assert "screenOnSecs" not in display_cfg

    lora_cfg = _require_mapping(config_section.get("lora"), label="config.lora")
    region = lora_cfg.get("region")
    assert isinstance(region, str), "region should be a string"
    assert region, "region should not be empty"
    _require_int_strict(lora_cfg.get("hop_limit"), label="hop_limit")
    assert isinstance(lora_cfg.get("tx_enabled"), bool)
    _require_int_strict(lora_cfg.get("tx_power"), label="tx_power")
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
    _require_int_strict(position_cfg.get("gps_attempt_time"), label="gps_attempt_time")
    _require_int_strict(
        position_cfg.get("gps_update_interval"), label="gps_update_interval"
    )
    _require_int_strict(
        position_cfg.get("position_broadcast_secs"), label="position_broadcast_secs"
    )
    _require_int_strict(position_cfg.get("position_flags"), label="position_flags")
    assert isinstance(position_cfg.get("position_broadcast_smart_enabled"), bool)
    assert "gpsEnabled" not in position_cfg
    assert "gpsAttemptTime" not in position_cfg
    assert "gpsUpdateInterval" not in position_cfg
    assert "positionBroadcastSecs" not in position_cfg
    assert "positionFlags" not in position_cfg
    assert "positionBroadcastSmartEnabled" not in position_cfg

    power_cfg = _require_mapping(config_section.get("power"), label="config.power")
    _require_int_strict(
        power_cfg.get("wait_bluetooth_secs"), label="wait_bluetooth_secs"
    )
    _require_int_strict(power_cfg.get("ls_secs"), label="ls_secs")
    _require_int_strict(power_cfg.get("min_wake_secs"), label="min_wake_secs")
    _require_int_strict(power_cfg.get("sds_secs"), label="sds_secs")
    assert "waitBluetoothSecs" not in power_cfg
    assert "lsSecs" not in power_cfg
    assert "minWakeSecs" not in power_cfg
    assert "sdsSecs" not in power_cfg

    telemetry_cfg = _require_mapping(
        module_config.get("telemetry"), label="module_config.telemetry"
    )
    _require_int_strict(
        telemetry_cfg.get("device_update_interval"), label="device_update_interval"
    )
    _require_int_strict(
        telemetry_cfg.get("environment_update_interval"),
        label="environment_update_interval",
    )
    assert "deviceUpdateInterval" not in telemetry_cfg
    assert "environmentUpdateInterval" not in telemetry_cfg
