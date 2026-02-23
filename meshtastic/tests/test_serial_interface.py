"""Meshtastic unit tests for serial_interface.py."""

import logging
import re
import sys
from unittest.mock import MagicMock, mock_open, patch

import pytest

from ..mesh_interface import MeshInterface
from ..protobuf import config_pb2
from ..serial_interface import SerialInterface


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
    serial_interface = None
    try:
        with caplog.at_level(logging.WARNING):
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
    # Defensive safety-net: side_effect would catch unexpected flush() calls,
    # but SerialInterface.close() skips flush when is_open is False.
    stream.flush.side_effect = RuntimeError("flush on closed stream")
    iface.close()

    # flush can be called during __init__ for setup; we only guarantee close()
    # won't raise and still performs underlying cleanup.
    assert mocked_findPorts.called
    stream.close.assert_called()
    if sys.platform != "win32":
        mocked_open.assert_called()
        mock_hupcl.assert_called()
    mock_sleep.assert_called()
