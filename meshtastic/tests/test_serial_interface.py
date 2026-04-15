"""Meshtastic unit tests for serial_interface.py."""

import logging
import re
import sys
import threading
from typing import Any, cast
from unittest.mock import MagicMock, mock_open, patch

import pytest

from ..mesh_interface import MeshInterface
from ..protobuf import config_pb2
from ..serial_interface import SerialInterface
from ..stream_interface import StreamInterface


# pylint: disable=R0917
@pytest.mark.unit
@patch("time.sleep")
@patch("meshtastic.serial_interface.SerialInterface._set_hupcl_with_termios")
@patch("builtins.open", new_callable=mock_open, read_data="data")
@patch("serial.Serial")
@patch("meshtastic.util.findPorts", return_value=["/dev/ttyUSBfake"])
def test_SerialInterface_single_port(
    mocked_findPorts: MagicMock,
    mocked_serial: MagicMock,
    mocked_open: MagicMock,
    mock_hupcl: MagicMock,
    mock_sleep: MagicMock,
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

    # doesn't get called in SerialInterface on windows
    if sys.platform != "win32":
        mocked_open.assert_called()
        mock_hupcl.assert_called()

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
@patch("time.sleep")
@patch("meshtastic.serial_interface.SerialInterface._set_hupcl_with_termios")
@patch("builtins.open", new_callable=mock_open, read_data="data")
@patch("serial.Serial")
@patch("meshtastic.util.findPorts", return_value=["/dev/ttyUSBfake"])
def test_SerialInterface_close_skips_flush_when_stream_closed(
    mocked_findPorts: MagicMock,
    mocked_serial: MagicMock,
    mocked_open: MagicMock,
    mock_hupcl: MagicMock,
    mock_sleep: MagicMock,
) -> None:
    """close() should not fail when stream exists but is already closed."""
    stream = mocked_serial.return_value

    iface = SerialInterface(noProto=True, connectNow=False)
    stream.is_open = False
    # setup may flush during constructor; only assert close() behavior.
    stream.flush.reset_mock()
    mock_sleep.reset_mock()
    # Defensive safety-net: side_effect would catch unexpected flush() calls,
    # but SerialInterface.close() skips flush when is_open is False.
    stream.flush.side_effect = RuntimeError("flush on closed stream")
    iface.close()

    # flush can be called during __init__ for setup; we only guarantee close()
    # won't raise and still performs underlying cleanup.
    mocked_findPorts.assert_called()
    stream.close.assert_called()
    if sys.platform != "win32":
        mocked_open.assert_called()
        mock_hupcl.assert_called()
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
def test_serial_interface_connect_does_not_retry_non_retryable_errors() -> None:
    """connect() should fail immediately on non-retryable errors."""
    iface = object.__new__(SerialInterface)
    iface.devPath = "/dev/ttyUSB0"
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
        iface.connect()
    open_stream.assert_not_called()
