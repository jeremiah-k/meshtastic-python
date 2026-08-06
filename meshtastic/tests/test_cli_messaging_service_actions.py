"""Focused behavioral tests for connected messaging/service CLI actions."""

from __future__ import annotations

import argparse
from typing import Any
from unittest.mock import MagicMock, create_autospec

import pytest

from meshtastic.cli.context import ActionOutcome, CliContext
from meshtastic.mesh_interface import MeshInterface
from meshtastic.cli.messaging_service_actions import (
    MessagingServiceHooks,
    _handle_content_reads,
    _handle_information_actions,
    _handle_long_running_services,
    _handle_messaging_actions,
)


def _args(**overrides: Any) -> argparse.Namespace:
    values: dict[str, Any] = {
        "sendtext": None,
        "private": False,
        "dest": "!00000002",
        "traceroute": None,
        "request_telemetry": None,
        "request_position": False,
        "gpio_wrb": None,
        "gpio_rd": None,
        "gpio_watch": None,
        "info": False,
        "get": None,
        "nodes": False,
        "show_fields": None,
        "slog": None,
        "power_stress": False,
        "listen": False,
        "tunnel": False,
        "tunnel_net": None,
    }
    values.update(overrides)
    return argparse.Namespace(**values)


def _interface_double() -> MagicMock:
    """Return an interface double constrained to the MeshInterface method surface."""
    return create_autospec(MeshInterface, instance=True)


def _context(interface: Any, **arg_overrides: Any) -> CliContext:
    return CliContext(
        interface=interface,
        args=_args(**arg_overrides),
        get_node_kwargs={"timeout": 5.0},
        outcome=ActionOutcome(),
    )


def _cli_exit(_message: str, return_value: int = 1) -> None:
    raise SystemExit(return_value)


def _hooks(**overrides: Any) -> MessagingServiceHooks:
    values: dict[str, Any] = {
        "cli_exit": _cli_exit,
        "cli_print": MagicMock(),
        "get_channel_index": lambda: 2,
        "check_channel": MagicMock(return_value=True),
        "remote_hardware_client": MagicMock(),
        "get_pref": MagicMock(return_value=True),
        "validate_cli_show_fields": MagicMock(),
        "newer_version": MagicMock(return_value=None),
        "install_upgrade_hint": "pipx upgrade mtjk",
        "powermon_available": MagicMock(return_value=True),
        "powermon_error": MagicMock(return_value=None),
        "log_set_factory": MagicMock(),
        "power_stress_factory": MagicMock(),
        "get_meter": MagicMock(return_value=object()),
        "platform_system": MagicMock(return_value="Linux"),
    }
    values.update(overrides)
    return MessagingServiceHooks(**values)


@pytest.mark.unit
def test_messaging_actions_send_traceroute_with_selected_channel() -> None:
    """Traceroute should use the selected channel and local hop limit."""
    interface = _interface_double()
    interface.localNode = MagicMock()
    interface.localNode.localConfig.lora.hop_limit = 5
    context = _context(interface, traceroute="!00000003")
    hooks = _hooks()

    _handle_messaging_actions(context, hooks)

    interface.sendTraceRoute.assert_called_once_with(
        "!00000003", 5, channelIndex=2
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    ("requested", "expected"),
    [
        ("environment", "environment_metrics"),
        ("air_quality", "air_quality_metrics"),
        ("local_stats", "local_stats"),
        ("unknown", "device_metrics"),
    ],
)
def test_messaging_actions_maps_telemetry_types(
    requested: str, expected: str
) -> None:
    """Telemetry request aliases should map to the historical metric names."""
    interface = _interface_double()
    context = _context(interface, request_telemetry=requested)
    hooks = _hooks()

    _handle_messaging_actions(context, hooks)

    interface.sendTelemetry.assert_called_once_with(
        destinationId="!00000002",
        wantResponse=True,
        channelIndex=2,
        telemetryType=expected,
    )


@pytest.mark.unit
def test_messaging_actions_reject_broadcast_response_requests() -> None:
    """Telemetry and position response requests require a concrete destination."""
    interface = _interface_double()
    context = _context(interface, dest="^all", request_telemetry="device")
    hooks = _hooks()

    with pytest.raises(SystemExit):
        _handle_messaging_actions(context, hooks)

    interface.sendTelemetry.assert_not_called()


@pytest.mark.unit
def test_messaging_actions_writes_gpio_bitmask() -> None:
    """GPIO writes should combine bit/value pairs before sending one request."""
    interface = _interface_double()
    client = MagicMock()
    context = _context(interface, gpio_wrb=[("4", "1"), ("7", "1")])
    hooks = _hooks(remote_hardware_client=MagicMock(return_value=client))

    _handle_messaging_actions(context, hooks)

    client.writeGPIOs.assert_called_once_with("!00000002", 0x90, 0x90)
    assert context.outcome.close_now is True


