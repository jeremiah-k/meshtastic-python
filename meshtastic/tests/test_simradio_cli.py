"""Native meshtasticd single-node CLI smoke coverage.

Each test receives a freshly erased simulator. Mutations are verified through a
new ``TCPInterface`` connection so assertions cover argparse, transport,
firmware persistence, and the fork's library state model without depending on
human-oriented CLI wording.
"""

from __future__ import annotations

import base64
import re
import time
from functools import partial
from pathlib import Path

import pytest
import yaml

from meshtastic.protobuf import channel_pb2, config_pb2
from meshtastic.tcp_interface import TCPInterface

from .simradio_harness import SimNode
from .simradio_helpers import (
    PAUSE_AFTER_CLI_SECONDS,
    cli_then_verify,
    connect_iface,
    run_cli,
    verify_state,
)

pytestmark = [pytest.mark.simradio, pytest.mark.smokevirt]

REBOOT_CLI_TIMEOUT_SECONDS = 90.0


def _channel(iface: TCPInterface, index: int) -> channel_pb2.Channel | None:
    return iface.localNode.getChannelByChannelIndex(index)


def _long_name(iface: TCPInterface) -> str:
    value = iface.getLongName()
    assert value is not None
    return value


def _short_name(iface: TCPInterface) -> str:
    value = iface.getShortName()
    assert value is not None
    return value


def _channel_url_payload(url: str) -> bytes:
    """Decode the protobuf fragment from any supported channel URL shape."""
    fragment = url.rpartition("#")[2]
    assert fragment, f"channel URL has no fragment: {url!r}"
    fragment += "=" * (-len(fragment) % 4)
    return base64.urlsafe_b64decode(fragment)


def _assert_owner(
    iface: TCPInterface,
    expected_long: str,
    expected_short: str | None = None,
) -> None:
    assert _long_name(iface) == expected_long
    if expected_short is not None:
        assert _short_name(iface) == expected_short


def _assert_modem_preset(
    iface: TCPInterface,
    expected: config_pb2.Config.LoRaConfig.ModemPreset.ValueType,
) -> None:
    actual = iface.localNode.localConfig.lora.modem_preset
    assert actual == expected, (
        "modem preset mismatch: "
        f"expected={config_pb2.Config.LoRaConfig.ModemPreset.Name(expected)} "
        f"actual={config_pb2.Config.LoRaConfig.ModemPreset.Name(actual)}"
    )


def _assert_channel_role(
    iface: TCPInterface,
    index: int,
    expected: channel_pb2.Channel.Role.ValueType,
) -> None:
    channel = _channel(iface, index)
    assert channel is not None
    assert channel.role == expected


def _assert_wifi_ssid(iface: TCPInterface, expected: str) -> None:
    assert iface.localNode.localConfig.network.wifi_ssid == expected


def _assert_wifi_psk(iface: TCPInterface, expected: str) -> None:
    assert iface.localNode.localConfig.network.wifi_psk == expected


def _assert_hop_limit(iface: TCPInterface, expected: int) -> None:
    assert iface.localNode.localConfig.lora.hop_limit == expected


def _assert_position_flags(iface: TCPInterface, expected: int) -> None:
    assert iface.localNode.localConfig.position.position_flags == expected


def test_simradio_cli_read_only_and_output_paths(
    firmware_node: SimNode,
    tmp_path: Path,
) -> None:
    """Core display, debug, QR, nodes, and serial-log paths should work."""
    info = run_cli(firmware_node.port, "--info")
    assert info.returncode == 0, info.output
    for heading in (
        "Connected to radio",
        "Owner",
        "My info",
        "Nodes in mesh",
        "Preferences",
        "Channels",
    ):
        assert heading in info.output

    nodes = run_cli(firmware_node.port, "--nodes")
    assert nodes.returncode == 0, nodes.output
    assert "Connected to radio" in nodes.output

    debug = run_cli(firmware_node.port, "--info", "--debug")
    assert debug.returncode == 0, debug.output
    assert re.search(r"^DEBUG ", debug.output, re.MULTILINE), debug.output

    serial_log = tmp_path / "serial.log"
    logged = run_cli(
        firmware_node.port,
        "--info",
        "--seriallog",
        str(serial_log),
    )
    assert logged.returncode == 0, logged.output
    assert serial_log.is_file()

    qr = run_cli(firmware_node.port, "--qr")
    assert qr.returncode == 0, qr.output
    assert len(qr.output) > 500

    device_test = run_cli(firmware_node.port, "--test")
    assert device_test.returncode != 0, device_test.output
    assert re.search(r"(?i)at least two devices", device_test.output)


