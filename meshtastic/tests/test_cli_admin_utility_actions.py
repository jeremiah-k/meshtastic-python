"""Tests for the firmware 2.5-2.8 admin utility CLI actions."""

import argparse
from typing import cast
from unittest.mock import MagicMock

import pytest

import meshtastic.cli.device_actions as device_actions
from meshtastic._interface_errors import MeshInterfaceError
from meshtastic.cli.context import ActionOutcome, CliContext, CliExit
from meshtastic.cli.device_actions import DeviceActionHooks
from meshtastic.mesh_interface import MeshInterface
from meshtastic.node import _backup_location_value
from meshtastic.protobuf import admin_pb2, connection_status_pb2


def _hooks(
    prints: list[str] | None = None,
    exits: list[tuple[str, int]] | None = None,
) -> DeviceActionHooks:
    def fake_exit(message: str, code: int = 0) -> None:
        if exits is not None:
            exits.append((message, code))
        raise SystemExit(code)

    return DeviceActionHooks(
        cli_exit=cast(CliExit, fake_exit),
        cli_print=(prints.append if prints is not None else (lambda _s: None)),
        set_pref=MagicMock(return_value=True),
        is_local_destination=MagicMock(return_value=True),
        send_local_factory_reset_and_wait=MagicMock(),
        post_factory_reset_ready_probe=MagicMock(),
        handle_ota_update=MagicMock(),
        build_lockdown_auth=MagicMock(),
        read_lockdown_passphrase_file=MagicMock(return_value=b"x"),
        send_lockdown_auth=MagicMock(),
        validate_lockdown_passphrase=MagicMock(return_value=b"x"),
        build_key_verification_admin=MagicMock(),
        send_key_verification=MagicMock(),
    )


def _context(interface: MagicMock, args: dict[str, object]) -> CliContext:
    """Build a connected CLI context carrying the given argument overrides."""
    defaults: dict[str, object] = {
        "dest": "^local",
        "remove_node": None,
        "set_favorite_node": None,
        "remove_favorite_node": None,
        "set_ignored_node": None,
        "remove_ignored_node": None,
        "reset_nodedb": False,
        "backup_preferences": None,
        "restore_preferences": None,
        "remove_backup_preferences": None,
        "toggle_muted_node": None,
        "delete_file": None,
        "send_input_event": None,
        "input_kb_char": None,
        "input_touch_x": 0,
        "input_touch_y": 0,
        "request_connection_status": False,
    }
    defaults.update(args)
    return CliContext(
        interface=cast(MeshInterface, interface),
        args=argparse.Namespace(**defaults),
        get_node_kwargs={},
        outcome=ActionOutcome(),
    )


@pytest.mark.unit
def test_backup_location_value_accepts_names_case_insensitively() -> None:
    """flash/SD names map to BackupLocation enum values."""
    assert _backup_location_value("flash") == admin_pb2.AdminMessage.BackupLocation.FLASH
    assert _backup_location_value("SD") == admin_pb2.AdminMessage.BackupLocation.SD


@pytest.mark.unit
def test_backup_location_value_rejects_unknown_names() -> None:
    """Unknown locations raise the interface error naming valid choices."""
    with pytest.raises(MeshInterfaceError, match="Unknown backup location"):
        _backup_location_value("usb")


@pytest.mark.unit
def test_preference_backup_actions_call_node_methods() -> None:
    """Each backup flag drives the matching Node method with its location."""
    interface = MagicMock()
    prints: list[str] = []
    context = _context(
        interface,
        {"backup_preferences": "sd"},
    )
    device_actions._handle_admin_utility_actions(context, _hooks(prints))
    interface.getNode.assert_called_once_with("^local", False)
    interface.getNode.return_value.backupPreferences.assert_called_once_with("sd")
    assert any("Backing up preferences (sd)" in line for line in prints)
    assert context.outcome.close_now is True
    assert context.outcome.wait_for_ack_nak is True


@pytest.mark.unit
def test_restore_and_remove_backup_actions() -> None:
    """Restore/remove flags route through their Node methods."""
    interface = MagicMock()
    device_actions._handle_admin_utility_actions(
        _context(interface, {"restore_preferences": "flash"}), _hooks()
    )
    interface.getNode.return_value.restorePreferences.assert_called_once_with("flash")

    interface2 = MagicMock()
    device_actions._handle_admin_utility_actions(
        _context(interface2, {"remove_backup_preferences": "sd"}), _hooks()
    )
    interface2.getNode.return_value.removeBackupPreferences.assert_called_once_with(
        "sd"
    )


@pytest.mark.unit
def test_delete_file_and_input_event_actions() -> None:
    """File deletion and input events forward their arguments verbatim."""
    interface = MagicMock()
    device_actions._handle_admin_utility_actions(
        _context(interface, {"delete_file": "/fs/old.cfg"}), _hooks()
    )
    interface.getNode.return_value.deleteFile.assert_called_once_with("/fs/old.cfg")

    interface2 = MagicMock()
    device_actions._handle_admin_utility_actions(
        _context(
            interface2,
            {"send_input_event": 212, "input_kb_char": "x", "input_touch_x": 5},
        ),
        _hooks(),
    )
    interface2.getNode.return_value.sendInputEvent.assert_called_once_with(
        212, kb_char=ord("x"), touch_x=5, touch_y=0
    )


@pytest.mark.unit
def test_toggle_muted_node_uses_nodedb_action_path() -> None:
    """ToggleMutedNode rides the shared node-database action table."""
    interface = MagicMock()
    context = _context(interface, {"toggle_muted_node": "!abcd1234"})
    device_actions._handle_node_database_actions(context)
    interface.getNode.return_value.toggleMutedNode.assert_called_once_with("!abcd1234")
    assert context.outcome.close_now is True

@pytest.mark.unit
def test_connection_status_prints_each_transport() -> None:
    """The status printer reports present transports with their details."""
    status = connection_status_pb2.DeviceConnectionStatus()
    status.wifi.status.is_connected = True
    status.wifi.status.ip_address = 0xC0A8012A
    status.wifi.status.is_mqtt_connected = True
    status.wifi.ssid = "meshnet"
    status.serial.is_connected = True
    status.serial.baud = 115200

    prints: list[str] = []
    device_actions._print_device_connection_status(status, _hooks(prints))

    joined = "\n".join(prints)
    assert "wifi: connected (192.168.1.42, mqtt, ssid meshnet)" in joined
    assert "serial: connected (baud 115200)" in joined
    assert "bluetooth" not in joined



@pytest.mark.unit
def test_connection_status_missing_response_terminates() -> None:
    """A missing status response exits with the firmware-version hint."""
    interface = MagicMock()
    interface.getNode.return_value.requestDeviceConnectionStatus.return_value = None
    exits: list[tuple[str, int]] = []
    with pytest.raises(SystemExit):
        device_actions._handle_admin_utility_actions(
            _context(interface, {"request_connection_status": True}), _hooks(exits=exits)
        )
    assert exits and exits[0][1] == 1
    assert "firmware 2.5+" in exits[0][0]


@pytest.mark.unit
def test_ip4_to_str_formats_dotted_quad() -> None:
    """Packed IPv4 addresses render as dotted quads."""
    assert device_actions._ip4_to_str(0xC0A8012A) == "192.168.1.42"
    assert device_actions._ip4_to_str(0x7F000001) == "127.0.0.1"