@pytest.mark.unit
def test_information_actions_remote_nodes_stops_dispatch(capsys: pytest.CaptureFixture[str]) -> None:
    """Remote node-list requests should retain the historical early-return behavior."""
    interface = _interface_double()
    context = _context(interface, nodes=True)
    cli_print = MagicMock()
    hooks = _hooks(cli_print=cli_print)

    _handle_information_actions(context, hooks)

    assert context.outcome.stop_processing is True
    interface.showNodes.assert_not_called()
    cli_print.assert_called_once_with(
        "Showing node list of a remote node is not supported."
    )


@pytest.mark.unit
def test_information_actions_validates_selected_node_fields() -> None:
    """Local node listing should validate requested fields before rendering."""
    interface = _interface_double()
    context = _context(interface, dest="^all", nodes=True, show_fields=["user.id"])
    validate = MagicMock()
    hooks = _hooks(validate_cli_show_fields=validate)

    _handle_information_actions(context, hooks)

    validate.assert_called_once_with(interface, ["user.id"])
    interface.showNodes.assert_called_once_with(True, ["user.id"])


@pytest.mark.unit
def test_long_running_services_registers_log_cleanup_and_runs_stress() -> None:
    """Slog lifetime should be retained explicitly while power stress still closes."""
    interface = _interface_double()
    log_set = MagicMock()
    stress = MagicMock()
    context = _context(interface, slog="default", power_stress=True)
    hooks = _hooks(
        log_set_factory=MagicMock(return_value=log_set),
        power_stress_factory=MagicMock(return_value=stress),
    )

    _handle_long_running_services(context, hooks)

    stress.run.assert_called_once_with()
    assert context.outcome.close_now is True
    assert context.outcome.cleanup_callbacks == [log_set.close]


@pytest.mark.unit
def test_long_running_services_listen_overrides_close_request() -> None:
    """Listen mode should keep the interface open after earlier close requests."""
    interface = _interface_double()
    context = _context(interface, listen=True)
    context.outcome.close_now = True

    _handle_long_running_services(context, _hooks())

    assert context.outcome.close_now is False


@pytest.mark.unit
def test_sendtext_does_not_force_ack_wait_without_ack_flag() -> None:
    """Text sends should leave ACK waiting under the explicit ``--ack`` option."""
    interface = _interface_double()
    node = MagicMock()
    interface.getNode.return_value = node
    context = _context(interface, sendtext="hello")

    _handle_messaging_actions(context, _hooks())

    assert context.outcome.close_now is True
    assert context.outcome.wait_for_ack_nak is False
    interface.sendText.assert_called_once()


@pytest.mark.unit
def test_gpio_read_rejects_invalid_hex_mask() -> None:
    """Malformed GPIO masks should exit cleanly rather than raising ValueError."""
    interface = _interface_double()
    context = _context(interface, gpio_rd="zz")
    client_factory = MagicMock()
    hooks = _hooks(remote_hardware_client=client_factory)

    with pytest.raises(SystemExit) as exc_info:
        _handle_messaging_actions(context, hooks)

    assert exc_info.value.code == 1
    client_factory.return_value.readGPIOs.assert_not_called()


@pytest.mark.unit
def test_information_get_accumulates_success_across_preferences() -> None:
    """Any successful preference lookup should retain the completion message."""
    interface = _interface_double()
    context = _context(interface, get=[["lora"], ["missing"]])
    get_pref = MagicMock(side_effect=[True, False])
    cli_print = MagicMock()

    _handle_information_actions(
        context,
        _hooks(get_pref=get_pref, cli_print=cli_print),
    )

    cli_print.assert_any_call("Completed getting preferences")


@pytest.mark.unit
def test_content_reads_escape_terminal_control_sequences(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Remote text content must not inject terminal control sequences."""
    interface = _interface_double()
    node = MagicMock()
    node.get_canned_message.return_value = "hello\x1b[31mred\x07"
    node.get_ringtone.return_value = "tone\nnext"
    interface.getNode.return_value = node
    context = _context(interface, get_canned_message=True, get_ringtone=True)

    _handle_content_reads(context)

    output = capsys.readouterr().out
    assert "\x1b" not in output
    assert "\x07" not in output
    assert r"\x1b[31m" in output
    assert r"\x07" in output
    assert r"tone\nnext" in output


@pytest.mark.unit
@pytest.mark.parametrize("raw_mask", ["-1", "10000000000000000"])
def test_gpio_read_rejects_masks_outside_uint64(raw_mask: str) -> None:
    """GPIO read masks must fit the uint64 protobuf field before transmission."""
    interface = _interface_double()
    client = MagicMock()
    context = _context(interface, gpio_rd=raw_mask)
    hooks = _hooks(remote_hardware_client=MagicMock(return_value=client))

    with pytest.raises(SystemExit):
        _handle_messaging_actions(context, hooks)

    client.readGPIOs.assert_not_called()


@pytest.mark.unit
def test_gpio_write_rejects_bit_index_outside_uint64() -> None:
    """GPIO write bit indices must not exceed the uint64 hardware mask."""
    interface = _interface_double()
    client = MagicMock()
    context = _context(interface, gpio_wrb=[("64", "1")])
    hooks = _hooks(remote_hardware_client=MagicMock(return_value=client))

    with pytest.raises(SystemExit):
        _handle_messaging_actions(context, hooks)

    client.writeGPIOs.assert_not_called()
