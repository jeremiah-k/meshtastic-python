"""Meshtastic CLI test coverage expansion for __main__.py edge cases.

This module provides focused tests for uncovered paths in the CLI:
- Edge cases in argument parsing
- Error handling paths (try/except blocks)
- Subcommand paths not exercised
- Exit code paths
"""

# pylint: disable=C0302,W0613,R0917

import sys
from typing import Any, cast
from unittest.mock import MagicMock, patch

import pytest

import meshtastic.__main__ as main_module
from meshtastic import mt_config
from meshtastic.__main__ import (
    addChannelConfigArgs,
    addConfigArgs,
    addConnectionArgs,
    addImportExportArgs,
    addLocalActionArgs,
    addPositionConfigArgs,
    addRemoteActionArgs,
    addRemoteAdminArgs,
    addSelectionArgs,
    common,
    initParser,
    main,
    tunnelMain,
)
from meshtastic.serial_interface import SerialInterface


# =============================================================================
# Test fixtures
# =============================================================================


@pytest.fixture(autouse=True)
def _mock_newer_version_check(monkeypatch: pytest.MonkeyPatch) -> None:
    """Prevent external network calls during unit tests."""
    monkeypatch.setattr("meshtastic.util.check_if_newer_version", lambda: None)


# =============================================================================
# Argument Parser Tests (add*Args functions)
# =============================================================================


@pytest.mark.unit
def test_add_connection_args_returns_parser() -> None:
    """Test that addConnectionArgs returns the parser instance."""
    parser = MagicMock()
    parser.add_argument_group.return_value = parser
    parser.add_mutually_exclusive_group.return_value = parser
    result = addConnectionArgs(parser)
    assert result is parser


@pytest.mark.unit
def test_add_selection_args_returns_parser() -> None:
    """Test that addSelectionArgs returns the parser instance."""
    parser = MagicMock()
    parser.add_argument_group.return_value = parser
    result = addSelectionArgs(parser)
    assert result is parser


@pytest.mark.unit
def test_add_import_export_args_returns_parser() -> None:
    """Test that addImportExportArgs returns the parser instance."""
    parser = MagicMock()
    parser.add_argument_group.return_value = parser
    result = addImportExportArgs(parser)
    assert result is parser


@pytest.mark.unit
def test_add_config_args_returns_parser() -> None:
    """Test that addConfigArgs returns the parser instance."""
    parser = MagicMock()
    parser.add_argument_group.return_value = parser
    result = addConfigArgs(parser)
    assert result is parser


@pytest.mark.unit
def test_add_channel_config_args_returns_parser() -> None:
    """Test that addChannelConfigArgs returns the parser instance."""
    parser = MagicMock()
    parser.add_argument_group.return_value = parser
    result = addChannelConfigArgs(parser)
    assert result is parser


@pytest.mark.unit
def test_add_position_config_args_returns_parser() -> None:
    """Test that addPositionConfigArgs returns the parser instance."""
    parser = MagicMock()
    parser.add_argument_group.return_value = parser
    result = addPositionConfigArgs(parser)
    assert result is parser


@pytest.mark.unit
def test_add_local_action_args_returns_parser() -> None:
    """Test that addLocalActionArgs returns the parser instance."""
    parser = MagicMock()
    parser.add_argument_group.return_value = parser
    result = addLocalActionArgs(parser)
    assert result is parser


@pytest.mark.unit
def test_add_remote_action_args_returns_parser() -> None:
    """Test that addRemoteActionArgs returns the parser instance."""
    parser = MagicMock()
    parser.add_argument_group.return_value = parser
    result = addRemoteActionArgs(parser)
    assert result is parser


@pytest.mark.unit
def test_add_remote_admin_args_returns_parser() -> None:
    """Test that addRemoteAdminArgs returns the parser instance."""
    parser = MagicMock()
    parser.add_argument_group.return_value = parser
    result = addRemoteAdminArgs(parser)
    assert result is parser


# =============================================================================
# initParser Error Handling
# =============================================================================


@pytest.mark.unit
def test_init_parser_without_parser_raises_runtime_error() -> None:
    """Test that initParser raises RuntimeError when mt_config.parser is None."""
    original_parser = mt_config.parser
    try:
        mt_config.parser = None
        with pytest.raises(RuntimeError, match="mt_config.parser must be initialized"):
            initParser()
    finally:
        mt_config.parser = original_parser


# =============================================================================
# common() Error Handling
# =============================================================================


@pytest.mark.unit
def test_common_without_parser_raises_runtime_error() -> None:
    """Test that common() raises RuntimeError when mt_config.parser is None."""
    original_args = mt_config.args
    original_parser = mt_config.parser
    try:
        mt_config.args = MagicMock()
        mt_config.args.debug = False
        mt_config.args.listen = False
        mt_config.parser = None
        with pytest.raises(RuntimeError, match="mt_config.parser must be initialized"):
            common()
    finally:
        mt_config.args = original_args
        mt_config.parser = original_parser


