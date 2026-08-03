"""Focused CLI tests for LoRa modem-preset channel options."""

import re
import sys
from unittest.mock import MagicMock, patch

import pytest

import meshtastic.__main__ as main_module
from meshtastic import mt_config
from meshtastic.__main__ import main
from meshtastic.node import Node
from meshtastic.protobuf import config_pb2, localonly_pb2
from meshtastic.serial_interface import SerialInterface


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_main_ch_longfast_on_non_primary_channel(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Verify that invoking the CLI with --ch-longfast and a non-primary.

    --ch-index exits with code 1 and prints a warning that the modem preset
    cannot be set for a non-primary channel while still showing
    "Connected to radio".

    """
    monkeypatch.setattr(sys, "argv", ["meshtastic", "--ch-longfast", "--ch-index", "1"])

    mocked_node = MagicMock(autospec=Node)

    iface = MagicMock(autospec=SerialInterface)
    iface.__enter__ = MagicMock(return_value=iface)
    iface.__exit__ = MagicMock(return_value=None)
    iface.getNode.return_value = mocked_node

    with patch("meshtastic.serial_interface.SerialInterface", return_value=iface) as mo:
        with pytest.raises(SystemExit) as pytest_wrapped_e:
            main()
        assert pytest_wrapped_e.type is SystemExit
        assert pytest_wrapped_e.value.code == 1
        out, err = capsys.readouterr()
        combined = out + err
        assert re.search(r"Connected to radio", out, re.MULTILINE)
        assert re.search(
            r"Warning: Cannot set modem preset for non-primary channel",
            combined,
            re.MULTILINE,
        )
        mo.assert_called()


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
@pytest.mark.parametrize(
    ("cli_args", "expected_preset"),
    (
        pytest.param(
            ("--ch-longmod",),
            config_pb2.Config.LoRaConfig.ModemPreset.LONG_MODERATE,
            id="long-moderate",
        ),
        pytest.param(
            ("--ch-longturbo",),
            config_pb2.Config.LoRaConfig.ModemPreset.LONG_TURBO,
            id="long-turbo",
        ),
        pytest.param(
            ("--ch-shortturbo",),
            config_pb2.Config.LoRaConfig.ModemPreset.SHORT_TURBO,
            id="short-turbo",
        ),
        pytest.param(
            ("--ch-preset", "medium-turbo"),
            config_pb2.Config.LoRaConfig.ModemPreset.MEDIUM_TURBO,
            id="generic-active-schema-value",
        ),
    ),
)
def test_main_modem_preset_options_write_expected_lora_config(
    monkeypatch: pytest.MonkeyPatch,
    cli_args: tuple[str, ...],
    expected_preset: config_pb2.Config.LoRaConfig.ModemPreset.ValueType,
) -> None:
    """New shorthand and generic options should share the existing write path."""
    monkeypatch.setattr(sys, "argv", ["meshtastic", *cli_args])
    mocked_node = MagicMock(autospec=Node)
    mocked_node.localConfig = localonly_pb2.LocalConfig()
    iface = MagicMock(autospec=SerialInterface)
    iface.__enter__ = MagicMock(return_value=iface)
    iface.__exit__ = MagicMock(return_value=None)
    iface.getNode.return_value = mocked_node

    with patch("meshtastic.serial_interface.SerialInterface", return_value=iface):
        main()

    assert mocked_node.localConfig.lora.modem_preset == expected_preset
    mocked_node.writeConfig.assert_called_once_with("lora")
    mocked_node.requestConfig.assert_called_once()


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_main_ch_preset_accepts_integer_enum_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Programmatic callers can pass integer ModemPreset values in ch_preset."""
    monkeypatch.setattr(sys, "argv", ["meshtastic", "--ch-longmod"])

    mocked_node = MagicMock(autospec=Node)
    mocked_node.localConfig = localonly_pb2.LocalConfig()

    iface = MagicMock(autospec=SerialInterface)
    iface.__enter__ = MagicMock(return_value=iface)
    iface.__exit__ = MagicMock(return_value=None)
    iface.getNode.return_value = mocked_node

    with patch("meshtastic.serial_interface.SerialInterface", return_value=iface):
        # After initParser, patch ch_preset to an integer before common() runs
        original_init = main_module.initParser

        def _patched_init() -> None:
            original_init()
            assert mt_config.args is not None
            mt_config.args.ch_preset = config_pb2.Config.LoRaConfig.ModemPreset.Value(
                "SHORT_TURBO"
            )

        monkeypatch.setattr(main_module, "initParser", _patched_init)
        main()

    assert (
        mocked_node.localConfig.lora.modem_preset
        == config_pb2.Config.LoRaConfig.ModemPreset.SHORT_TURBO
    )
    mocked_node.writeConfig.assert_called_with("lora")


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_main_ch_preset_rejects_invalid_integer_enum_value(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Invalid integer enum values in ch_preset should cause a clean CLI exit."""
    monkeypatch.setattr(sys, "argv", ["meshtastic", "--ch-longmod"])

    mocked_node = MagicMock(autospec=Node)
    mocked_node.localConfig = localonly_pb2.LocalConfig()
    iface = MagicMock(autospec=SerialInterface)
    iface.__enter__ = MagicMock(return_value=iface)
    iface.__exit__ = MagicMock(return_value=None)
    iface.getNode.return_value = mocked_node

    with patch("meshtastic.serial_interface.SerialInterface", return_value=iface):
        original_init = main_module.initParser

        def _patched_init() -> None:
            original_init()
            assert mt_config.args is not None
            mt_config.args.ch_preset = 99999

        monkeypatch.setattr(main_module, "initParser", _patched_init)
        with pytest.raises(SystemExit) as exc_info:
            main()
        assert exc_info.value.code == 1
        _out, err = capsys.readouterr()
        assert "has no name defined for value 99999" in err
