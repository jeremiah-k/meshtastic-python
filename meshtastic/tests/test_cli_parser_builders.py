"""Focused tests for CLI argument-group builders."""

import argparse
from collections.abc import Callable

import pytest

import meshtastic.__main__ as cli_entrypoint
from meshtastic.cli import parser as parser_module
from meshtastic.cli.parser import (
    addChannelConfigArgs,
    addConfigArgs,
    addConnectionArgs,
    addImportExportArgs,
    addLocalActionArgs,
    addPositionConfigArgs,
    addRemoteActionArgs,
    addRemoteAdminArgs,
    addSelectionArgs,
)


def _parser() -> argparse.ArgumentParser:
    return argparse.ArgumentParser(add_help=False)


@pytest.mark.unit
@pytest.mark.parametrize(
    "builder",
    [
        addConnectionArgs,
        addSelectionArgs,
        addImportExportArgs,
        addConfigArgs,
        addChannelConfigArgs,
        addPositionConfigArgs,
        addLocalActionArgs,
        addRemoteActionArgs,
        addRemoteAdminArgs,
    ],
)
def test_argument_builder_returns_same_parser(
    builder: Callable[[argparse.ArgumentParser], argparse.ArgumentParser],
) -> None:
    parser = _parser()
    assert builder(parser) is parser


@pytest.mark.unit
def test_config_builder_parses_dynamic_modem_preset() -> None:
    parser = addConfigArgs(_parser())
    args = parser.parse_args(["--ch-preset", "medium-turbo"])
    assert args.ch_preset == "MEDIUM_TURBO"


@pytest.mark.unit
def test_local_action_builder_keeps_lockdown_actions_mutually_exclusive() -> None:
    parser = addLocalActionArgs(_parser())
    with pytest.raises(SystemExit):
        parser.parse_args(["--lockdown-unlock", "--lockdown-disable"])


@pytest.mark.unit
@pytest.mark.parametrize(
    ("option", "value"),
    [
        ("--send-input-event", "-1"),
        ("--send-input-event", str(1 << 32)),
        ("--input-touch-x", "-1"),
        ("--input-touch-y", str(1 << 32)),
    ],
)
def test_remote_admin_builder_rejects_out_of_range_input_fields(
    option: str, value: str
) -> None:
    """Input-event uint32 fields fail in argparse instead of protobuf assignment."""
    parser = addRemoteAdminArgs(_parser())

    with pytest.raises(SystemExit):
        parser.parse_args([option, value])


@pytest.mark.unit
def test_remote_admin_builder_rejects_relative_delete_path() -> None:
    """A device-side delete path fails before connecting when it is not absolute."""
    parser = addRemoteAdminArgs(_parser())

    with pytest.raises(SystemExit):
        parser.parse_args(["--delete-file", "prefs/config.proto"])


@pytest.mark.unit
def test_remote_admin_builder_accepts_uint32_input_boundaries() -> None:
    """Input-event fields accept the full protobuf uint32 range."""
    args = addRemoteAdminArgs(_parser()).parse_args(
        [
            "--send-input-event",
            "0",
            "--input-touch-x",
            str((1 << 32) - 1),
            "--input-touch-y",
            "0",
        ]
    )

    assert args.send_input_event == 0
    assert args.input_touch_x == (1 << 32) - 1
    assert args.input_touch_y == 0


@pytest.mark.unit
def test_connection_builder_accepts_tcp_host() -> None:
    args = addConnectionArgs(_parser()).parse_args(["--host", "localhost:4403"])
    assert args.host == "localhost:4403"


@pytest.mark.unit
def test_selection_builder_accepts_local_destination() -> None:
    args = addSelectionArgs(_parser()).parse_args(["--dest", "^local"])
    assert args.dest == "^local"


@pytest.mark.unit
@pytest.mark.parametrize(
    "symbol_name",
    [
        "_MODEM_PRESET_SHORTHANDS",
        "addConnectionArgs",
        "addSelectionArgs",
        "addImportExportArgs",
        "addConfigArgs",
        "addChannelConfigArgs",
        "addPositionConfigArgs",
        "addLocalActionArgs",
        "addRemoteActionArgs",
        "addRemoteAdminArgs",
    ],
)
def test_entrypoint_reexports_parser_symbol(symbol_name: str) -> None:
    """Each legacy re-export in __main__ must reference the canonical parser symbol."""
    assert getattr(cli_entrypoint, symbol_name) is getattr(parser_module, symbol_name)


@pytest.mark.unit
def test_remote_admin_builder_rejects_non_integer_input_event() -> None:
    """Non-numeric uint32 input fails through argparse's stable type-error surface."""
    parser = addRemoteAdminArgs(_parser())
    with pytest.raises(SystemExit):
        parser.parse_args(["--send-input-event", "button"])


@pytest.mark.unit
def test_remote_admin_builder_accepts_absolute_delete_path() -> None:
    """Absolute device paths pass parser validation unchanged."""
    args = addRemoteAdminArgs(_parser()).parse_args(
        ["--delete-file", "/prefs/config.proto"]
    )
    assert args.delete_file == "/prefs/config.proto"


@pytest.mark.unit
@pytest.mark.parametrize(
    ("option", "value"),
    [
        ("--key-verify-nonce", "not-an-integer"),
        ("--key-verify-nonce", "-1"),
        ("--key-verify-nonce", str(1 << 64)),
        ("--key-verify-security-number", "not-an-integer"),
        ("--key-verify-security-number", "0"),
        ("--key-verify-security-number", "1000000"),
        ("--key-verify-wait", "not-a-number"),
        ("--key-verify-wait", "0"),
        ("--key-verify-wait", "nan"),
        ("--key-verify-wait", "inf"),
    ],
)
def test_local_action_builder_rejects_invalid_key_verification_values(
    option: str, value: str
) -> None:
    """Key-verification scalar bounds fail in argparse before transport setup."""
    parser = addLocalActionArgs(_parser())

    with pytest.raises(SystemExit):
        parser.parse_args([option, value])


@pytest.mark.unit
def test_local_action_builder_accepts_key_verification_boundaries() -> None:
    """Key-verification parser accepts the full protocol bounds."""
    args = addLocalActionArgs(_parser()).parse_args(
        [
            "--key-verify",
            "provide",
            "--key-verify-nonce",
            str((1 << 64) - 1),
            "--key-verify-security-number",
            "999999",
            "--key-verify-wait",
            "0.5",
        ]
    )

    assert args.key_verify_nonce == (1 << 64) - 1
    assert args.key_verify_security_number == 999_999
    assert args.key_verify_wait == 0.5


@pytest.mark.unit
@pytest.mark.parametrize("value", ["", "ab", "😀"])
def test_remote_admin_builder_rejects_unrepresentable_keyboard_input(
    value: str,
) -> None:
    """Keyboard input fails in argparse when firmware cannot represent it."""
    parser = addRemoteAdminArgs(_parser())

    with pytest.raises(SystemExit):
        parser.parse_args(["--input-kb-char", value])


@pytest.mark.unit
def test_remote_admin_builder_accepts_latin1_keyboard_boundary() -> None:
    """The firmware keyboard field accepts a single code point through 255."""
    args = addRemoteAdminArgs(_parser()).parse_args(["--input-kb-char", "ÿ"])
    assert args.input_kb_char == "ÿ"