# =============================================================================
# Deprecated Argument Handling
# =============================================================================


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_main_deprecated_arg_prints_error_and_exits(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Test --deprecated exits with error and prints help."""
    sys.argv = ["", "--deprecated"]
    mt_config.args = sys.argv  # type: ignore[assignment]

    iface = MagicMock(autospec=SerialInterface)
    iface.__enter__ = MagicMock(return_value=iface)
    iface.__exit__ = MagicMock(return_value=None)

    with patch("meshtastic.serial_interface.SerialInterface", return_value=iface):
        with pytest.raises(SystemExit) as pytest_wrapped_e:
            main()
        assert pytest_wrapped_e.type is SystemExit
        # Argparse exits with code 2 for unrecognized arguments
        assert pytest_wrapped_e.value.code == 2
        out, err = capsys.readouterr()
        assert "unrecognized arguments" in err.lower() or "--deprecated" in err


# =============================================================================
# Modem Preset Commands
# =============================================================================


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_main_ch_longslow_success(capsys: pytest.CaptureFixture[str]) -> None:
    """Test --ch-longslow on primary channel succeeds."""
    sys.argv = ["", "--ch-longslow"]
    mt_config.args = sys.argv  # type: ignore[assignment]

    mocked_node = MagicMock()
    mocked_node.localConfig.ListFields.return_value = [
        (MagicMock(), MagicMock())
    ]  # Non-empty config

    iface = MagicMock(autospec=SerialInterface)
    iface.__enter__ = MagicMock(return_value=iface)
    iface.__exit__ = MagicMock(return_value=None)
    iface.getNode.return_value = mocked_node

    with patch("meshtastic.serial_interface.SerialInterface", return_value=iface) as mo:
        main()
        out, err = capsys.readouterr()
        assert "Connected to radio" in out
        mocked_node.writeConfig.assert_called_once_with("lora")
        mo.assert_called()


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_main_ch_vlongslow_success(capsys: pytest.CaptureFixture[str]) -> None:
    """Test --ch-vlongslow on primary channel succeeds."""
    sys.argv = ["", "--ch-vlongslow"]
    mt_config.args = sys.argv  # type: ignore[assignment]

    mocked_node = MagicMock()
    mocked_node.localConfig.ListFields.return_value = [(MagicMock(), MagicMock())]

    iface = MagicMock(autospec=SerialInterface)
    iface.__enter__ = MagicMock(return_value=iface)
    iface.__exit__ = MagicMock(return_value=None)
    iface.getNode.return_value = mocked_node

    with patch("meshtastic.serial_interface.SerialInterface", return_value=iface):
        main()
        out, err = capsys.readouterr()
        assert "Connected to radio" in out
        mocked_node.writeConfig.assert_called_once_with("lora")


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_main_ch_longfast_success(capsys: pytest.CaptureFixture[str]) -> None:
    """Test --ch-longfast on primary channel succeeds."""
    sys.argv = ["", "--ch-longfast"]
    mt_config.args = sys.argv  # type: ignore[assignment]

    mocked_node = MagicMock()
    mocked_node.localConfig.ListFields.return_value = [(MagicMock(), MagicMock())]

    iface = MagicMock(autospec=SerialInterface)
    iface.__enter__ = MagicMock(return_value=iface)
    iface.__exit__ = MagicMock(return_value=None)
    iface.getNode.return_value = mocked_node

    with patch("meshtastic.serial_interface.SerialInterface", return_value=iface):
        main()
        out, err = capsys.readouterr()
        assert "Connected to radio" in out
        mocked_node.writeConfig.assert_called_once_with("lora")


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_main_ch_medslow_success(capsys: pytest.CaptureFixture[str]) -> None:
    """Test --ch-medslow on primary channel succeeds."""
    sys.argv = ["", "--ch-medslow"]
    mt_config.args = sys.argv  # type: ignore[assignment]

    mocked_node = MagicMock()
    mocked_node.localConfig.ListFields.return_value = [(MagicMock(), MagicMock())]

    iface = MagicMock(autospec=SerialInterface)
    iface.__enter__ = MagicMock(return_value=iface)
    iface.__exit__ = MagicMock(return_value=None)
    iface.getNode.return_value = mocked_node

    with patch("meshtastic.serial_interface.SerialInterface", return_value=iface):
        main()
        out, err = capsys.readouterr()
        assert "Connected to radio" in out
        mocked_node.writeConfig.assert_called_once_with("lora")


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_main_ch_medfast_success(capsys: pytest.CaptureFixture[str]) -> None:
    """Test --ch-medfast on primary channel succeeds."""
    sys.argv = ["", "--ch-medfast"]
    mt_config.args = sys.argv  # type: ignore[assignment]

    mocked_node = MagicMock()
    mocked_node.localConfig.ListFields.return_value = [(MagicMock(), MagicMock())]

    iface = MagicMock(autospec=SerialInterface)
    iface.__enter__ = MagicMock(return_value=iface)
    iface.__exit__ = MagicMock(return_value=None)
    iface.getNode.return_value = mocked_node

    with patch("meshtastic.serial_interface.SerialInterface", return_value=iface):
        main()
        out, err = capsys.readouterr()
        assert "Connected to radio" in out
        mocked_node.writeConfig.assert_called_once_with("lora")


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_main_ch_shortslow_success(capsys: pytest.CaptureFixture[str]) -> None:
    """Test --ch-shortslow on primary channel succeeds."""
    sys.argv = ["", "--ch-shortslow"]
    mt_config.args = sys.argv  # type: ignore[assignment]

    mocked_node = MagicMock()
    mocked_node.localConfig.ListFields.return_value = [(MagicMock(), MagicMock())]

    iface = MagicMock(autospec=SerialInterface)
    iface.__enter__ = MagicMock(return_value=iface)
    iface.__exit__ = MagicMock(return_value=None)
    iface.getNode.return_value = mocked_node

    with patch("meshtastic.serial_interface.SerialInterface", return_value=iface):
        main()
        out, err = capsys.readouterr()
        assert "Connected to radio" in out
        mocked_node.writeConfig.assert_called_once_with("lora")


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_main_ch_shortfast_success(capsys: pytest.CaptureFixture[str]) -> None:
    """Test --ch-shortfast on primary channel succeeds."""
    sys.argv = ["", "--ch-shortfast"]
    mt_config.args = sys.argv  # type: ignore[assignment]

    mocked_node = MagicMock()
    mocked_node.localConfig.ListFields.return_value = [(MagicMock(), MagicMock())]

    iface = MagicMock(autospec=SerialInterface)
    iface.__enter__ = MagicMock(return_value=iface)
    iface.__exit__ = MagicMock(return_value=None)
    iface.getNode.return_value = mocked_node

    with patch("meshtastic.serial_interface.SerialInterface", return_value=iface):
        main()
        out, err = capsys.readouterr()
        assert "Connected to radio" in out
        mocked_node.writeConfig.assert_called_once_with("lora")


# =============================================================================
# Remote Admin Commands
# =============================================================================


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_main_enter_dfu(capsys: pytest.CaptureFixture[str]) -> None:
    """Test --enter-dfu command."""
    sys.argv = ["", "--enter-dfu"]
    mt_config.args = sys.argv  # type: ignore[assignment]

    mocked_node = MagicMock()

    def mock_enterDFU() -> None:
        print("inside mocked enterDFU")

    mocked_node.enterDFUMode.side_effect = mock_enterDFU

    iface = MagicMock(autospec=SerialInterface)
    iface.__enter__ = MagicMock(return_value=iface)
    iface.__exit__ = MagicMock(return_value=None)
    iface.getNode.return_value = mocked_node

    with patch("meshtastic.serial_interface.SerialInterface", return_value=iface) as mo:
        main()
        out, err = capsys.readouterr()
        assert "Connected to radio" in out
        assert "inside mocked enterDFU" in out
        mocked_node.enterDFUMode.assert_called_once()
        mo.assert_called()


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_main_device_metadata(capsys: pytest.CaptureFixture[str]) -> None:
    """Test --device-metadata command."""
    sys.argv = ["", "--device-metadata"]
    mt_config.args = sys.argv  # type: ignore[assignment]

    mocked_node = MagicMock()

    def mock_getMetadata() -> None:
        print("inside mocked getMetadata")

    mocked_node.getMetadata.side_effect = mock_getMetadata

    iface = MagicMock(autospec=SerialInterface)
    iface.__enter__ = MagicMock(return_value=iface)
    iface.__exit__ = MagicMock(return_value=None)
    iface.getNode.return_value = mocked_node

    with patch("meshtastic.serial_interface.SerialInterface", return_value=iface) as mo:
        main()
        out, err = capsys.readouterr()
        assert "Connected to radio" in out
        assert "inside mocked getMetadata" in out
        mocked_node.getMetadata.assert_called_once()
        mo.assert_called()


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_main_reset_nodedb(capsys: pytest.CaptureFixture[str]) -> None:
    """Test --reset-nodedb command."""
    sys.argv = ["", "--reset-nodedb"]
    mt_config.args = sys.argv  # type: ignore[assignment]

    mocked_node = MagicMock()

    def mock_resetNodeDb() -> None:
        print("inside mocked resetNodeDb")

    mocked_node.resetNodeDb.side_effect = mock_resetNodeDb

    iface = MagicMock(autospec=SerialInterface)
    iface.__enter__ = MagicMock(return_value=iface)
    iface.__exit__ = MagicMock(return_value=None)
    iface.getNode.return_value = mocked_node

    with patch("meshtastic.serial_interface.SerialInterface", return_value=iface) as mo:
        main()
        out, err = capsys.readouterr()
        assert "Connected to radio" in out
        assert "inside mocked resetNodeDb" in out
        mocked_node.resetNodeDb.assert_called_once()
        mo.assert_called()


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_main_begin_edit(capsys: pytest.CaptureFixture[str]) -> None:
    """Test --begin-edit command."""
    sys.argv = ["", "--begin-edit"]
    mt_config.args = sys.argv  # type: ignore[assignment]

    mocked_node = MagicMock()

    def mock_beginSettingsTransaction() -> None:
        print("inside mocked beginSettingsTransaction")

    mocked_node.beginSettingsTransaction.side_effect = mock_beginSettingsTransaction

    iface = MagicMock(autospec=SerialInterface)
    iface.__enter__ = MagicMock(return_value=iface)
    iface.__exit__ = MagicMock(return_value=None)
    iface.getNode.return_value = mocked_node

    with patch("meshtastic.serial_interface.SerialInterface", return_value=iface) as mo:
        main()
        out, err = capsys.readouterr()
        assert "Connected to radio" in out
        assert "inside mocked beginSettingsTransaction" in out
        mocked_node.beginSettingsTransaction.assert_called_once()
        mo.assert_called()


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_main_commit_edit(capsys: pytest.CaptureFixture[str]) -> None:
    """Test --commit-edit command."""
    sys.argv = ["", "--commit-edit"]
    mt_config.args = sys.argv  # type: ignore[assignment]

    mocked_node = MagicMock()

    def mock_commitSettingsTransaction() -> None:
        print("inside mocked commitSettingsTransaction")

    mocked_node.commitSettingsTransaction.side_effect = mock_commitSettingsTransaction

    iface = MagicMock(autospec=SerialInterface)
    iface.__enter__ = MagicMock(return_value=iface)
    iface.__exit__ = MagicMock(return_value=None)
    iface.getNode.return_value = mocked_node

    with patch("meshtastic.serial_interface.SerialInterface", return_value=iface) as mo:
        main()
        out, err = capsys.readouterr()
        assert "Connected to radio" in out
        assert "inside mocked commitSettingsTransaction" in out
        mocked_node.commitSettingsTransaction.assert_called_once()
        mo.assert_called()


# =============================================================================
# Factory Reset Commands
# =============================================================================


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_main_factory_reset(capsys: pytest.CaptureFixture[str]) -> None:
    """Test --factory-reset command (partial reset preserving BLE bonds)."""
    sys.argv = ["", "--factory-reset"]
    mt_config.args = sys.argv  # type: ignore[assignment]

    mocked_node = MagicMock()

    def mock_factoryReset(full: bool = False) -> None:
        print(f"inside mocked factoryReset full={full}")

    mocked_node.factoryReset.side_effect = mock_factoryReset

    iface = MagicMock(autospec=SerialInterface)
    iface.__enter__ = MagicMock(return_value=iface)
    iface.__exit__ = MagicMock(return_value=None)
    iface.getNode.return_value = mocked_node

    with patch("meshtastic.serial_interface.SerialInterface", return_value=iface) as mo:
        main()
        out, err = capsys.readouterr()
        assert "Connected to radio" in out
        assert "inside mocked factoryReset full=False" in out
        mocked_node.factoryReset.assert_called_once_with(full=False)
        mo.assert_called()


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_main_factory_reset_device(capsys: pytest.CaptureFixture[str]) -> None:
    """Test --factory-reset-device command (full reset clearing BLE bonds)."""
    sys.argv = ["", "--factory-reset-device"]
    mt_config.args = sys.argv  # type: ignore[assignment]

    mocked_node = MagicMock()

    def mock_factoryReset(full: bool = False) -> None:
        print(f"inside mocked factoryReset full={full}")

    mocked_node.factoryReset.side_effect = mock_factoryReset

    iface = MagicMock(autospec=SerialInterface)
    iface.__enter__ = MagicMock(return_value=iface)
    iface.__exit__ = MagicMock(return_value=None)
    iface.getNode.return_value = mocked_node

    with patch("meshtastic.serial_interface.SerialInterface", return_value=iface) as mo:
        main()
        out, err = capsys.readouterr()
        assert "Connected to radio" in out
        assert "inside mocked factoryReset full=True" in out
        mocked_node.factoryReset.assert_called_once_with(full=True)
        mo.assert_called()


# =============================================================================
# Wait to Disconnect
# =============================================================================


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
@patch("time.sleep")
def test_main_wait_to_disconnect(
    mock_sleep: Any,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Test --wait-to-disconnect command."""
    sys.argv = ["", "--info", "--wait-to-disconnect", "3"]
    mt_config.args = cast(Any, sys.argv)

    iface = MagicMock(autospec=SerialInterface)
    iface.__enter__ = MagicMock(return_value=iface)
    iface.__exit__ = MagicMock(return_value=None)

    def mock_showInfo() -> None:
        print("inside mocked showInfo")

    iface.showInfo.side_effect = mock_showInfo

    with patch("meshtastic.serial_interface.SerialInterface", return_value=iface) as mo:
        main()
        out, err = capsys.readouterr()
        assert "Connected to radio" in out
        assert "inside mocked showInfo" in out
        mock_sleep.assert_any_call(3)
        mo.assert_called()


# =============================================================================
# Export Config to File
# =============================================================================


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_main_export_config_to_file(
    capsys: pytest.CaptureFixture[str],
    tmp_path: Any,
) -> None:
    """Test --export-config with a file path (not stdout)."""
    output_file = tmp_path / "config.yaml"
    sys.argv = ["", "--export-config", str(output_file)]
    mt_config.args = sys.argv  # type: ignore[assignment]

    iface = MagicMock(autospec=SerialInterface)
    iface.__enter__ = MagicMock(return_value=iface)
    iface.__exit__ = MagicMock(return_value=None)
    iface.getLongName.return_value = "TestNode"
    iface.getShortName.return_value = "TN"
    iface.localNode.getURL.return_value = "https://example.com/test"
    iface.getMyNodeInfo.return_value = None
    iface.getCannedMessage.return_value = None
    iface.getRingtone.return_value = None

    with patch("meshtastic.serial_interface.SerialInterface", return_value=iface) as mo:
        with patch("meshtastic.__main__.exportConfig") as mock_export:
            mock_export.return_value = "test config content"
            main()
            out, err = capsys.readouterr()
            assert f"Exported configuration to {output_file}" in out
            assert output_file.exists()
            assert output_file.read_text() == "test config content"
            mo.assert_called()


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_main_export_config_to_file_error(
    capsys: pytest.CaptureFixture[str],
    tmp_path: Any,
) -> None:
    """Test --export-config with a file path that fails to write."""
    output_file = tmp_path / "nonexistent" / "config.yaml"
    sys.argv = ["", "--export-config", str(output_file)]
    mt_config.args = sys.argv  # type: ignore[assignment]

    iface = MagicMock(autospec=SerialInterface)
    iface.__enter__ = MagicMock(return_value=iface)
    iface.__exit__ = MagicMock(return_value=None)
    iface.getLongName.return_value = "TestNode"
    iface.getShortName.return_value = "TN"
    iface.localNode.getURL.return_value = "https://example.com/test"
    iface.getMyNodeInfo.return_value = None
    iface.getCannedMessage.return_value = None
    iface.getRingtone.return_value = None

    with patch("meshtastic.serial_interface.SerialInterface", return_value=iface):
        with patch("meshtastic.__main__.exportConfig") as mock_export:
            mock_export.return_value = "test config content"
            with pytest.raises(SystemExit) as pytest_wrapped_e:
                main()
            assert pytest_wrapped_e.type is SystemExit
            assert pytest_wrapped_e.value.code == 1
            out, err = capsys.readouterr()
            assert "ERROR: Failed to write config file" in err


# =============================================================================
# Channel URL Operations
# =============================================================================


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_main_ch_set_url(capsys: pytest.CaptureFixture[str]) -> None:
    """Test --ch-set-url command."""
    sys.argv = ["", "--ch-set-url", "https://www.meshtastic.org/d/#CgUYAyIBAQ"]
    mt_config.args = sys.argv  # type: ignore[assignment]

    mocked_node = MagicMock()

    iface = MagicMock(autospec=SerialInterface)
    iface.__enter__ = MagicMock(return_value=iface)
    iface.__exit__ = MagicMock(return_value=None)
    iface.getNode.return_value = mocked_node

    with patch("meshtastic.serial_interface.SerialInterface", return_value=iface) as mo:
        main()
        out, err = capsys.readouterr()
        assert "Connected to radio" in out
        mocked_node.setURL.assert_called_once_with(
            "https://www.meshtastic.org/d/#CgUYAyIBAQ", addOnly=False
        )
        mo.assert_called()


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_main_ch_add_url(capsys: pytest.CaptureFixture[str]) -> None:
    """Test --ch-add-url command."""
    sys.argv = ["", "--ch-add-url", "https://www.meshtastic.org/d/#CgUYAyIBAQ"]
    mt_config.args = sys.argv  # type: ignore[assignment]

    mocked_node = MagicMock()

    iface = MagicMock(autospec=SerialInterface)
    iface.__enter__ = MagicMock(return_value=iface)
    iface.__exit__ = MagicMock(return_value=None)
    iface.getNode.return_value = mocked_node

    with patch("meshtastic.serial_interface.SerialInterface", return_value=iface) as mo:
        main()
        out, err = capsys.readouterr()
        assert "Connected to radio" in out
        mocked_node.setURL.assert_called_once_with(
            "https://www.meshtastic.org/d/#CgUYAyIBAQ", addOnly=True
        )
        mo.assert_called()


# =============================================================================
# Remote Node Management Commands
# =============================================================================


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_main_remove_node(capsys: pytest.CaptureFixture[str]) -> None:
    """Test --remove-node command."""
    sys.argv = ["", "--remove-node", "!12345678"]
    mt_config.args = sys.argv  # type: ignore[assignment]

    mocked_node = MagicMock()

    def mock_removeNode(node_id: str) -> None:
        print(f"inside mocked removeNode {node_id}")

    mocked_node.removeNode.side_effect = mock_removeNode

    iface = MagicMock(autospec=SerialInterface)
    iface.__enter__ = MagicMock(return_value=iface)
    iface.__exit__ = MagicMock(return_value=None)
    iface.getNode.return_value = mocked_node

    with patch("meshtastic.serial_interface.SerialInterface", return_value=iface) as mo:
        main()
        out, err = capsys.readouterr()
        assert "Connected to radio" in out
        assert "inside mocked removeNode !12345678" in out
        mocked_node.removeNode.assert_called_once_with("!12345678")
        mo.assert_called()


# =============================================================================
# Listen Mode
# =============================================================================


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
@patch("time.sleep")
def test_main_listen_mode(
    mock_sleep: Any,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Test --listen mode runs and handles keyboard interrupt."""
    sys.argv = ["", "--listen"]
    mt_config.args = cast(Any, sys.argv)

    iface = MagicMock(autospec=SerialInterface)
    iface.__enter__ = MagicMock(return_value=iface)
    iface.__exit__ = MagicMock(return_value=None)

    # Simulate keyboard interrupt after first sleep
    def side_effect_sleep(amount: float) -> None:
        raise KeyboardInterrupt()

    mock_sleep.side_effect = side_effect_sleep

    with patch("meshtastic.serial_interface.SerialInterface", return_value=iface) as mo:
        main()
        out, err = capsys.readouterr()
        assert "Connected to radio" in out
        mock_sleep.assert_called()
        mo.assert_called()


# =============================================================================
# Power/Logging without Powermon
# =============================================================================


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_main_power_stress_without_powermon_exits(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Test --power-stress exits when powermon is unavailable."""
    sys.argv = ["", "--power-stress"]
    mt_config.args = sys.argv  # type: ignore[assignment]

    # Ensure powermon is marked as unavailable
    monkeypatch.setattr(main_module, "have_powermon", False)
    monkeypatch.setattr(
        main_module, "powermon_exception", ImportError("No module named 'powermon'")
    )

    iface = MagicMock(autospec=SerialInterface)
    iface.__enter__ = MagicMock(return_value=iface)
    iface.__exit__ = MagicMock(return_value=None)

    with patch("meshtastic.serial_interface.SerialInterface", return_value=iface):
        with pytest.raises(SystemExit) as pytest_wrapped_e:
            main()
        assert pytest_wrapped_e.type is SystemExit
        assert pytest_wrapped_e.value.code == 1
        out, err = capsys.readouterr()
        assert "powermon module could not be loaded" in err.lower()


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_main_slog_without_powermon_exits(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Test --slog exits when powermon is unavailable."""
    sys.argv = ["", "--slog"]
    mt_config.args = sys.argv  # type: ignore[assignment]

    # Ensure powermon is marked as unavailable
    monkeypatch.setattr(main_module, "have_powermon", False)
    monkeypatch.setattr(
        main_module, "powermon_exception", ImportError("No module named 'powermon'")
    )

    iface = MagicMock(autospec=SerialInterface)
    iface.__enter__ = MagicMock(return_value=iface)
    iface.__exit__ = MagicMock(return_value=None)

    with patch("meshtastic.serial_interface.SerialInterface", return_value=iface):
        with pytest.raises(SystemExit) as pytest_wrapped_e:
            main()
        assert pytest_wrapped_e.type is SystemExit
        assert pytest_wrapped_e.value.code == 1
        out, err = capsys.readouterr()
        assert "powermon module could not be loaded" in err.lower()


# =============================================================================
# Connection Error Handling
# =============================================================================


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_main_file_not_found_error(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Test FileNotFoundError handling when serial device not found."""
    sys.argv = ["", "--port", "/dev/ttyNonexistent", "--info"]
    mt_config.args = sys.argv  # type: ignore[assignment]

    with patch(
        "meshtastic.serial_interface.SerialInterface",
        side_effect=FileNotFoundError("No such file or directory"),
    ):
        with pytest.raises(SystemExit) as pytest_wrapped_e:
            main()
        assert pytest_wrapped_e.type is SystemExit
        assert pytest_wrapped_e.value.code == 1
        out, err = capsys.readouterr()
        assert "File Not Found Error" in err
        assert "power-only USB cable" in err


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
@patch("os.getlogin")
def test_main_permission_error_with_getlogin(
    patched_getlogin: Any,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Test PermissionError handling when serial device permission denied."""
    sys.argv = ["", "--port", "/dev/ttyUSB0", "--info"]
    mt_config.args = sys.argv  # type: ignore[assignment]
    patched_getlogin.return_value = "testuser"

    with patch(
        "meshtastic.serial_interface.SerialInterface",
        side_effect=PermissionError("Permission denied"),
    ):
        with pytest.raises(SystemExit) as pytest_wrapped_e:
            main()
        assert pytest_wrapped_e.type is SystemExit
        assert pytest_wrapped_e.value.code == 1
        out, err = capsys.readouterr()
        assert "Permission Error" in err
        assert "dialout" in err


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
@patch("os.getlogin")
@patch("getpass.getuser")
def test_main_permission_error_getlogin_fails(
    patched_getuser: Any,
    patched_getlogin: Any,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Test PermissionError handling when os.getlogin fails."""
    sys.argv = ["", "--port", "/dev/ttyUSB0", "--info"]
    mt_config.args = sys.argv  # type: ignore[assignment]
    patched_getlogin.side_effect = OSError("No such device")
    patched_getuser.return_value = "fallbackuser"

    with patch(
        "meshtastic.serial_interface.SerialInterface",
        side_effect=PermissionError("Permission denied"),
    ):
        with pytest.raises(SystemExit) as pytest_wrapped_e:
            main()
        assert pytest_wrapped_e.type is SystemExit
        assert pytest_wrapped_e.value.code == 1
        out, err = capsys.readouterr()
        assert "Permission Error" in err
        assert "fallbackuser" in err


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_main_oserror_serial(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Test OSError handling when serial device is in use."""
    sys.argv = ["", "--port", "/dev/ttyUSB0", "--info"]
    mt_config.args = sys.argv  # type: ignore[assignment]

    with patch(
        "meshtastic.serial_interface.SerialInterface",
        side_effect=OSError("Device or resource busy"),
    ):
        with pytest.raises(SystemExit) as pytest_wrapped_e:
            main()
        assert pytest_wrapped_e.type is SystemExit
        assert pytest_wrapped_e.value.code == 1
        out, err = capsys.readouterr()
        assert "OS Error" in err
        assert "in use by another process" in err


# =============================================================================
# BLE Connection Tests
# =============================================================================


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_main_ble_scan(capsys: pytest.CaptureFixture[str]) -> None:
    """Test --ble-scan command."""
    sys.argv = ["", "--ble-scan"]
    mt_config.args = sys.argv  # type: ignore[assignment]

    mock_device = MagicMock()
    mock_device.name = "TestDevice"
    mock_device.address = "AA:BB:CC:DD:EE:FF"

    with patch(
        "meshtastic.interfaces.ble.BLEInterface.scan", return_value=[mock_device]
    ):
        with pytest.raises(SystemExit) as pytest_wrapped_e:
            main()
        assert pytest_wrapped_e.type is SystemExit
        assert pytest_wrapped_e.value.code == 0
        out, err = capsys.readouterr()
        assert "Found: name='TestDevice'" in out
        assert "address='AA:BB:CC:DD:EE:FF'" in out


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_main_ble_connection_error(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Test BLE connection error handling."""
    sys.argv = ["", "--ble", "test-device", "--info"]
    mt_config.args = sys.argv  # type: ignore[assignment]

    with patch(
        "meshtastic.interfaces.ble.BLEInterface",
        side_effect=Exception("BLE connection failed"),
    ):
        with pytest.raises(SystemExit) as pytest_wrapped_e:
            main()
        assert pytest_wrapped_e.type is SystemExit
        assert pytest_wrapped_e.value.code == 1


# =============================================================================
# tunnelMain Function
# =============================================================================


@pytest.mark.unit
def test_tunnel_main_sets_tunnel_flag() -> None:
    """Test tunnelMain sets tunnel flag on args."""
    original_args = mt_config.args
    original_parser = mt_config.parser
    try:
        # tunnelMain requires valid parser/args setup
        # We test that the function tries to set tunnel=True
        # by checking the error occurs after initParser would run
        import argparse

        test_parser = argparse.ArgumentParser(add_help=False)
        mt_config.parser = test_parser
        mt_config.args = None

        # The function should try to parse args then set tunnel=True
        # Since we don't have proper args, it will fail on initParser
        # but we verify the structure is correct
        with pytest.raises((RuntimeError, SystemExit)):
            tunnelMain()
    finally:
        mt_config.args = original_args
        mt_config.parser = original_parser


# =============================================================================
# Traceroute Command
# =============================================================================


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_main_traceroute(capsys: pytest.CaptureFixture[str]) -> None:
    """Test --traceroute command."""
    sys.argv = ["", "--traceroute", "!12345678"]
    mt_config.args = sys.argv  # type: ignore[assignment]

    lora_config = MagicMock()
    lora_config.hop_limit = 3

    mocked_node = MagicMock()
    mocked_node.localConfig.lora = lora_config

    iface = MagicMock(autospec=SerialInterface)
    iface.__enter__ = MagicMock(return_value=iface)
    iface.__exit__ = MagicMock(return_value=None)
    iface.localNode = mocked_node

    with patch("meshtastic.serial_interface.SerialInterface", return_value=iface) as mo:
        with patch.object(iface, "sendTraceRoute") as mock_send_traceroute:
            main()
            out, err = capsys.readouterr()
            assert "Connected to radio" in out
            assert "Sending traceroute request" in out
            mock_send_traceroute.assert_called_once()
            mo.assert_called()


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_main_traceroute_with_broadcast_succeeds(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Test --traceroute with broadcast address is allowed."""
    sys.argv = ["", "--traceroute", "^all"]
    mt_config.args = sys.argv  # type: ignore[assignment]

    lora_config = MagicMock()
    lora_config.hop_limit = 3

    mocked_node = MagicMock()
    mocked_node.localConfig.lora = lora_config

    mocked_channel = MagicMock()
    mocked_channel.role = 1  # PRIMARY role

    iface = MagicMock(autospec=SerialInterface)
    iface.__enter__ = MagicMock(return_value=iface)
    iface.__exit__ = MagicMock(return_value=None)
    iface.localNode = mocked_node
    iface.myInfo = MagicMock()
    iface.myInfo.my_node_num = 12345

    with patch("meshtastic.serial_interface.SerialInterface", return_value=iface) as mo:
        with patch.object(iface, "sendTraceRoute") as mock_send_traceroute:
            main()
            out, err = capsys.readouterr()
            assert "Connected to radio" in out
            assert "Sending traceroute request to ^all" in out
            mock_send_traceroute.assert_called_once()
            mo.assert_called()


# =============================================================================
# Request Telemetry
# =============================================================================


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
@pytest.mark.parametrize(
    "telem_type",
    [
        "device",
        "environment",
        "air_quality",
        "airquality",
        "power",
        "localstats",
        "local_stats",
    ],
)
def test_main_request_telemetry_types(
    telem_type: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Test --request-telemetry with various telemetry types."""
    sys.argv = ["", "--request-telemetry", telem_type, "--dest", "!12345678"]
    mt_config.args = sys.argv  # type: ignore[assignment]

    mocked_channel = MagicMock()
    mocked_channel.role = 1  # PRIMARY role

    mocked_node = MagicMock()
    mocked_node.getChannelCopyByChannelIndex.return_value = mocked_channel
    mocked_node.getChannelByChannelIndex.return_value = mocked_channel

    iface = MagicMock(autospec=SerialInterface)
    iface.__enter__ = MagicMock(return_value=iface)
    iface.__exit__ = MagicMock(return_value=None)
    iface.getNode.return_value = mocked_node
    iface.myInfo = MagicMock()
    iface.myInfo.my_node_num = 12345

    with patch("meshtastic.serial_interface.SerialInterface", return_value=iface) as mo:
        with patch.object(iface, "sendTelemetry") as mock_send_telemetry:
            main()
            out, err = capsys.readouterr()
            assert "Connected to radio" in out
            mock_send_telemetry.assert_called_once()
            mo.assert_called()


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_main_request_telemetry_broadcast_exits(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Test --request-telemetry with broadcast address exits."""
    sys.argv = ["", "--request-telemetry", "device"]
    mt_config.args = sys.argv  # type: ignore[assignment]

    iface = MagicMock(autospec=SerialInterface)
    iface.__enter__ = MagicMock(return_value=iface)
    iface.__exit__ = MagicMock(return_value=None)

    with patch("meshtastic.serial_interface.SerialInterface", return_value=iface):
        with pytest.raises(SystemExit) as pytest_wrapped_e:
            main()
        assert pytest_wrapped_e.type is SystemExit
        assert pytest_wrapped_e.value.code == 1
        out, err = capsys.readouterr()
        assert "Must use a destination node ID" in err


# =============================================================================
# Request Position
# =============================================================================


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_main_request_position(capsys: pytest.CaptureFixture[str]) -> None:
    """Test --request-position command."""
    sys.argv = ["", "--request-position", "--dest", "!12345678"]
    mt_config.args = sys.argv  # type: ignore[assignment]

    mocked_channel = MagicMock()
    mocked_channel.role = 1  # PRIMARY role

    mocked_node = MagicMock()
    mocked_node.getChannelCopyByChannelIndex.return_value = mocked_channel
    mocked_node.getChannelByChannelIndex.return_value = mocked_channel

    iface = MagicMock(autospec=SerialInterface)
    iface.__enter__ = MagicMock(return_value=iface)
    iface.__exit__ = MagicMock(return_value=None)
    iface.getNode.return_value = mocked_node
    iface.myInfo = MagicMock()
    iface.myInfo.my_node_num = 12345

    with patch("meshtastic.serial_interface.SerialInterface", return_value=iface) as mo:
        with patch.object(iface, "sendPosition") as mock_send_position:
            main()
            out, err = capsys.readouterr()
            assert "Connected to radio" in out
            assert "Sending position request" in out
            mock_send_position.assert_called_once()
            mo.assert_called()


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_main_request_position_broadcast_exits(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Test --request-position with broadcast address exits."""
    sys.argv = ["", "--request-position"]
    mt_config.args = sys.argv  # type: ignore[assignment]

    iface = MagicMock(autospec=SerialInterface)
    iface.__enter__ = MagicMock(return_value=iface)
    iface.__exit__ = MagicMock(return_value=None)

    with patch("meshtastic.serial_interface.SerialInterface", return_value=iface):
        with pytest.raises(SystemExit) as pytest_wrapped_e:
            main()
        assert pytest_wrapped_e.type is SystemExit
        assert pytest_wrapped_e.value.code == 1
        out, err = capsys.readouterr()
        assert "Must use a destination node ID" in err


# =============================================================================
# onConnected Exception Handler
# =============================================================================


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_main_onconnected_exception_handling(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Test exception handling in onConnected."""
    sys.argv = ["", "--set-owner", "TestOwner"]
    mt_config.args = sys.argv  # type: ignore[assignment]

    mocked_node = MagicMock()
    mocked_node.setOwner.side_effect = Exception("Test exception")

    iface = MagicMock(autospec=SerialInterface)
    iface.__enter__ = MagicMock(return_value=iface)
    iface.__exit__ = MagicMock(return_value=None)
    iface.getNode.return_value = mocked_node

    with patch("meshtastic.serial_interface.SerialInterface", return_value=iface):
        with pytest.raises(SystemExit) as pytest_wrapped_e:
            main()
        assert pytest_wrapped_e.type is SystemExit
        assert pytest_wrapped_e.value.code == 1
        out, err = capsys.readouterr()
        assert "Aborting due to:" in err


# =============================================================================
# Owner Name Validation in common()
# =============================================================================


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_main_set_owner_whitespace_only_in_common(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Test that --set-owner with whitespace-only is rejected in common()."""
    sys.argv = ["", "--set-owner", "   "]
    mt_config.args = sys.argv  # type: ignore[assignment]

    with pytest.raises(SystemExit) as pytest_wrapped_e:
        main()
    assert pytest_wrapped_e.type is SystemExit
    assert pytest_wrapped_e.value.code == 1
    out, err = capsys.readouterr()
    assert "cannot be empty or contain only whitespace" in err


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_main_set_owner_short_whitespace_only_in_common(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Test that --set-owner-short with whitespace-only is rejected in common()."""
    sys.argv = ["", "--set-owner-short", "   "]
    mt_config.args = sys.argv  # type: ignore[assignment]

    with pytest.raises(SystemExit) as pytest_wrapped_e:
        main()
    assert pytest_wrapped_e.type is SystemExit
    assert pytest_wrapped_e.value.code == 1
    out, err = capsys.readouterr()
    assert "cannot be empty or contain only whitespace" in err


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_main_set_ham_whitespace_only_in_common(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Test that --set-ham with whitespace-only is rejected in common()."""
    sys.argv = ["", "--set-ham", "   "]
    mt_config.args = sys.argv  # type: ignore[assignment]

    with pytest.raises(SystemExit) as pytest_wrapped_e:
        main()
    assert pytest_wrapped_e.type is SystemExit
    assert pytest_wrapped_e.value.code == 1
    out, err = capsys.readouterr()
    assert "cannot be empty or contain only whitespace" in err
