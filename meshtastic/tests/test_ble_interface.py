"""Meshtastic test for ble_interface.py"""
from unittest.mock import MagicMock
import pytest

from meshtastic.ble_interface import BLEInterface


@pytest.mark.ble
def test_ble_interface_init_no_address():
    """Test that we can instantiate a BLEInterface without an address."""
    iface = BLEInterface()
    assert iface
    iface.close()


@pytest.mark.ble
def test_connect(mocker):
    """Test that we can connect."""
    # We need to mock _run_coro because it will try to run a real coroutine
    mock_run_coro = mocker.patch("meshtastic.ble_interface.BLEInterface._run_coro")
    mock_start_config = mocker.patch("meshtastic.mesh_interface.MeshInterface._startConfig")
    mock_wait_for_config = mocker.patch("meshtastic.mesh_interface.MeshInterface.waitForConfig")

    iface = BLEInterface(noProto=True)
    iface.connect(address="someaddress")

    assert mock_run_coro.call_count == 1
    mock_start_config.assert_called_once()
    mock_wait_for_config.assert_not_called()  # because noProto=True
    iface.close()


@pytest.mark.ble
def test_close():
    """Test the close method."""
    iface = BLEInterface()
    # Mock methods that would be called during close
    iface.client = MagicMock()
    iface.client.is_connected = True
    disconnect_coro = iface.client.disconnect()
    iface._run_coro = MagicMock()
    iface._stop_event_loop = MagicMock()

    iface.close()

    iface._run_coro.assert_called_once_with(disconnect_coro)
    iface._stop_event_loop.assert_called_once()


@pytest.mark.ble
def test_send_to_radio():
    """Test the _sendToRadioImpl method."""
    iface = BLEInterface()
    iface.client = MagicMock()
    iface._run_coro = MagicMock()

    mock_packet = MagicMock()
    mock_packet.SerializeToString.return_value = b"somebytes"
    iface._sendToRadioImpl(mock_packet)
    assert iface._run_coro.call_count == 1
    iface.close()