def test_simradio_cli_invalid_settings_report_choices(
    firmware_node: SimNode,
) -> None:
    """Invalid schema paths should report choices without mutating firmware."""
    cases = (
        ("--get", "not_a_setting"),
        ("--set", "not_a_setting", "value"),
        ("--ch-set", "not_a_setting", "value", "--ch-index", "0"),
    )
    for arguments in cases:
        result = run_cli(firmware_node.port, *arguments)
        assert result.returncode == 0, result.output
        assert "Choices" in result.output


def test_simradio_cli_primary_channel_guards(
    firmware_node: SimNode,
) -> None:
    """Destructive primary-channel operations must remain rejected."""
    cases = (
        ("--ch-del", "--ch-index", "0"),
        ("--ch-disable", "--ch-index", "0"),
        ("--ch-enable", "--ch-index", "0"),
        ("--ch-del",),
    )
    for arguments in cases:
        result = run_cli(firmware_node.port, *arguments)
        assert result.returncode != 0, result.output
        assert re.search(r"(?i)primary|ch-index|need to specify", result.output)


def test_simradio_cli_owner_and_fixed_position_lifecycle(
    firmware_node: SimNode,
) -> None:
    """Owner and fixed-position mutations should persist and be removable."""
    cli_then_verify(
        firmware_node.port,
        ("--set-owner", "Simradio Alice", "--set-owner-short", "SIM"),
        lambda iface: _assert_owner(iface, "Simradio Alice", "SIM"),
    )

    def _assert_position(iface: TCPInterface) -> None:
        node_info = iface.getMyNodeInfo()
        assert node_info is not None
        position = node_info.get("position", {}) or {}
        assert abs(float(position.get("latitude", 0)) - 32.7767) < 0.001
        assert abs(float(position.get("longitude", 0)) + 96.7970) < 0.001
        assert int(position.get("altitude", 0)) == 1337

    cli_then_verify(
        firmware_node.port,
        (
            "--setlat",
            "32.7767",
            "--setlon",
            "-96.7970",
            "--setalt",
            "1337",
        ),
        _assert_position,
    )

    def _assert_position_removed(iface: TCPInterface) -> None:
        node_info = iface.getMyNodeInfo()
        assert node_info is not None
        position = node_info.get("position", {}) or {}
        assert "latitude" not in position or float(position.get("latitude", 0)) == 0
        assert "longitude" not in position or float(position.get("longitude", 0)) == 0

    cli_then_verify(
        firmware_node.port,
        ("--remove-position",),
        _assert_position_removed,
    )


def test_simradio_cli_ham_and_position_flags(firmware_node: SimNode) -> None:
    """Ham mode and symbolic position flags should persist together."""

    def _assert_ham_mode(iface: TCPInterface) -> None:
        _assert_owner(iface, "KI5SIM")
        user = iface.getMyUser()
        assert user is not None
        assert user.get("isLicensed") is True
        primary = _channel(iface, 0)
        assert primary is not None
        assert primary.settings.psk in (b"\x00", b"")

    cli_then_verify(
        firmware_node.port,
        ("--set-ham", "KI5SIM"),
        _assert_ham_mode,
    )

    expected_flags = (
        int(config_pb2.Config.PositionConfig.PositionFlags.ALTITUDE)
        | int(config_pb2.Config.PositionConfig.PositionFlags.ALTITUDE_MSL)
        | int(config_pb2.Config.PositionConfig.PositionFlags.HEADING)
    )
    cli_then_verify(
        firmware_node.port,
        ("--pos-fields", "ALTITUDE", "ALTITUDE_MSL", "HEADING"),
        lambda iface: _assert_position_flags(iface, expected_flags),
    )


