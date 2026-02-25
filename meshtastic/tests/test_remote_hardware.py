"""Meshtastic unit tests for remote_hardware.py."""

import logging
import re
from unittest.mock import MagicMock, create_autospec

import pytest

from ..mesh_interface import MeshInterface
from ..protobuf import portnums_pb2, remote_hardware_pb2
from ..remote_hardware import WATCH_MASKS_ATTR, RemoteHardwareClient, onGPIOreceive
from ..serial_interface import SerialInterface


@pytest.mark.unit
def test_RemoteHardwareClient(mock_gpio_iface):
    """Test that we can instantiate a RemoteHardwareClient instance."""
    rhw = RemoteHardwareClient(mock_gpio_iface)
    assert rhw.iface == mock_gpio_iface


@pytest.mark.unit
def test_onGPIOreceive(caplog):
    """Test onGPIOreceive."""
    iface = create_autospec(SerialInterface, instance=True)
    packet = {
        "decoded": {"remotehw": {"type": "foo", "gpioValue": "4096", "gpioMask": -1}}
    }
    with caplog.at_level(logging.INFO):
        onGPIOreceive(packet, iface)
        assert re.search(r"Received RemoteHardware", caplog.text)
        assert re.search(r"value=4096", caplog.text)


@pytest.mark.unit
def test_onGPIOreceive_mask_fallback(caplog):
    """Test onGPIOreceive uses packet gpioMask when no tracked mask is available."""
    iface = create_autospec(SerialInterface, instance=True)
    packet = {"decoded": {"remotehw": {"gpioValue": "7", "gpioMask": 7}}}
    with caplog.at_level(logging.DEBUG):
        onGPIOreceive(packet, iface)
        assert re.search(r"Received RemoteHardware", caplog.text)
        assert re.search(r"\bmask[=:\s]+7\b", caplog.text)
        assert re.search(r"value=7", caplog.text)


@pytest.mark.unit
def test_onGPIOreceive_uses_node_watch_mask(caplog):
    """Test onGPIOreceive falls back to tracked per-node watch mask when needed."""
    iface = create_autospec(SerialInterface, instance=True)
    setattr(iface, WATCH_MASKS_ATTR, {"num:16": 7})
    packet = {"from": 16, "decoded": {"remotehw": {"gpioValue": "7"}}}
    with caplog.at_level(logging.DEBUG):
        onGPIOreceive(packet, iface)
        assert re.search(r"Received RemoteHardware", caplog.text)
        assert re.search(r"\bmask[=:\s]+7\b", caplog.text)
        assert re.search(r"value=7", caplog.text)


@pytest.mark.unit
def test_onGPIOreceive_ignores_nondict_decoded() -> None:
    """Test that onGPIOreceive ignores packets with non-dict decoded payloads."""
    iface = create_autospec(SerialInterface, instance=True)
    iface.gotResponse = False
    packet = {"decoded": None}

    onGPIOreceive(packet, iface)

    assert iface.gotResponse is False


@pytest.mark.unit
def test_onGPIOreceive_ignores_nondict_remotehw() -> None:
    """Test that onGPIOreceive ignores packets with non-dict remotehw sections."""
    iface = create_autospec(SerialInterface, instance=True)
    iface.gotResponse = False
    packet = {"decoded": {"remotehw": "not-a-dict"}}

    onGPIOreceive(packet, iface)

    assert iface.gotResponse is False


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
def test_readGPIOs(caplog, mock_gpio_iface):
    """Test readGPIOs."""
    rhw = RemoteHardwareClient(mock_gpio_iface)
    with caplog.at_level(logging.DEBUG):
        rhw.readGPIOs("0x10", 123)
    assert re.search(r"readGPIOs", caplog.text)
    mock_gpio_iface.sendData.assert_called_once()
    args, kwargs = mock_gpio_iface.sendData.call_args
    assert args[1] == "0x10"
    assert args[2] == portnums_pb2.REMOTE_HARDWARE_APP
    payload = args[0]
    assert payload.type == remote_hardware_pb2.HardwareMessage.Type.READ_GPIOS
    assert payload.gpio_mask == 123
    assert kwargs["wantAck"] is True
    assert kwargs["channelIndex"] == rhw.channelIndex
    assert kwargs["wantResponse"] is True
    # readGPIOs relies on pub/sub dispatch for default handling; no explicit callback is expected.
    assert kwargs["onResponse"] is None


@pytest.mark.unit
def test_writeGPIOs(caplog, mock_gpio_iface):
    """Test writeGPIOs."""
    rhw = RemoteHardwareClient(mock_gpio_iface)
    with caplog.at_level(logging.DEBUG):
        rhw.writeGPIOs("0x10", 123, 1)
    assert re.search(r"writeGPIOs", caplog.text)
    mock_gpio_iface.sendData.assert_called_once()
    args, kwargs = mock_gpio_iface.sendData.call_args
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


@pytest.mark.unit
def test_watchGPIOs(caplog, mock_gpio_iface):
    """Test watchGPIOs."""

    rhw = RemoteHardwareClient(mock_gpio_iface)
    with caplog.at_level(logging.DEBUG):
        rhw.watchGPIOs("0x10", 123)
    assert re.search(r"watchGPIOs", caplog.text)
    mock_gpio_iface.sendData.assert_called_once()
    args, kwargs = mock_gpio_iface.sendData.call_args
    assert args[1] == "0x10"
    assert args[2] == portnums_pb2.REMOTE_HARDWARE_APP
    payload = args[0]
    assert payload.type == remote_hardware_pb2.HardwareMessage.Type.WATCH_GPIOS
    assert payload.gpio_mask == 123
    assert kwargs["wantAck"] is True
    assert kwargs["channelIndex"] == rhw.channelIndex
    assert kwargs["wantResponse"] is False
    assert kwargs["onResponse"] is None
    assert getattr(mock_gpio_iface, WATCH_MASKS_ATTR)["num:16"] == 123


@pytest.mark.unit
def test_watchGPIOs_does_not_cache_mask_on_send_failure(mock_gpio_iface):
    """WatchGPIOs should not persist a watch mask when sendData fails."""
    rhw = RemoteHardwareClient(mock_gpio_iface)
    mock_gpio_iface.sendData.side_effect = RuntimeError("send failed")

    with pytest.raises(RuntimeError, match="send failed"):
        rhw.watchGPIOs("0x10", 123)

    watch_masks = getattr(mock_gpio_iface, WATCH_MASKS_ATTR, {})
    assert "num:16" not in watch_masks


@pytest.mark.unit
def test_send_hardware_no_nodeid(mock_gpio_iface):
    """Test sending no nodeid to _send_hardware()."""
    rhw = RemoteHardwareClient(mock_gpio_iface)
    with pytest.raises(
        MeshInterface.MeshInterfaceError, match="Must use a destination node ID"
    ):
        rhw._send_hardware(None, None)  # type: ignore[arg-type]
