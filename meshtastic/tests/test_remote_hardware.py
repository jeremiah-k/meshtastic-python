"""Meshtastic unit tests for remote_hardware.py."""

import logging
import re
from unittest.mock import MagicMock

import pytest

from ..mesh_interface import MeshInterface
from ..remote_hardware import RemoteHardwareClient, onGPIOreceive
from ..serial_interface import SerialInterface


def _mock_iface_with_gpio_channel(channel_index: int = 0) -> MagicMock:
    """Create a SerialInterface mock with an explicit gpio channel stub."""
    iface = MagicMock(autospec=SerialInterface)
    channel = MagicMock()
    channel.index = channel_index
    iface.localNode.getChannelByName.return_value = channel
    return iface


@pytest.mark.unit
def test_RemoteHardwareClient():
    """Test that we can instantiate a RemoteHardwareClient instance."""
    iface = _mock_iface_with_gpio_channel()
    rhw = RemoteHardwareClient(iface)
    assert rhw.iface == iface
    iface.close()


@pytest.mark.unit
def test_onGPIOreceive(caplog):
    """Test onGPIOreceive."""
    iface = MagicMock(autospec=SerialInterface)
    packet = {"decoded": {"remotehw": {"type": "foo", "gpioValue": "4096"}}}
    with caplog.at_level(logging.INFO):
        onGPIOreceive(packet, iface)
        assert re.search(r"Received RemoteHardware", caplog.text)


@pytest.mark.unit
def test_onGPIOreceive_mask_fallback(caplog):
    """Test onGPIOreceive uses packet gpioMask when interface.mask is None."""
    iface = MagicMock(autospec=SerialInterface)
    iface.mask = None
    packet = {"decoded": {"remotehw": {"gpioValue": "7", "gpioMask": 7}}}
    with caplog.at_level(logging.INFO):
        onGPIOreceive(packet, iface)
        assert re.search(r"Received RemoteHardware", caplog.text)


@pytest.mark.unit
def test_RemoteHardwareClient_no_gpio_channel():
    """Test that RemoteHardwareClient raises MeshInterfaceError when there is no channel named 'gpio'."""
    iface = MagicMock(autospec=SerialInterface)
    iface.localNode.getChannelByName.return_value = None
    with pytest.raises(MeshInterface.MeshInterfaceError) as exc_info:
        RemoteHardwareClient(iface)
    assert "No channel named 'gpio'" in str(exc_info.value)


@pytest.mark.unit
def test_readGPIOs(caplog):
    """Test readGPIOs."""
    iface = _mock_iface_with_gpio_channel()
    rhw = RemoteHardwareClient(iface)
    with caplog.at_level(logging.DEBUG):
        rhw.readGPIOs("0x10", 123)
    assert re.search(r"readGPIOs", caplog.text, re.MULTILINE)
    iface.close()


@pytest.mark.unit
def test_writeGPIOs(caplog):
    """Test writeGPIOs."""
    iface = _mock_iface_with_gpio_channel()
    rhw = RemoteHardwareClient(iface)
    with caplog.at_level(logging.DEBUG):
        rhw.writeGPIOs("0x10", 123, 1)
    assert re.search(r"writeGPIOs", caplog.text, re.MULTILINE)
    iface.close()


@pytest.mark.unit
def test_watchGPIOs(caplog):
    """Test watchGPIOs."""
    iface = _mock_iface_with_gpio_channel()
    rhw = RemoteHardwareClient(iface)
    with caplog.at_level(logging.DEBUG):
        rhw.watchGPIOs("0x10", 123)
    assert re.search(r"watchGPIOs", caplog.text, re.MULTILINE)
    iface.close()


@pytest.mark.unit
def test_sendHardware_no_nodeid():
    """Test sending no nodeid to _sendHardware()."""
    iface = MagicMock(autospec=SerialInterface)
    channel = MagicMock()
    channel.index = 0
    iface.localNode.getChannelByName.return_value = channel
    rhw = RemoteHardwareClient(iface)
    with pytest.raises(MeshInterface.MeshInterfaceError) as exc_info:
        rhw._sendHardware(None, None)
    assert "Must use a destination node ID" in str(exc_info.value)
