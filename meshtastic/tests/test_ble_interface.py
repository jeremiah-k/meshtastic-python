"""Meshtastic test for ble_interface.py"""
from unittest.mock import MagicMock, AsyncMock, patch, call
import pytest
import asyncio
import threading
import time

from meshtastic.ble_interface import BLEInterface
from bleak.exc import BleakError


@pytest.mark.ble
def test_ble_interface_init_no_address():
    """Test that we can instantiate a BLEInterface without an address."""
    iface = BLEInterface()
    assert iface
    assert not iface._is_connected
    assert not iface._closed
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


@pytest.mark.ble
def test_from_num_handler_closed_interface():
    """Test that from_num_handler does nothing when interface is closed."""
    iface = BLEInterface()
    iface._closed = True
    iface._event_loop = MagicMock()
    
    # Should not create a task when interface is closed
    iface.from_num_handler(None, b'\x01\x00\x00\x00')
    iface._event_loop.create_task.assert_not_called()
    iface.close()


@pytest.mark.ble
def test_from_num_handler_with_lock(mocker):
    """Test that from_num_handler always schedules read tasks."""
    iface = BLEInterface()
    iface._closed = False
    iface._event_loop = MagicMock()
    iface._read_lock = MagicMock()
    iface._read_lock.locked.return_value = True
    
    # Should always create a task regardless of lock state
    iface.from_num_handler(None, b'\x01\x00\x00\x00')
    iface._event_loop.create_task.assert_called_once()
    
    # Reset mock and test again with lock not held
    iface._event_loop.create_task.reset_mock()
    iface._read_lock.locked.return_value = False
    iface.from_num_handler(None, b'\x01\x00\x00\x00')
    iface._event_loop.create_task.assert_called_once()
    iface.close()


@pytest.mark.ble
def test_run_coro_closed_interface():
    """Test that _run_coro returns None when interface is closed."""
    iface = BLEInterface()
    iface._closed = True
    iface._event_loop = MagicMock()
    
    result = iface._run_coro(asyncio.sleep(0))
    assert result is None
    iface.close()


@pytest.mark.ble
def test_run_coro_event_loop_not_running():
    """Test that _run_coro returns None when event loop is not running."""
    iface = BLEInterface()
    iface._closed = False
    iface._event_loop = MagicMock()
    iface._event_loop.is_running.return_value = False
    
    result = iface._run_coro(asyncio.sleep(0))
    assert result is None
    iface.close()


@pytest.mark.ble
def test_run_coro_timeout_error(mocker):
    """Test that _run_coro handles timeout errors properly."""
    iface = BLEInterface()
    iface._closed = False
    iface._event_loop = MagicMock()
    iface._event_loop.is_running.return_value = True
    
    # Mock future that raises TimeoutError
    mock_future = MagicMock()
    mock_future.result.side_effect = asyncio.TimeoutError("Timeout")
    
    with patch('asyncio.run_coroutine_threadsafe', return_value=mock_future):
        with pytest.raises(BLEInterface.BLEError, match="Operation timed out"):
            iface._run_coro(asyncio.sleep(0), timeout=1.0)
    iface.close()


@pytest.mark.ble
def test_run_coro_cancelled_error(mocker):
    """Test that _run_coro handles cancelled errors properly."""
    iface = BLEInterface()
    iface._closed = False
    iface._event_loop = MagicMock()
    iface._event_loop.is_running.return_value = True
    
    # Mock future that raises CancelledError
    mock_future = MagicMock()
    mock_future.result.side_effect = asyncio.CancelledError("Cancelled")
    
    with patch('asyncio.run_coroutine_threadsafe', return_value=mock_future):
        result = iface._run_coro(asyncio.sleep(0), timeout=1.0)
        assert result is None
    iface.close()


@pytest.mark.ble
def test_run_coro_general_exception(mocker):
    """Test that _run_coro handles general exceptions properly."""
    iface = BLEInterface()
    iface._closed = False
    iface._event_loop = MagicMock()
    iface._event_loop.is_running.return_value = True
    
    # Mock future that raises general exception
    mock_future = MagicMock()
    mock_future.result.side_effect = RuntimeError("General error")
    
    with patch('asyncio.run_coroutine_threadsafe', return_value=mock_future):
        with pytest.raises(BLEInterface.BLEError, match="Failed to execute coroutine"):
            iface._run_coro(asyncio.sleep(0), timeout=1.0)
    iface.close()