def test_simradio_cli_modem_preset_matrix(firmware_node: SimNode) -> None:
    """Common 2.8 shorthands and the schema-driven option should persist."""
    cases = (
        ("--ch-longmod", config_pb2.Config.LoRaConfig.ModemPreset.LONG_MODERATE),
        (
            "--ch-longmoderate",
            config_pb2.Config.LoRaConfig.ModemPreset.LONG_MODERATE,
        ),
        ("--ch-longfast", config_pb2.Config.LoRaConfig.ModemPreset.LONG_FAST),
        ("--ch-longturbo", config_pb2.Config.LoRaConfig.ModemPreset.LONG_TURBO),
        ("--ch-medslow", config_pb2.Config.LoRaConfig.ModemPreset.MEDIUM_SLOW),
        ("--ch-medfast", config_pb2.Config.LoRaConfig.ModemPreset.MEDIUM_FAST),
        ("--ch-shortslow", config_pb2.Config.LoRaConfig.ModemPreset.SHORT_SLOW),
        ("--ch-shortfast", config_pb2.Config.LoRaConfig.ModemPreset.SHORT_FAST),
        ("--ch-shortturbo", config_pb2.Config.LoRaConfig.ModemPreset.SHORT_TURBO),
    )
    for flag, expected in cases:
        cli_then_verify(
            firmware_node.port,
            (flag,),
            partial(_assert_modem_preset, expected=expected),
        )

    cli_then_verify(
        firmware_node.port,
        ("--ch-preset", "medium-turbo"),
        lambda iface: _assert_modem_preset(
            iface,
            config_pb2.Config.LoRaConfig.ModemPreset.MEDIUM_TURBO,
        ),
    )


def test_simradio_cli_channel_lifecycle(firmware_node: SimNode) -> None:
    """Add, mutate, disable, enable, and delete a secondary channel."""

    def _assert_added(iface: TCPInterface) -> None:
        channel = _channel(iface, 1)
        assert channel is not None
        assert channel.role == channel_pb2.Channel.Role.SECONDARY
        assert channel.settings.name == "smoketest"

    cli_then_verify(
        firmware_node.port,
        ("--ch-add", "smoketest"),
        _assert_added,
    )

    def _assert_flags_disabled(iface: TCPInterface) -> None:
        channel = _channel(iface, 1)
        assert channel is not None
        assert channel.settings.downlink_enabled is False
        assert channel.settings.uplink_enabled is False

    cli_then_verify(
        firmware_node.port,
        (
            "--ch-set",
            "downlink_enabled",
            "false",
            "--ch-set",
            "uplink_enabled",
            "false",
            "--ch-index",
            "1",
        ),
        _assert_flags_disabled,
    )

    cli_then_verify(
        firmware_node.port,
        ("--ch-disable", "--ch-index", "1"),
        lambda iface: _assert_channel_role(
            iface, 1, channel_pb2.Channel.Role.DISABLED
        ),
    )
    cli_then_verify(
        firmware_node.port,
        ("--ch-enable", "--ch-index", "1"),
        lambda iface: _assert_channel_role(
            iface, 1, channel_pb2.Channel.Role.SECONDARY
        ),
    )
    cli_then_verify(
        firmware_node.port,
        ("--ch-del", "--ch-index", "1"),
        lambda iface: _assert_channel_role(
            iface, 1, channel_pb2.Channel.Role.DISABLED
        ),
    )


def test_simradio_cli_channel_url_paths(firmware_node: SimNode) -> None:
    """A valid channel URL should round-trip; malformed payloads should fail."""
    expected_url = "https://www.meshtastic.org/d/#CgUYAyIBAQ"
    expected_payload = _channel_url_payload(expected_url)

    def _assert_url(iface: TCPInterface) -> None:
        actual_url = iface.localNode.getURL()
        assert "meshtastic.org" in actual_url
        assert _channel_url_payload(actual_url).startswith(expected_payload)

    cli_then_verify(
        firmware_node.port,
        ("--seturl", expected_url),
        _assert_url,
        cli_timeout=REBOOT_CLI_TIMEOUT_SECONDS,
    )

    invalid_url = (
        "https://www.meshtastic.org/c/#"
        "GAMiENTxuzogKQdZ8Lz_q89Oab8qB0RlZmF1bHQ="
    )
    invalid = run_cli(firmware_node.port, "--seturl", invalid_url)
    assert invalid.returncode != 0, invalid.output
    assert re.search(r"(?i)warning|no settings|invalid|error", invalid.output)


