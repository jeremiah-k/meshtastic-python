"""Additional edge case tests for BLE client functionality."""

import pytest

try:
    from meshtastic.interfaces.ble.client import BLEClient, get_zombie_thread_count
    from meshtastic.interfaces.ble.constants import BLECLIENT_ERROR_ASYNC_TIMEOUT
except ImportError:
    pytest.skip("BLE dependencies not available", allow_module_level=True)


@pytest.mark.unit
def test_bleclient_discovery_mode_without_address():
    """BLEClient should support discovery-only mode when initialized without an address."""
    client = BLEClient(address=None)
    try:
        assert client.address is None
        assert client.bleak_client is None
        assert not client.isConnected()
    finally:
        client.close()


@pytest.mark.unit
def test_bleclient_isConnected_handles_missing_bleak_client():
    """IsConnected should return False when bleak_client is None."""
    client = BLEClient(address=None)
    try:
        assert not client.isConnected()
    finally:
        client.close()


@pytest.mark.unit
def test_bleclient_is_connected_alias():
    """is_connected should be an alias for isConnected."""
    client = BLEClient(address=None)
    try:
        assert client.is_connected() == client.isConnected()
    finally:
        client.close()


@pytest.mark.unit
def test_bleclient_close_is_idempotent():
    """close() should be idempotent and safe to call multiple times."""
    client = BLEClient(address=None)
    client.close()
    client.close()  # Should not raise
    client.close()  # Should not raise


@pytest.mark.unit
def test_bleclient_context_manager():
    """BLEClient should work as a context manager."""
    with BLEClient(address=None) as client:
        assert client is not None
    # Client should be closed after exiting context


@pytest.mark.unit
def test_bleclient_error_class_exists():
    """BLEClient should have a BLEError exception class."""
    assert hasattr(BLEClient, "BLEError")
    assert issubclass(BLEClient.BLEError, Exception)


@pytest.mark.unit
def test_bleclient_operations_require_initialized_client():
    """BLEClient operations should raise BLEError when bleak_client is not initialized."""
    client = BLEClient(address=None)
    try:
        with pytest.raises(
            BLEClient.BLEError, match="Cannot connect: BLE client not initialized"
        ):
            client.connect()

        with pytest.raises(
            BLEClient.BLEError, match="Cannot disconnect: BLE client not initialized"
        ):
            client.disconnect()

        with pytest.raises(
            BLEClient.BLEError, match="Cannot read: BLE client not initialized"
        ):
            client.read_gatt_char("uuid")

        with pytest.raises(
            BLEClient.BLEError, match="Cannot write: BLE client not initialized"
        ):
            client.write_gatt_char("uuid", b"data")

        with pytest.raises(
            BLEClient.BLEError, match="Cannot pair: BLE client not initialized"
        ):
            client.pair()

        with pytest.raises(
            BLEClient.BLEError, match="Cannot get services: BLE client not initialized"
        ):
            client._get_services()

        with pytest.raises(
            BLEClient.BLEError, match="Cannot start notify: BLE client not initialized"
        ):
            client.start_notify("uuid", lambda *args: None)

        with pytest.raises(
            BLEClient.BLEError, match="Cannot stop notify: BLE client not initialized"
        ):
            client.stopNotify("uuid")
    finally:
        client.close()


@pytest.mark.unit
def test_bleclient_has_characteristic_returns_false_without_client():
    """has_characteristic should return False when bleak_client is None."""
    client = BLEClient(address=None)
    try:
        assert not client.has_characteristic("some-uuid")
    finally:
        client.close()


@pytest.mark.unit
def test_bleclient_stop_notify_alias():
    """stop_notify should be an alias for stopNotify."""
    client = BLEClient(address=None)
    try:
        # Both should raise the same error
        with pytest.raises(BLEClient.BLEError, match="Cannot stop notify"):
            client.stopNotify("uuid")
        with pytest.raises(BLEClient.BLEError, match="Cannot stop notify"):
            client.stop_notify("uuid")
    finally:
        client.close()


@pytest.mark.unit
def test_bleclient_operations_fail_when_closed():
    """Operations should fail with clear error when client is closed."""
    client = BLEClient(address=None, log_if_no_address=False)
    client.close()

    # Create a simple coroutine to test
    async def dummy_coro():
        return "result"

    with pytest.raises(
        BLEClient.BLEError, match="Cannot schedule operation: BLE client is closed"
    ):
        client._async_await(dummy_coro())


@pytest.mark.unit
def test_get_zombie_thread_count_function_exists():
    """get_zombie_thread_count should be accessible from client module."""
    count = get_zombie_thread_count()
    assert isinstance(count, int)
    assert count >= 0


@pytest.mark.unit
def test_bleclient_async_await_static_method():
    """BLEClient should have _with_timeout static method."""
    assert hasattr(BLEClient, "_with_timeout")
    assert callable(BLEClient._with_timeout)


@pytest.mark.unit
def test_bleclient_error_constant():
    """Verify BLECLIENT_ERROR_ASYNC_TIMEOUT constant is properly defined."""
    assert BLECLIENT_ERROR_ASYNC_TIMEOUT == "Async operation timed out"


@pytest.mark.unit
def test_bleclient_discover_method_exists():
    """BLEClient should have discover and _discover methods."""
    client = BLEClient(address=None, log_if_no_address=False)
    try:
        assert hasattr(client, "discover")
        assert hasattr(client, "_discover")
        assert callable(client.discover)
        assert callable(client._discover)
    finally:
        client.close()


@pytest.mark.unit
def test_bleclient_async_run_alias():
    """async_run should be an alias for _async_run."""
    client = BLEClient(address=None, log_if_no_address=False)
    try:

        async def dummy():
            return 42

        # Both methods should exist
        assert hasattr(client, "_async_run")
        assert hasattr(client, "async_run")

        # Since client is closed, both should fail the same way
        client.close()
        with pytest.raises(BLEClient.BLEError, match="Cannot schedule operation"):
            client._async_run(dummy())
        with pytest.raises(BLEClient.BLEError, match="Cannot schedule operation"):
            client.async_run(dummy())
    finally:
        if not getattr(client, "_closed", False):
            client.close()


@pytest.mark.unit
def test_bleclient_async_await_alias():
    """async_await should be a public alias for _async_await."""
    client = BLEClient(address=None, log_if_no_address=False)
    try:
        assert hasattr(client, "_async_await")
        assert hasattr(client, "async_await")
        assert callable(client.async_await)
    finally:
        client.close()