@pytest.mark.ble
def test_handle_disconnected():
    """Test the _handle_disconnected method."""
    iface = BLEInterface()
    iface._closed = False
    iface._is_connected = True
    
    iface._handle_disconnected()
    
    assert not iface._is_connected
    iface.close()


@pytest.mark.ble
def test_handle_disconnected_when_closed():
    """Test that _handle_disconnected doesn't log when interface is closed."""
    iface = BLEInterface()
    iface._closed = True
    iface._is_connected = True
    
    # Should not log anything when closed
    iface._handle_disconnected()
    
    assert not iface._is_connected
    iface.close()


@pytest.mark.ble
def test_receive_from_radio_impl_closed():
    """Test that _receiveFromRadioImpl returns early when interface is closed."""
    async def test_impl():
        iface = BLEInterface()
        iface._closed = True
        iface.client = MagicMock()
        
        # Should return immediately without doing anything
        await iface._receiveFromRadioImpl()
        iface.client.read_gatt_char.assert_not_called()
        iface.close()
    
    # Run the async test
    asyncio.run(test_impl())


@pytest.mark.ble
def test_receive_from_radio_impl_no_client():
    """Test that _receiveFromRadioImpl returns early when client is None."""
    async def test_impl():
        iface = BLEInterface()
        iface._closed = False
        iface.client = None
        
        # Should return immediately without doing anything
        await iface._receiveFromRadioImpl()
        iface.close()
    
    # Run the async test
    asyncio.run(test_impl())


@pytest.mark.ble
def test_receive_from_radio_impl_successful_read(mocker):
    """Test that _receiveFromRadioImpl handles successful reads."""
    async def test_impl():
        iface = BLEInterface()
        iface._closed = False
        iface.client = MagicMock()
        iface.client.read_gatt_char = AsyncMock(return_value=b'test_data')
        iface._handleFromRadio = MagicMock()
        
        await iface._receiveFromRadioImpl()
        
        iface.client.read_gatt_char.assert_called_once_with('2c55e69e-4993-11ed-b878-0242ac120002')
        iface._handleFromRadio.assert_called_once_with(b'test_data')
        iface.close()
    
    # Run the async test
    asyncio.run(test_impl())


@pytest.mark.ble
def test_receive_from_radio_impl_retry_logic(mocker):
    """Test that _receiveFromRadioImpl retries on empty reads."""
    async def test_impl():
        iface = BLEInterface()
        iface._closed = False
        iface.client = MagicMock()
        # Return empty data first, then valid data
        iface.client.read_gatt_char = AsyncMock(side_effect=[b'', b'test_data'])
        iface._handleFromRadio = MagicMock()
        
        with patch('asyncio.sleep', new_callable=AsyncMock) as mock_sleep:
            await iface._receiveFromRadioImpl()
            
            # Should have been called twice
            assert iface.client.read_gatt_char.call_count == 2
            mock_sleep.assert_called_once_with(0.1)
            iface._handleFromRadio.assert_called_once_with(b'test_data')
        iface.close()
    
    # Run the async test
    asyncio.run(test_impl())


@pytest.mark.ble
def test_receive_from_radio_impl_bleak_error(mocker):
    """Test that _receiveFromRadioImpl handles BleakError properly."""
    async def test_impl():
        iface = BLEInterface()
        iface._closed = False
        iface.client = MagicMock()
        iface.client.read_gatt_char = AsyncMock(side_effect=BleakError("BLE error"))
        iface.close = MagicMock()
        
        await iface._receiveFromRadioImpl()
        
        iface.close.assert_called_once()
        iface.close()
    
    # Run the async test
    asyncio.run(test_impl())


@pytest.mark.ble
def test_scan():
    """Test the scan method."""
    with patch('meshtastic.ble_interface.BLEInterface._scan_async') as mock_scan_async:
        mock_scan_async.return_value = ['device1', 'device2']
        
        result = BLEInterface.scan()
        
        assert result == ['device1', 'device2']
        mock_scan_async.assert_called_once()


@pytest.mark.ble
def test_connect_already_connected(mocker):
    """Test that connect does nothing when already connected."""
    iface = BLEInterface()
    iface._is_connected = True
    iface._run_coro = MagicMock()
    
    iface.connect(address="test")
    
    # Should not call _run_coro when already connected
    iface._run_coro.assert_not_called()
    iface.close()


@pytest.mark.ble
def test_connect_interface_closed(mocker):
    """Test that connect raises error when interface is closed."""
    iface = BLEInterface()
    iface._closed = True
    
    with pytest.raises(BLEInterface.BLEError, match="Interface is closed"):
        iface.connect(address="test")
    iface.close()
