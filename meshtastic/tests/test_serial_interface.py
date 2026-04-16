"""Meshtastic unit tests for serial_interface.py."""

import logging
import re
import threading
from typing import Any, cast
from unittest.mock import MagicMock, mock_open, patch

import pytest

from ..mesh_interface import MeshInterface
from ..protobuf import config_pb2
from ..serial_interface import (
    SERIAL_CONNECT_STREAM_CLOSED_RETRY_DELAY_SECONDS,
    SerialInterface,
)
from ..stream_interface import StreamInterface


# pylint: disable=R0917
@pytest.mark.unit
@patch("os.path.exists", return_value=True)
@patch("time.sleep")
@patch("meshtastic.serial_interface.SerialInterface._clear_hupcl_on_fd")
@patch("meshtastic.serial_interface.SerialInterface._set_hupcl_with_termios")
@patch("builtins.open", new_callable=mock_open, read_data="data")
@patch("serial.Serial")
@patch("meshtastic.util.findPorts", return_value=["/dev/ttyUSBfake"])
def test_SerialInterface_single_port(
    mocked_findPorts: MagicMock,
    mocked_serial: MagicMock,
    mocked_open: MagicMock,
    mock_hupcl: MagicMock,
    mock_clear_hupcl: MagicMock,
    mock_sleep: MagicMock,
    mock_exists: MagicMock,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Test that we can instantiate a SerialInterface with a single port."""
    iface = SerialInterface(noProto=True)
    iface.localNode.localConfig.lora.CopyFrom(config_pb2.Config.LoRaConfig())
    iface.showInfo()
    iface.localNode.showInfo()
    iface.close()
    mocked_findPorts.assert_called()
    mocked_serial.assert_called()

    mock_sleep.assert_called()
    out, err = capsys.readouterr()
    assert re.search(r"Nodes in mesh", out, re.MULTILINE)
    assert re.search(r"Preferences", out, re.MULTILINE)
    assert re.search(r"Channels", out, re.MULTILINE)
    assert re.search(r"Primary channel", out, re.MULTILINE)
    assert err == ""


@pytest.mark.unit
@patch("meshtastic.util.findPorts", return_value=[])
def test_SerialInterface_no_ports(
    mocked_findPorts: MagicMock, caplog: pytest.LogCaptureFixture
) -> None:
    """Test that we can instantiate a SerialInterface with no ports."""
    serial_interface: SerialInterface | None = None
    try:
        with caplog.at_level(logging.INFO):
            serial_interface = SerialInterface(noProto=True)
        mocked_findPorts.assert_called()
        assert serial_interface.devPath is None
        assert re.search(r"No.*Meshtastic.*device.*detected", caplog.text, re.MULTILINE)
    finally:
        if serial_interface is not None:
            serial_interface.close()


@pytest.mark.unit
@patch(
    "meshtastic.util.findPorts", return_value=["/dev/ttyUSBfake1", "/dev/ttyUSBfake2"]
)
def test_SerialInterface_multiple_ports(mocked_findPorts: MagicMock) -> None:
    """Test that SerialInterface raises MeshInterfaceError when multiple ports are detected."""
    with pytest.raises(MeshInterface.MeshInterfaceError) as exc_info:
        SerialInterface(noProto=True)
    mocked_findPorts.assert_called()
    assert "Multiple serial ports were detected" in str(exc_info.value)
    assert "'--port'" in str(exc_info.value)


@pytest.mark.unit
def test_SerialInterface_rejects_empty_explicit_port_path() -> None:
    """Explicit empty/whitespace serial paths should fail fast with MeshInterfaceError."""
    with pytest.raises(MeshInterface.MeshInterfaceError) as exc_info:
        SerialInterface(devPath="   ", noProto=True)
    assert "Serial port path cannot be empty" in str(exc_info.value)


@pytest.mark.unit
@patch("os.path.exists", return_value=True)
@patch("time.sleep")
@patch("meshtastic.serial_interface.SerialInterface._clear_hupcl_on_fd")
@patch("meshtastic.serial_interface.SerialInterface._set_hupcl_with_termios")
@patch("builtins.open", new_callable=mock_open, read_data="data")
@patch("serial.Serial")
@patch("meshtastic.util.findPorts", return_value=["/dev/ttyUSBfake"])
def test_SerialInterface_close_skips_flush_when_stream_closed(
    mocked_findPorts: MagicMock,
    mocked_serial: MagicMock,
    mocked_open: MagicMock,
    mock_hupcl: MagicMock,
    mock_clear_hupcl: MagicMock,
    mock_sleep: MagicMock,
    mock_exists: MagicMock,
) -> None:
    """close() should not fail when stream exists but is already closed."""
    stream = mocked_serial.return_value

    iface = SerialInterface(noProto=True, connectNow=False)
    stream.is_open = False
    stream.flush.reset_mock()
    mock_sleep.reset_mock()
    stream.flush.side_effect = RuntimeError("flush on closed stream")
    iface.close()

    mocked_findPorts.assert_called()
    stream.close.assert_called()
    stream.flush.assert_not_called()
    mock_sleep.assert_not_called()


@pytest.mark.unit
def test_resolve_dev_path_returns_explicit_non_empty_path() -> None:
    """_resolve_dev_path should return an explicit non-empty path unchanged."""
    iface = object.__new__(SerialInterface)
    iface.devPath = "/dev/ttyUSB0"

    assert iface._resolve_dev_path() == "/dev/ttyUSB0"

    iface.devPath = "  /dev/ttyUSB0  "
    assert iface._resolve_dev_path() == "/dev/ttyUSB0"


@pytest.mark.unit
def test_serial_interface_repr_includes_optional_fields() -> None:
    """__repr__ should include debugOut/noProto/noNodes when they are set."""
    iface = object.__new__(SerialInterface)
    iface.devPath = "/dev/ttyUSB0"
    iface.debugOut = lambda _line: None
    iface.noProto = True
    iface.noNodes = True

    rendered = repr(iface)

    assert "SerialInterface(devPath='/dev/ttyUSB0'" in rendered
    assert "debugOut=" in rendered
    assert "noProto=True" in rendered
    assert "noNodes=True" in rendered
    assert rendered.endswith(")")


@pytest.mark.unit
@patch("time.sleep")
def test_serial_interface_connect_retries_transient_connect_errors(
    mock_sleep: MagicMock,
) -> None:
    """connect() should retry when startup fails due transient transport loss."""
    iface = object.__new__(SerialInterface)
    iface.devPath = "/dev/ttyUSB0"
    stream = MagicMock()
    stream.is_open = True
    iface.stream = stream
    iface._connect_lock = threading.Lock()
    iface._dev_path_auto_detected = False
    transient_error = MeshInterface.MeshInterfaceError(
        "Connection lost while waiting for connection completion (stream.closed)"
    )
    with (
        patch.object(
            StreamInterface,
            "connect",
            side_effect=[transient_error, None],
        ) as base_connect,
        patch.object(iface, "_open_serial_stream", return_value=stream),
    ):
        iface.connect()
    assert base_connect.call_count == 2
    stream.close.assert_called_once()
    mock_sleep.assert_called_once()


@pytest.mark.unit
@patch("time.sleep")
def test_serial_interface_connect_uses_fast_retry_delay_for_stream_closed(
    mock_sleep: MagicMock,
) -> None:
    """connect() should use fast retry pacing after bootstrap stream-close failures."""
    iface = object.__new__(SerialInterface)
    iface.devPath = "/dev/ttyUSB0"
    stream = MagicMock()
    stream.is_open = True
    iface.stream = stream
    iface._connect_lock = threading.Lock()
    iface._dev_path_auto_detected = False
    iface._last_disconnect_source = "stream.closed"
    transient_error = MeshInterface.MeshInterfaceError(
        "Connection lost while waiting for connection completion (stream.closed)"
    )
    with (
        patch.object(
            StreamInterface,
            "connect",
            side_effect=[transient_error, None],
        ),
        patch.object(iface, "_open_serial_stream", return_value=stream),
    ):
        iface.connect()
    mock_sleep.assert_called_once_with(SERIAL_CONNECT_STREAM_CLOSED_RETRY_DELAY_SECONDS)


@pytest.mark.unit
def test_serial_interface_connect_does_not_retry_non_retryable_errors() -> None:
    """connect() should fail immediately on non-retryable errors."""
    iface = object.__new__(SerialInterface)
    iface.devPath = "/dev/ttyUSB0"
    iface._dev_path_auto_detected = False
    stream = MagicMock()
    stream.is_open = True
    iface.stream = stream
    with patch.object(
        StreamInterface,
        "connect",
        side_effect=RuntimeError("non-retryable failure"),
    ) as base_connect:
        with pytest.raises(RuntimeError, match="non-retryable failure"):
            iface.connect()
    assert base_connect.call_count == 1


@pytest.mark.unit
def test_serial_interface_connect_skips_reopen_when_reader_alive() -> None:
    """connect() should not reopen stream while reader thread is already running."""
    iface = object.__new__(SerialInterface)
    iface.devPath = "/dev/ttyUSB0"
    iface._dev_path_auto_detected = False
    iface.stream = None
    iface._connect_lock = threading.Lock()
    iface._stream_close_in_progress = False
    iface._wantExit = False
    cast_iface = cast(Any, iface)
    cast_iface._provides_own_stream = False
    iface._rxThread = MagicMock()
    iface._rxThread.is_alive.return_value = True
    iface._rxThread.ident = 1
    with patch.object(iface, "_open_serial_stream") as open_stream:
        with pytest.raises(StreamInterface.StreamInterfaceError, match="reader thread"):
            iface.connect()
    open_stream.assert_not_called()


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
@patch("os.path.exists", return_value=True)
@patch("time.sleep")
@patch("meshtastic.serial_interface.SerialInterface._clear_hupcl_on_fd")
@patch("meshtastic.serial_interface.SerialInterface._set_hupcl_with_termios")
@patch("builtins.open", new_callable=mock_open, read_data="data")
@patch("serial.Serial")
@patch("meshtastic.util.findPorts", return_value=["/dev/ttyUSBfake"])
def test_auto_detected_flag_true_when_devPath_none(
    mocked_findPorts: MagicMock,
    mocked_serial: MagicMock,
    mocked_open: MagicMock,
    mock_hupcl: MagicMock,
    mock_clear_hupcl: MagicMock,
    mock_sleep: MagicMock,
    mock_exists: MagicMock,
) -> None:
    """_dev_path_auto_detected should be True when devPath is auto-detected."""
    iface = SerialInterface(noProto=True)
    assert iface._dev_path_auto_detected is True
    iface.close()


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
@patch("os.path.exists", return_value=True)
@patch("time.sleep")
@patch("meshtastic.serial_interface.SerialInterface._clear_hupcl_on_fd")
@patch("meshtastic.serial_interface.SerialInterface._set_hupcl_with_termios")
@patch("builtins.open", new_callable=mock_open, read_data="data")
@patch("serial.Serial")
def test_auto_detected_flag_false_when_devPath_explicit(
    mocked_serial: MagicMock,
    mocked_open: MagicMock,
    mock_hupcl: MagicMock,
    mock_clear_hupcl: MagicMock,
    mock_sleep: MagicMock,
    mock_exists: MagicMock,
) -> None:
    """_dev_path_auto_detected should be False when devPath is user-specified."""
    iface = SerialInterface(devPath="/dev/ttyUSB0", noProto=True)
    assert iface._dev_path_auto_detected is False
    iface.close()


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
@patch("time.sleep")
def test_devPath_reset_on_retry_for_auto_detected(mock_sleep: MagicMock) -> None:
    """DevPath should be reset to None on retry when auto-detected."""
    iface = object.__new__(SerialInterface)
    iface.devPath = "/dev/ttyUSB0"
    stream = MagicMock()
    stream.is_open = True
    iface.stream = stream
    iface._connect_lock = threading.Lock()
    iface._dev_path_auto_detected = True
    transient_error = MeshInterface.MeshInterfaceError(
        "Connection lost while waiting for connection completion (stream.closed)"
    )
    with (
        patch.object(
            StreamInterface,
            "connect",
            side_effect=[transient_error, None],
        ),
        patch.object(iface, "_open_serial_stream", return_value=stream),
    ):
        iface.connect()
    assert iface.devPath is None


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
@patch("time.sleep")
def test_devPath_preserved_on_retry_for_user_specified(
    mock_sleep: MagicMock,
) -> None:
    """DevPath should NOT be reset on retry when user-specified."""
    iface = object.__new__(SerialInterface)
    iface.devPath = "/dev/ttyUSB0"
    stream = MagicMock()
    stream.is_open = True
    iface.stream = stream
    iface._connect_lock = threading.Lock()
    iface._dev_path_auto_detected = False
    transient_error = MeshInterface.MeshInterfaceError(
        "Connection lost while waiting for connection completion (stream.closed)"
    )
    with (
        patch.object(
            StreamInterface,
            "connect",
            side_effect=[transient_error, None],
        ),
        patch.object(iface, "_open_serial_stream", return_value=stream),
    ):
        iface.connect()
    assert iface.devPath == "/dev/ttyUSB0"


@pytest.mark.unit
@patch("os.path.exists", return_value=True)
@patch("time.sleep")
@patch("meshtastic.serial_interface.SerialInterface._clear_hupcl_on_fd")
@patch("meshtastic.serial_interface.SerialInterface._set_hupcl_with_termios")
@patch("builtins.open", new_callable=mock_open, read_data="data")
def test_input_buffer_drained_when_data_waiting(
    mocked_open: MagicMock,
    mock_hupcl: MagicMock,
    mock_clear_hupcl: MagicMock,
    mock_sleep: MagicMock,
    mock_exists: MagicMock,
) -> None:
    """reset_input_buffer should be called when in_waiting > 0."""
    mocked_serial_instance = MagicMock()
    mocked_serial_instance.in_waiting = 42
    mocked_serial_instance.flush = MagicMock()
    iface = object.__new__(SerialInterface)
    iface.devPath = "/dev/ttyUSB0"
    with patch("serial.Serial", return_value=mocked_serial_instance):
        iface._open_serial_stream()
    mocked_serial_instance.reset_input_buffer.assert_called_once()


@pytest.mark.unit
@patch("os.path.exists", return_value=True)
@patch("time.sleep")
@patch("meshtastic.serial_interface.SerialInterface._clear_hupcl_on_fd")
@patch("meshtastic.serial_interface.SerialInterface._set_hupcl_with_termios")
@patch("builtins.open", new_callable=mock_open, read_data="data")
def test_input_buffer_not_drained_when_empty(
    mocked_open: MagicMock,
    mock_hupcl: MagicMock,
    mock_clear_hupcl: MagicMock,
    mock_sleep: MagicMock,
    mock_exists: MagicMock,
) -> None:
    """Input buffer drain should NOT be called when in_waiting is 0."""
    mocked_serial_instance = MagicMock()
    mocked_serial_instance.in_waiting = 0
    mocked_serial_instance.flush = MagicMock()
    iface = object.__new__(SerialInterface)
    iface.devPath = "/dev/ttyUSB0"
    with patch("serial.Serial", return_value=mocked_serial_instance):
        iface._open_serial_stream()
    mocked_serial_instance.reset_input_buffer.assert_not_called()


@pytest.mark.unit
@patch("os.path.exists", return_value=True)
@patch("time.sleep")
@patch("meshtastic.serial_interface.SerialInterface._clear_hupcl_on_fd")
def test_open_serial_stream_sets_control_lines_before_open(
    mock_clear_hupcl: MagicMock,
    mock_sleep: MagicMock,
    mock_exists: MagicMock,
) -> None:
    """Serial open should preconfigure control lines before opening the port."""
    mocked_serial_instance = MagicMock()
    mocked_serial_instance.in_waiting = 0
    mocked_serial_instance.flush = MagicMock()
    iface = object.__new__(SerialInterface)
    iface.devPath = "/dev/ttyUSB0"
    with patch("serial.Serial", return_value=mocked_serial_instance) as serial_ctor:
        iface._open_serial_stream()
    serial_ctor.assert_called_once()
    assert mocked_serial_instance.port == "/dev/ttyUSB0"
    assert mocked_serial_instance.dtr is True
    assert mocked_serial_instance.rts is False
    mocked_serial_instance.open.assert_called_once()


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
@patch("os.path.exists", return_value=False)
def test_port_existence_check_raises_for_missing_device(
    mock_exists: MagicMock,
) -> None:
    """MeshInterfaceError should be raised when serial port path does not exist."""
    iface = object.__new__(SerialInterface)
    iface.devPath = "/dev/ttyUSB0"
    with pytest.raises(MeshInterface.MeshInterfaceError, match="does not exist"):
        iface._open_serial_stream()
