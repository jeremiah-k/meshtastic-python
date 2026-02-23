"""Meshtastic unit tests for remote_hardware.py."""

import logging
import re
from unittest.mock import MagicMock, create_autospec

import pytest

from ..mesh_interface import MeshInterface
from ..protobuf import portnums_pb2, remote_hardware_pb2
from ..remote_hardware import RemoteHardwareClient, onGPIOreceive
from ..serial_interface import SerialInterface


def _mock_iface_with_gpio_channel(channel_index: int = 0) -> MagicMock:
    """Create a SerialInterface mock that provides a stubbed GPIO channel.

    Parameters
    ----------
    channel_index : int
        Index to assign to the mocked GPIO channel (default 0).

    Returns
    -------
    MagicMock
        An autospecced SerialInterface mock whose localNode.getChannelByName returns the mocked channel.
    """
    iface = create_autospec(SerialInterface, instance=True)
    iface.localNode = MagicMock()
    channel = MagicMock()
    channel.index = channel_index
    iface.localNode.getChannelByName.return_value = channel
    return iface


@pytest.mark.unit
def test_RemoteHardwareClient():
    """Test that we can instantiate a RemoteHardwareClient instance."""
    iface = _mock_iface_with_gpio_channel()
    try:
        rhw = RemoteHardwareClient(iface)
        assert rhw.iface == iface
    finally:
        iface.close()


@pytest.mark.unit
def test_onGPIOreceive(caplog):
    """Test onGPIOreceive."""
    iface = create_autospec(SerialInterface, instance=True)
    iface.mask = 0xFFFFFFFF
    packet = {"decoded": {"remotehw": {"type": "foo", "gpioValue": "4096"}}}
    with caplog.at_level(logging.INFO):
        onGPIOreceive(packet, iface)
        assert re.search(r"Received RemoteHardware", caplog.text)
        assert re.search(r"value=4096", caplog.text)


@pytest.mark.unit
def test_onGPIOreceive_mask_fallback(caplog):
    """Test onGPIOreceive uses packet gpioMask when interface.mask is None."""
    iface = create_autospec(SerialInterface, instance=True)
    iface.mask = None
    packet = {"decoded": {"remotehw": {"gpioValue": "7", "gpioMask": 7}}}
    with caplog.at_level(logging.DEBUG):
        onGPIOreceive(packet, iface)
        assert re.search(r"Received RemoteHardware", caplog.text)
        assert re.search(r"mask:7", caplog.text)
        assert re.search(r"value=7", caplog.text)


@pytest.mark.unit
def test_RemoteHardwareClient_no_gpio_channel():
    """Test that RemoteHardwareClient raises MeshInterfaceError when there is no channel named 'gpio'."""
    iface = create_autospec(SerialInterface, instance=True)
    iface.localNode = MagicMock()
    iface.localNode.getChannelByName.return_value = None
    with pytest.raises(MeshInterface.MeshInterfaceError) as exc_info:
        RemoteHardwareClient(iface)
    assert "No channel named 'gpio'" in str(exc_info.value)


@pytest.mark.unit
def test_readGPIOs(caplog):
    """Test readGPIOs."""
    iface = _mock_iface_with_gpio_channel()
    try:
        rhw = RemoteHardwareClient(iface)
        with caplog.at_level(logging.DEBUG):
            rhw.readGPIOs("0x10", 123)
        assert re.search(r"readGPIOs", caplog.text)
        iface.sendData.assert_called_once()
        args, kwargs = iface.sendData.call_args
        assert args[1] == "0x10"
        assert args[2] == portnums_pb2.REMOTE_HARDWARE_APP
        payload = args[0]
        assert payload.type == remote_hardware_pb2.HardwareMessage.Type.READ_GPIOS
        assert payload.gpio_mask == 123
        assert kwargs["wantAck"] is True
        assert kwargs["channelIndex"] == rhw.channelIndex
        assert kwargs["wantResponse"] is True
        assert kwargs["onResponse"] is None
    finally:
        iface.close()


@pytest.mark.unit
def test_writeGPIOs(caplog):
    """Test writeGPIOs."""
    iface = _mock_iface_with_gpio_channel()
    try:
        rhw = RemoteHardwareClient(iface)
        with caplog.at_level(logging.DEBUG):
            rhw.writeGPIOs("0x10", 123, 1)
        assert re.search(r"writeGPIOs", caplog.text)
        iface.sendData.assert_called_once()
        args, kwargs = iface.sendData.call_args
        assert args[1] == "0x10"
        assert args[2] == portnums_pb2.REMOTE_HARDWARE_APP
        payload = args[0]
        assert payload.type == remote_hardware_pb2.HardwareMessage.Type.WRITE_GPIOS
        assert payload.gpio_mask == 123
        assert payload.gpio_value == 1
        assert kwargs["wantAck"] is True
        assert kwargs["channelIndex"] == rhw.channelIndex
        assert kwargs["wantResponse"] is False
        assert kwargs["onResponse"] is None
    finally:
        iface.close()


@pytest.mark.unit
def test_watchGPIOs(caplog):
    """Verify RemoteHardwareClient.watchGPIOs logs a "watchGPIOs" marker when invoked with a GPIO node and mask.

    Runs watchGPIOs("0x10", 123) with DEBUG-level logging enabled and asserts that the captured logs contain "watchGPIOs".

    """
    iface = _mock_iface_with_gpio_channel()
    try:
        rhw = RemoteHardwareClient(iface)
        with caplog.at_level(logging.DEBUG):
            rhw.watchGPIOs("0x10", 123)
        assert re.search(r"watchGPIOs", caplog.text)
        iface.sendData.assert_called_once()
        args, kwargs = iface.sendData.call_args
        assert args[1] == "0x10"
        assert args[2] == portnums_pb2.REMOTE_HARDWARE_APP
        payload = args[0]
        assert payload.type == remote_hardware_pb2.HardwareMessage.Type.WATCH_GPIOS
        assert payload.gpio_mask == 123
        assert kwargs["wantAck"] is True
        assert kwargs["channelIndex"] == rhw.channelIndex
        assert kwargs["wantResponse"] is False
        assert kwargs["onResponse"] is None
        assert iface.mask == 123
    finally:
        iface.close()


@pytest.mark.unit
def test_send_hardware_no_nodeid():
    """Test sending no nodeid to _send_hardware()."""
    iface = _mock_iface_with_gpio_channel()
    try:
        rhw = RemoteHardwareClient(iface)
        with pytest.raises(
            MeshInterface.MeshInterfaceError, match="Must use a destination node ID"
        ):
            rhw._send_hardware(None, None)  # type: ignore[arg-type]
    finally:
        iface.close()
