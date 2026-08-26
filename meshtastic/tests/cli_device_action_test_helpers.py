"""Shared builders for focused connected device-action tests."""

import argparse
from typing import Any, cast
from unittest.mock import MagicMock, create_autospec

from meshtastic.cli.context import ActionOutcome, CliContext, CliExit
from meshtastic.cli.device_actions import DeviceActionHooks
from meshtastic.mesh_interface import MeshInterface
from meshtastic.node import Node


def device_action_hooks(
    prints: list[str] | None = None,
    exits: list[tuple[str, int]] | None = None,
    **overrides: Any,
) -> DeviceActionHooks:
    """Build device-action hooks that capture CLI output and termination.

    Keyword overrides replace individual hook seams, mirroring the
    ``_hooks(**overrides)`` fixture idiom used by the device-action suites.
    """

    def fake_exit(message: str, code: int = 0) -> None:
        if exits is not None:
            exits.append((message, code))
        raise SystemExit(code)

    values: dict[str, Any] = {
        "cli_exit": cast(CliExit, fake_exit),
        "cli_print": (prints.append if prints is not None else (lambda _text: None)),
        "set_pref": MagicMock(return_value=True),
        "is_local_destination": MagicMock(return_value=True),
        "send_local_factory_reset_and_wait": MagicMock(),
        "post_factory_reset_ready_probe": MagicMock(),
        "handle_ota_update": MagicMock(),
        "build_lockdown_auth": MagicMock(),
        "read_lockdown_passphrase_file": MagicMock(return_value=b"x"),
        "send_lockdown_auth": MagicMock(),
        "validate_lockdown_passphrase": MagicMock(return_value=b"x"),
        "build_key_verification_admin": MagicMock(),
        "send_key_verification": MagicMock(),
    }
    values.update(overrides)
    return DeviceActionHooks(**values)


def device_action_context(interface: MagicMock, args: dict[str, object]) -> CliContext:
    """Build a connected CLI context carrying device-action overrides."""
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
        "get_ui_config": False,
        "store_ui_config": None,
    }
    defaults.update(args)
    return CliContext(
        interface=cast(MeshInterface, interface),
        args=argparse.Namespace(**defaults),
        get_node_kwargs={},
        outcome=ActionOutcome(),
    )


def device_interface_mock() -> MagicMock:
    """Build autospecced interface and node doubles for CLI dispatch tests."""
    interface = cast(MagicMock, create_autospec(MeshInterface, instance=True))
    interface.getNode.return_value = create_autospec(Node, instance=True)
    return interface