def test_simradio_cli_yaml_export_restore_round_trip(
    firmware_node: SimNode,
    tmp_path: Path,
) -> None:
    """Exported YAML should restore owner and fixed-position configuration."""
    initial_config = tmp_path / "initial.yaml"
    initial_config.write_text(
        yaml.safe_dump(
            {
                "owner": "Profile Before",
                "owner_short": "PBF",
                "location": {"lat": 1.25, "lon": -2.5, "alt": 10},
                "config": {"position": {"fixed_position": True}},
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    def _assert_profile(iface: TCPInterface) -> None:
        _assert_owner(iface, "Profile Before", "PBF")
        assert iface.localNode.localConfig.position.fixed_position is True

    def _assert_mutated(iface: TCPInterface) -> None:
        _assert_owner(iface, "Profile Mutated")
        assert iface.localNode.localConfig.position.fixed_position is False

    cli_then_verify(
        firmware_node.port,
        ("--configure", str(initial_config)),
        _assert_profile,
        cli_timeout=REBOOT_CLI_TIMEOUT_SECONDS,
    )

    exported = tmp_path / "exported.yaml"
    export_result = run_cli(
        firmware_node.port,
        "--export-config",
        str(exported),
        timeout=REBOOT_CLI_TIMEOUT_SECONDS,
    )
    assert export_result.returncode == 0, export_result.output
    parsed = yaml.safe_load(exported.read_text(encoding="utf-8"))
    assert isinstance(parsed, dict)
    assert parsed.get("owner") == "Profile Before"
    assert parsed.get("owner_short") == "PBF"
    exported_config = parsed.get("config")
    assert isinstance(exported_config, dict)
    exported_position = exported_config.get("position")
    assert isinstance(exported_position, dict)
    assert exported_position.get("fixed_position") is True

    cli_then_verify(
        firmware_node.port,
        (
            "--set-owner",
            "Profile Mutated",
            "--set",
            "position.fixed_position",
            "false",
        ),
        _assert_mutated,
    )
    cli_then_verify(
        firmware_node.port,
        ("--configure", str(exported)),
        _assert_profile,
        cli_timeout=REBOOT_CLI_TIMEOUT_SECONDS,
    )


def test_simradio_cli_schema_get_set_paths(firmware_node: SimNode) -> None:
    """Known dynamic config paths should be writable and readable."""
    cli_then_verify(
        firmware_node.port,
        ("--set", "network.wifi_ssid", "simradio-ssid"),
        lambda iface: _assert_wifi_ssid(iface, "simradio-ssid"),
    )
    cli_then_verify(
        firmware_node.port,
        ("--set", "network.wifi_psk", "temp1234"),
        lambda iface: _assert_wifi_psk(iface, "temp1234"),
    )
    cli_then_verify(
        firmware_node.port,
        ("--set", "lora.hop_limit", "5"),
        lambda iface: _assert_hop_limit(iface, 5),
    )
    for field in (
        "network.wifi_ssid",
        "lora.hop_limit",
        "position.position_broadcast_secs",
    ):
        result = run_cli(firmware_node.port, "--get", field)
        assert result.returncode == 0, result.output
        assert field.rsplit(".", maxsplit=1)[-1] in result.output.casefold()


def test_simradio_cli_factory_reset_isolated(firmware_node: SimNode) -> None:
    """--factory-reset should clear a previously set value on an isolated node."""
    # Change a known setting first.
    set_result = run_cli(
        firmware_node.port,
        "--set-owner",
        "BeforeReset",
        timeout=30.0,
    )
    assert set_result.returncode == 0, set_result.output
    time.sleep(PAUSE_AFTER_CLI_SECONDS)

    def _assert_before_reset(iface: TCPInterface) -> None:
        assert iface.getLongName() == "BeforeReset"

    verify_state(firmware_node.port, _assert_before_reset)

    # Run the real factory reset with zero automatic retries.
    reset_result = run_cli(
        firmware_node.port,
        "--factory-reset",
        retries=0,
        timeout=REBOOT_CLI_TIMEOUT_SECONDS,
    )
    assert reset_result.returncode == 0, reset_result.output
    # The output must not follow the unknown-setting "Choices" path.
    assert "Choices are" not in reset_result.output

    # Wait for the node to come back after reboot.
    iface = connect_iface(firmware_node.port)
    iface.close()

    def _assert_default_owner(iface: TCPInterface) -> None:
        name = iface.getLongName()
        assert name is not None
        assert name != "BeforeReset", f"owner was not reset, got {name!r}"

    verify_state(firmware_node.port, _assert_default_owner)


def test_simradio_fixture_supports_explicit_connection(firmware_node: SimNode) -> None:
    """Library smoke tests may explicitly claim and release the firmware client."""
    assert firmware_node.iface is None
    iface = firmware_node.connect()
    try:
        assert iface.localNode is not None
        assert firmware_node.node_num > 0
        assert isinstance(iface.getMyNodeInfo(), dict)
        iface.showNodes()
    finally:
        firmware_node.disconnect()
