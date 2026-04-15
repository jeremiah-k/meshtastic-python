"""Meshtastic CLI test coverage expansion for __main__.py edge cases.

This module provides focused tests for uncovered paths in the CLI:
- Edge cases in argument parsing
- Error handling paths (try/except blocks)
- Subcommand paths not exercised
- Exit code paths
"""

# pylint: disable=C0302,W0613,R0917

import sys
import time as _time
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
        _out, err = capsys.readouterr()
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
        out, _err = capsys.readouterr()
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
        out, _err = capsys.readouterr()
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
        out, _err = capsys.readouterr()
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
        out, _err = capsys.readouterr()
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
        out, _err = capsys.readouterr()
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
        out, _err = capsys.readouterr()
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
        out, _err = capsys.readouterr()
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
        out, _err = capsys.readouterr()
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
        out, _err = capsys.readouterr()
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
        out, _err = capsys.readouterr()
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
        out, _err = capsys.readouterr()
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
        out, _err = capsys.readouterr()
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
        out, _err = capsys.readouterr()
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
        out, _err = capsys.readouterr()
        assert "Connected to radio" in out
        assert "inside mocked factoryReset full=True" in out
        mocked_node.factoryReset.assert_called_once_with(full=True)
        mo.assert_called()


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_main_factory_reset_device_ack_skips_wait(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Full factory reset should skip trailing ACK waits because transport drops."""
    sys.argv = ["", "--factory-reset-device", "--ack"]
    mt_config.args = sys.argv  # type: ignore[assignment]

    mocked_node = MagicMock()

    def mock_factoryReset(full: bool = False) -> None:
        print(f"inside mocked factoryReset full={full}")

    mocked_node.factoryReset.side_effect = mock_factoryReset

    iface = MagicMock(autospec=SerialInterface)
    iface.__enter__ = MagicMock(return_value=iface)
    iface.__exit__ = MagicMock(return_value=None)
    iface.getNode.return_value = mocked_node
    mocked_node.iface = iface

    with patch("meshtastic.serial_interface.SerialInterface", return_value=iface):
        main()
        out, _err = capsys.readouterr()
        assert "Connected to radio" in out
        assert "inside mocked factoryReset full=True" in out
        assert "Waiting for an acknowledgment from remote node" not in out

    mocked_node.factoryReset.assert_called_once_with(full=True)
    iface.waitForAckNak.assert_not_called()


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
        out, _err = capsys.readouterr()
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
            out, _err = capsys.readouterr()
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
    _out, err = capsys.readouterr()
    assert "cannot be empty or contain only whitespace" in err


# =============================================================================
# _post_configure_reconnect_and_verify tests
# =============================================================================


def _make_mock_interface(connected: bool = True) -> MagicMock:
    iface = MagicMock()
    iface.isConnected.is_set.return_value = connected
    iface.waitForConfig.return_value = None
    return iface


@pytest.mark.unit
def test_post_configure_no_disconnect_returns_quickly() -> None:
    iface = _make_mock_interface(connected=True)
    start = _time.monotonic()
    result = main_module._post_configure_reconnect_and_verify(
        iface,
        timeout=10.0,
        node_dest="!local",
    )
    elapsed = _time.monotonic() - start
    assert result is main_module._ConfigureReconnectResult.VERIFIED
    assert elapsed < 4.0


@pytest.mark.unit
def test_post_configure_detects_disconnect_and_reconnect() -> None:
    iface = _make_mock_interface(connected=True)
    call_count = {"n": 0}

    def _is_set_side_effect() -> bool:
        call_count["n"] += 1
        if call_count["n"] <= 3:
            return False
        return True

    iface.isConnected.is_set.side_effect = _is_set_side_effect
    result = main_module._post_configure_reconnect_and_verify(
        iface,
        timeout=10.0,
        node_dest="!local",
    )
    assert result is main_module._ConfigureReconnectResult.VERIFIED
    iface.waitForConfig.assert_called_once()


@pytest.mark.unit
def test_post_configure_reconnect_timeout() -> None:
    iface = _make_mock_interface(connected=False)
    result = main_module._post_configure_reconnect_and_verify(
        iface,
        timeout=0.5,
        node_dest="!local",
    )
    assert result is main_module._ConfigureReconnectResult.RECONNECT_FAILED


@pytest.mark.unit
def test_post_configure_config_reload_fails() -> None:
    iface = _make_mock_interface(connected=True)
    iface.waitForConfig.side_effect = RuntimeError("reload failed")
    result = main_module._post_configure_reconnect_and_verify(
        iface,
        timeout=10.0,
        node_dest="!local",
    )
    assert result is main_module._ConfigureReconnectResult.CONFIG_RELOAD_FAILED


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
    _out, err = capsys.readouterr()
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
    _out, err = capsys.readouterr()
    assert "cannot be empty or contain only whitespace" in err
