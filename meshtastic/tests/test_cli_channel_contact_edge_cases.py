"""Edge-case tests for internal channel/contact CLI action contracts."""

from __future__ import annotations

import argparse
from contextlib import nullcontext
from typing import Any, cast
from unittest.mock import MagicMock

import pytest

from meshtastic.cli import channel_contact_actions as actions
from meshtastic.cli.channel_contact_actions import ChannelContactHooks
from meshtastic.cli.context import ActionOutcome, CliContext, CliExit
from meshtastic.mesh_interface import MeshInterface


def _cli_exit(_message: str, return_value: int = 1) -> None:
    raise SystemExit(return_value)


def _hooks(**overrides: Any) -> ChannelContactHooks:
    values: dict[str, Any] = {
        "cli_exit": cast(CliExit, _cli_exit),
        "cli_print": MagicMock(),
        "get_channel_index": MagicMock(return_value=1),
        "set_channel_index": MagicMock(),
        "resolve_pref": MagicMock(return_value=True),
        "set_pref": MagicMock(return_value=True),
        "fatal_preference_value_errors": lambda: nullcontext(),
        "preference_value_error": ValueError,
        "print_channel_field_choices": MagicMock(),
        "is_local_destination": MagicMock(return_value=True),
        "modem_preset_shorthands": (),
        "qr_create": None,
    }
    values.update(overrides)
    return ChannelContactHooks(**values)


def _args(**overrides: Any) -> argparse.Namespace:
    values: dict[str, Any] = {
        "dest": "^all",
        "add_contact": None,
        "ch_add": None,
        "ch_del": False,
        "ch_set": None,
        "ch_enable": False,
        "ch_disable": False,
        "ch_set_url": None,
        "ch_add_url": None,
        "ch_preset": None,
        "show_region_presets": False,
        "qr": False,
        "qr_all": False,
        "qr_contact": None,
    }
    values.update(overrides)
    return argparse.Namespace(**values)


def _context(interface: Any, **overrides: Any) -> CliContext:
    return CliContext(
        interface=cast(MeshInterface, interface),
        args=_args(**overrides),
        get_node_kwargs={},
        outcome=ActionOutcome(),
    )


@pytest.mark.unit
def test_invalid_numeric_modem_preset_is_rejected() -> None:
    """Unknown numeric presets should fail instead of leaking invalid protobuf values."""
    with pytest.raises(SystemExit):
        actions._resolve_requested_modem_preset(
            _context(MagicMock(), ch_preset=999999), _hooks()
        )


@pytest.mark.unit
@pytest.mark.parametrize("case", ["missing", "negative", "out_of_range"])
def test_channel_update_rejects_unavailable_or_invalid_channel(case: str) -> None:
    """Channel mutation must fail before dereferencing unavailable channel state."""
    interface = MagicMock()
    node = interface.getNode.return_value
    hooks = _hooks()
    if case == "missing":
        node.channels = None
    elif case == "negative":
        node.channels = [MagicMock()]
        hooks = _hooks(get_channel_index=MagicMock(return_value=-1))
    else:
        node.channels = [MagicMock()]
        hooks = _hooks(get_channel_index=MagicMock(return_value=4))

    with pytest.raises(SystemExit):
        actions._handle_channel_update(_context(interface, ch_enable=True), hooks)
    node.writeChannel.assert_not_called()


@pytest.mark.unit
def test_channel_update_rejects_failed_preference_write() -> None:
    """A resolved setting whose value cannot be applied must not write the channel."""
    interface = MagicMock()
    channel = MagicMock()
    interface.getNode.return_value.channels = [MagicMock(), channel]
    hooks = _hooks(set_pref=MagicMock(return_value=False))

    with pytest.raises(SystemExit):
        actions._handle_channel_update(
            _context(interface, ch_set=[["name", "mesh"]]), hooks
        )
    interface.getNode.return_value.writeChannel.assert_not_called()


@pytest.mark.unit
def test_add_channel_url_uses_add_only_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    """--ch-add-url should preserve existing channels by selecting addOnly mode."""
    interface = MagicMock()
    context = _context(interface, ch_add_url="https://example.invalid/#abc")
    monkeypatch.setattr(actions, "_handle_channel_add", MagicMock())
    monkeypatch.setattr(actions, "_handle_channel_delete", MagicMock())
    monkeypatch.setattr(
        actions, "_resolve_requested_modem_preset", MagicMock(return_value=None)
    )
    monkeypatch.setattr(actions, "_handle_channel_update", MagicMock())

    actions._handle_channel_mutations(context, _hooks())

    interface.getNode.return_value.setURL.assert_called_once_with(
        "https://example.invalid/#abc", addOnly=True
    )
    assert context.outcome.close_now is True


@pytest.mark.unit
def test_enum_name_or_fallback_handles_known_and_unknown_values() -> None:
    """Region/preset display should remain stable across future enum values."""
    enum_wrapper = MagicMock()
    enum_wrapper.Name.side_effect = ["KNOWN", ValueError("unknown")]

    assert actions._enum_name_or_fallback(enum_wrapper, 1, "VALUE_") == "KNOWN"
    assert actions._enum_name_or_fallback(enum_wrapper, 99, "VALUE_") == "VALUE_99"


@pytest.mark.unit
def test_region_display_uses_numeric_fallbacks_for_future_firmware_values() -> None:
    """Unknown region and preset values should still produce readable metadata output."""
    interface = MagicMock()
    interface.regionPresets = {
        999: argparse.Namespace(presets=[998], default_preset=997, licensed_only=True)
    }
    cli_print = MagicMock()

    actions._handle_region_preset_display(
        _context(interface, show_region_presets=True), _hooks(cli_print=cli_print)
    )

    rendered = str(cli_print.call_args)
    assert "REGION_999" in rendered
    assert "PRESET_998" in rendered
    assert "PRESET_997" in rendered
    assert "licensed-only" in rendered


@pytest.mark.unit
def test_qr_without_optional_dependency_prints_install_guidance() -> None:
    """Missing pyqrcode should still emit the URL and an actionable installation hint."""
    cli_print = MagicMock()
    actions._print_qr(
        "https://example.invalid/#abc",
        description="Primary channel URL",
        qr_create=None,
        cli_print=cli_print,
    )

    assert cli_print.call_count == 2
    cli_print.assert_any_call("Install pyqrcode to view a QR code printed to terminal.")


@pytest.mark.unit
def test_invalid_named_modem_preset_is_rejected() -> None:
    """Unknown preset names should fail through the same diagnostic path as bad integers."""
    with pytest.raises(SystemExit):
        actions._resolve_requested_modem_preset(
            _context(MagicMock(), ch_preset="NOT_A_PRESET"), _hooks()
        )
