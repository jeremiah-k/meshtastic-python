"""Tests for the BLE interface module - Advanced functionality."""

import logging
import threading
import time
import types
from concurrent.futures import TimeoutError as FutureTimeoutError
from contextlib import ExitStack, contextmanager
from queue import Queue
from typing import Iterator, List, Optional, Tuple, cast
from unittest.mock import MagicMock, patch

import pytest
from bleak.exc import BleakError

# Import common fixtures
from test_ble_interface_fixtures import DummyClient, _build_interface

# Import meshtastic modules for use in tests
import meshtastic.ble_interface as ble_mod
import meshtastic.mesh_interface as mesh_iface_module
from meshtastic.ble_interface import (
    FROMNUM_UUID,
    LEGACY_LOGRADIO_UUID,
    LOGRADIO_UUID,
    BLEInterface,
)
from meshtastic.interfaces.ble.client import BLEClient
from meshtastic.interfaces.ble.exceptions import BLEError
from meshtastic.protobuf import mesh_pb2


def _get_connect_stub_calls(iface: BLEInterface) -> List[Optional[str]]:
    """
    Retrieve the test-only connect call tracking list attached to an interface.
    """
    return cast(List[Optional[str]], getattr(iface, "_connect_stub_calls", []))


def test_log_notification_registration_missing_characteristics(monkeypatch):
    """Test that log notification registration handles missing characteristics gracefully."""
    # UUID constants already imported at top as ble_mod.FROMNUM_UUID, ble_mod.LEGACY_LOGRADIO_UUID, ble_mod.LOGRADIO_UUID

    class MockClientWithoutLogChars(DummyClient):
        """Mock client that doesn't have log characteristics."""

        def __init__(self):
            """
            Initialize a mock BLE client exposing only the FROMNUM characteristic.
            
            Creates an empty list to record start_notify calls and a characteristic presence map
            containing only FROMNUM_UUID set to True.
            
            Attributes:
                start_notify_calls (list): Records (uuid, handler) pairs passed to start_notify.
                has_characteristic_map (dict): Maps characteristic UUIDs to booleans; contains only
                    FROMNUM_UUID: True to indicate availability of that characteristic.
            """
            super().__init__()
            self.start_notify_calls = []
            self.has_characteristic_map = {
                FROMNUM_UUID: True,  # Only have the critical one
            }

        def has_characteristic(self, uuid):
            """
            Determine whether the client reports support for a characteristic UUID.

            Parameters
            ----------
                uuid (str | uuid.UUID): The characteristic UUID to check.

            Returns
            -------
                bool: `True` if the UUID is present in the client's characteristic map, `False` otherwise.

            """
            return self.has_characteristic_map.get(uuid, False)

        def start_notify(self, *_args, **_kwargs):
            """
            Record a BLE notification registration request by appending the (uuid, handler) pair to self.start_notify_calls.
            
            If at least two positional arguments are provided, the first is treated as the characteristic UUID and the second as the handler; additional positional or keyword arguments are ignored.
            """
            # Extract uuid and handler from args if available
            if len(_args) >= 2:
                uuid, handler = _args[0], _args[1]
                self.start_notify_calls.append((uuid, handler))

    client = MockClientWithoutLogChars()
    iface = _build_interface(monkeypatch, client)

    # Call _register_notifications - should not fail even without log characteristics
    iface._register_notifications(client)

    # Verify that only FROMNUM notification was registered
    registered_uuids = [call[0] for call in client.start_notify_calls]

    assert FROMNUM_UUID in registered_uuids, (
        "FROMNUM notification should still be registered"
    )
    assert LEGACY_LOGRADIO_UUID not in registered_uuids, (
        "Legacy log notification should not be registered when characteristic is missing"
    )
    assert LOGRADIO_UUID not in registered_uuids, (
        "Current log notification should not be registered when characteristic is missing"
    )

    iface.close()


def test_receive_loop_handles_decode_error(monkeypatch, caplog):
    """Test that the receive loop handles DecodeError gracefully without closing."""
    # logging, threading, and time already imported at top
    # FROMRADIO_UUID already imported at top as ble_mod.FROMRADIO_UUID

    caplog.set_level(logging.WARNING)

    class MockClient(DummyClient):
        """Mock client that returns invalid protobuf data to trigger DecodeError."""

        def read_gatt_char(self, *_args, **_kwargs) -> bytes:
            """
            Provide test GATT characteristic payloads, producing a malformed FromRadio protobuf when requested.
            
            If the first positional argument equals ble_mod.FROMRADIO_UUID, return deliberately malformed protobuf bytes to simulate a decode error; for any other UUID or when no UUID is provided, return empty bytes.
            
            Returns:
                bytes: Malformed protobuf bytes for ble_mod.FROMRADIO_UUID, empty bytes otherwise.
            """
            # Extract uuid from args if available
            if _args and _args[0] == ble_mod.FROMRADIO_UUID:
                return b"invalid-protobuf-data"
            return b""

    client = MockClient()
    iface = _build_interface(monkeypatch, client)

    close_called = threading.Event()
    original_close = iface.close

    def mock_close(original_close=original_close, close_called=close_called):
        """
        Set the provided close_called event and then invoke the original close function.
        
        Sets the `close_called` event to notify waiters that close was invoked, then calls `original_close()`.
        """
        close_called.set()
        original_close()

    monkeypatch.setattr(iface, "close", mock_close)

    # Start the receive thread
    iface._want_receive = True

    # Set up the client
    with iface._state_lock:
        iface.client = client

    # Trigger the receive loop to process the bad data
    iface._read_trigger.set()

    # Give the thread time to run: poll for expected log
    for _ in range(50):
        if "Failed to parse FromRadio packet, discarding:" in caplog.text:
            break
        time.sleep(0.02)

    # Assert that the graceful handling occurred
    assert "Failed to parse FromRadio packet, discarding:" in caplog.text
    assert not close_called.is_set(), "close() should not be called for a DecodeError"

    # Clean up
    iface._want_receive = False
    iface.close()


def test_auto_reconnect_behavior(monkeypatch, caplog):
    """Test auto_reconnect functionality when disconnection occurs."""
    _ = caplog  # Mark as unused
    # time and meshtastic.mesh_interface already imported at top

    # Track published events
    published_events = []

    def _capture_events(topic, **kwargs):
        """
        Record a pub/sub event for test inspection by appending it to the module-level published_events list.
        
        Parameters:
            topic (str): Event topic name.
            **kwargs: Event payload key/value pairs stored as a dict.
        
        Details:
            Appends a tuple (topic, kwargs) to `published_events`.
        """
        published_events.append((topic, kwargs))

    # Create a fresh pub mock for this test
    fresh_pub = types.SimpleNamespace(
        subscribe=lambda *_args, **_kwargs: None,
        sendMessage=_capture_events,
        AUTO_TOPIC=None,
    )
    monkeypatch.setattr(mesh_iface_module, "pub", fresh_pub)
    monkeypatch.setattr(
        mesh_iface_module.publishingThread,
        "queueWork",
        lambda callback: callback() if callback else None,
    )

    # Create a client that can simulate disconnection
    client = DummyClient()

    # Build interface with auto_reconnect=True
    iface = _build_interface(monkeypatch, client)
    iface.auto_reconnect = True
    assert _get_connect_stub_calls(iface) == ["dummy"], (
        "Initial connect should occur on instantiation"
    )

    # Track if close() was called
    close_called = []
    original_close = iface.close

    def _track_close():
        """
        Record that the wrapper close function was invoked and then call the original close.
        
        This appends an entry to the surrounding `close_called` list to record the invocation.
        
        Returns:
            The value returned by the original close function.
        """
        close_called.append(True)
        return original_close()

    monkeypatch.setattr(iface, "close", _track_close)

    # Simulate disconnection by calling _on_ble_disconnect directly
    # This simulates what happens when bleak calls the disconnected callback
    disconnect_client = iface.client
    iface._on_ble_disconnect(disconnect_client.bleak_client)

    # Allow time for auto-reconnect thread to run
    for _ in range(50):
        if len(_get_connect_stub_calls(iface)) >= 2 and iface.client is client:
            break
        time.sleep(0.01)

    # Assertions
    # 1. BLEInterface.close() should NOT be called when auto_reconnect=True
    assert len(close_called) == 0, (
        "close() should not be called when auto_reconnect=True"
    )

    # 2. Connection status event should be published with connected=False
    disconnect_events = [
        (topic, kw)
        for topic, kw in published_events
        if topic == "meshtastic.connection.status" and kw.get("connected") is False
    ]
    assert len(disconnect_events) == 1, (
        f"Expected exactly one disconnect event, got {len(disconnect_events)}"
    )

    # 3. Auto-reconnect should have attempted a reconnect and restored the client
    assert len(_get_connect_stub_calls(iface)) >= 2, (
        f"Expected at least 2 connect calls, got {len(_get_connect_stub_calls(iface))}"
    )
    assert iface.client is client, (
        "client should be restored after successful auto-reconnect"
    )

    # Simulate config completion to publish connected=True and verify it was emitted
    iface.isConnected.clear()
    monkeypatch.setattr(iface, "_startHeartbeat", lambda: None)
    iface._connected()
    reconnect_events = [
        (topic, kw)
        for topic, kw in published_events
        if topic == "meshtastic.connection.status" and kw.get("connected") is True
    ]
    assert len(reconnect_events) == 1, (
        f"Expected exactly one reconnect event, got {len(reconnect_events)}"
    )

    # 4. Ensure disconnect flag resets for future disconnect handling
    assert not iface._disconnect_notified, (
        "_disconnect_notified should reset after auto-reconnect"
    )

    # 5. Receive thread should remain alive, waiting for new connection
    # Check that _want_receive is still True and receive thread is alive
    assert iface._want_receive is True, (
        "_want_receive should remain True for auto_reconnect"
    )
    assert iface._receiveThread is not None, "receive thread should still exist"
    # Wait up to ~1s for receive thread to be alive
    for _ in range(100):
        t = getattr(iface, "_receiveThread", None)
        if t and t.is_alive():
            break
        time.sleep(0.01)
    assert iface._receiveThread is not None, "receive thread should still exist"
    assert iface._receiveThread.is_alive(), "receive thread should remain alive"

    # Clean up
    iface.auto_reconnect = False  # Disable auto_reconnect for proper cleanup
    iface.close()


def test_send_to_radio_specific_exceptions(monkeypatch, caplog):
    """Test that sendToRadio handles specific exceptions correctly."""
    # logging already imported at top
    # BleakError already imported at top as ble_mod.BleakError, BLEInterface

    # Set logging level to DEBUG to capture debug messages
    caplog.set_level(logging.DEBUG)

    class ExceptionClient(DummyClient):
        """Mock client that raises exceptions during write operations."""

        def __init__(self, exception_type):
            """
            Construct an ExceptionClient that raises a specified exception type during BLE operations.
            
            Parameters:
                exception_type (type): Exception class that the client's BLE methods will raise when invoked.
            """
            super().__init__()
            self.exception_type = exception_type

        def write_gatt_char(self, *_args, **_kwargs):
            """
            Simulate a failed GATT characteristic write by raising the configured exception.

            Raises:
                Exception: An instance of `self.exception_type` with the message "write failed".
            """
            raise self.exception_type("write failed")  # noqa: TRY003 - aids targeted assertions

    # Test BleakError specifically
    client = ExceptionClient(BleakError)
    iface = _build_interface(monkeypatch, client)

    # Create a mock ToRadio message with actual data to ensure it's not empty
    # mesh_pb2 already imported at top

    to_radio = mesh_pb2.ToRadio()
    to_radio.packet.decoded.payload = b"test_data"

    # This should raise BLEInterface.BLEError
    with pytest.raises(BLEInterface.BLEError) as exc_info:
        iface._sendToRadioImpl(to_radio)

    assert "Error writing BLE" in str(exc_info.value)
    assert (
        "Error during write operation: BleakError" in caplog.text
        or "Error during write operation: _StubBleakError" in caplog.text
    )

    # Clear caplog for next test
    caplog.clear()
    iface.close()

    # Test RuntimeError
    client2 = ExceptionClient(RuntimeError)
    iface2 = _build_interface(monkeypatch, client2)

    with pytest.raises(BLEInterface.BLEError) as exc_info:
        iface2._sendToRadioImpl(to_radio)

    assert "Error writing BLE" in str(exc_info.value)
    assert "Error during write operation: RuntimeError" in caplog.text

    # Clear caplog for next test
    caplog.clear()
    iface2.close()

    # Test OSError
    client3 = ExceptionClient(OSError)
    iface3 = _build_interface(monkeypatch, client3)

    with pytest.raises(BLEInterface.BLEError) as exc_info:
        iface3._sendToRadioImpl(to_radio)

    assert "Error writing BLE" in str(exc_info.value)
    assert "Error during write operation: OSError" in caplog.text

    iface3.close()


def test_rapid_connect_disconnect_stress_test(monkeypatch, caplog):
    """Test rapid connect/disconnect cycles to validate thread-safety and reconnect logic."""
    _ = monkeypatch  # Mark as unused
    # logging, threading, and time already imported at top
    # MagicMock, patch already imported at top

    # BLEClient and BLEInterface already imported at top as ble_mod.BLEClient, ble_mod.BLEInterface

    # Set logging level to DEBUG to capture all messages
    caplog.set_level(logging.DEBUG)

    # Mock device for testing
    mock_device = MagicMock()
    mock_device.address = "00:11:22:33:44:55"
    mock_device.name = "Test Device"

    class MockBleakRootClient:
        """Mock BleakRootClient that tracks operations for stress testing."""

        def __init__(self):
            """
            Create a simulated BLE client and initialize its internal counters and state.
            
            Attributes:
                connect_count (int): Number of times connect() has been invoked.
                disconnect_count (int): Number of times disconnect() has been invoked.
                address (str): Mock Bluetooth address used to identify the client.
                _should_fail_connect (bool): If True, simulated connect attempts will raise an error.
            """
            self.connect_count = 0
            self.disconnect_count = 0
            self.address = "00:11:22:33:44:55"
            self._should_fail_connect = False

        def connect(self, *_args, **_kwargs):
            """
            Simulate connecting the mock client, recording the attempt.
            
            Increments the client's connect_count and returns the client. If the client is configured to fail, raises a RuntimeError.
            
            Returns:
                self: The mock client instance with connect_count incremented.
            
            Raises:
                RuntimeError: If `self._should_fail_connect` is True.
            """
            if self._should_fail_connect:
                raise RuntimeError("connect fail")  # noqa: TRY003
            self.connect_count += 1
            return self

        def is_connected(self):
            """
            Indicates the mock client's connection state.
            
            Returns:
                True: Always returns `True`, indicating the client is connected.
            """
            return True

        def disconnect(self, *_args, **_kwargs):
            """
            Record a disconnect attempt on this mock client.

            This method accepts any positional and keyword arguments for call-site compatibility; all arguments are ignored. It increments the `disconnect_count` attribute by 1.
            """
            self.disconnect_count += 1

        def start_notify(self, *_args, **_kwargs):
            """
            Act as a no-op notification registration stub that accepts any positional and keyword arguments.
            
            This method intentionally does nothing and is safe to call with arbitrary parameters when notification handling is not required.
            """

        def stop_notify(self, *_args, **_kwargs):
            """
            Compatibility stub that accepts any positional and keyword arguments and performs no action.
            
            Provided so code can call the BLE client's stop_notify method in tests without side effects or errors when a real client is not present.
            """

    class StressTestClient(BLEClient):
        """Mock client that simulates rapid connect/disconnect cycles."""

        def __init__(self):  # pylint: disable=super-init-not-called
            # Don't call super().__init__() to avoid creating real event loop
            """
            Create a mock BLE root client used by tests that simulates a Bleak client and connection state.

            Attributes:
                bleak_client: Mock underlying bleak client instance used to emulate BLE operations.
                connect_count: Integer count of successful connect attempts.
                disconnect_count: Integer count of disconnect attempts.
                is_connected_result: Boolean returned by is_connected checks to simulate connection state.
                _should_fail_connect: When True, connect attempts should be simulated as failing.
                _eventLoop, _eventThread: Placeholders to suppress event-loop related warnings during tests.
            """
            self.bleak_client = MockBleakRootClient()
            self.connect_count = 0
            self.disconnect_count = 0
            self.is_connected_result = True
            self._should_fail_connect = False
            # Add mock event loop attributes to prevent warnings
            self._eventLoop = None
            self._eventThread = None

        def connect(self, *_args, **_kwargs):
            """
            Simulate connecting the mock client, recording the attempt.
            
            Increments the client's connect_count and returns the client. If the client is configured to fail, raises a RuntimeError.
            
            Returns:
                self: The mock client instance with connect_count incremented.
            
            Raises:
                RuntimeError: If `self._should_fail_connect` is True.
            """
            if self._should_fail_connect:
                raise RuntimeError("connect fail")  # noqa: TRY003
            self.connect_count += 1
            return self

        def is_connected(self):
            """
            Report whether the mock client is currently configured as connected.
            
            Returns:
                bool: `True` if the mock client is configured as connected, `False` otherwise.
            """
            return self.is_connected_result

        def disconnect(self, *_args, **_kwargs):
            """
            Record a disconnect attempt on this mock client.

            This method accepts any positional and keyword arguments for call-site compatibility; all arguments are ignored. It increments the `disconnect_count` attribute by 1.
            """
            self.disconnect_count += 1

        def start_notify(self, *_args, **_kwargs):
            """
            Act as a no-op notification registration stub that accepts any positional and keyword arguments.
            
            This method intentionally does nothing and is safe to call with arbitrary parameters when notification handling is not required.
            """

        def stop_notify(self, *_args, **_kwargs):
            """
            Compatibility stub that accepts any positional and keyword arguments and performs no action.
            
            Provided so code can call the BLE client's stop_notify method in tests without side effects or errors when a real client is not present.
            """

        def close(self):
            """
            No-op close method used in tests to avoid interacting with the event loop.

            This intentionally performs no action so that calling `close()` on a mock client does not trigger
            event-loop side effects or errors during unit tests.
            """

    @contextmanager
    def create_interface_with_auto_reconnect() -> Iterator[Tuple[BLEInterface, "StressTestClient"]]:
        """
        Create a BLEInterface preconfigured for stress testing with auto-reconnect enabled.
        
        This context-managed generator yields a BLEInterface whose device discovery and connect behavior are patched to return a mock device and attach a StressTestClient, records connection attempts in iface._connect_stub_calls, and ensures the interface is closed on exit.
        
        Returns:
            Iterator[Tuple[BLEInterface, StressTestClient]]: Yields tuples of (iface, client) where `iface` is the configured BLEInterface and `client` is the attached StressTestClient.
        """

        outer_client = StressTestClient()
        connect_calls: list = []

        stack = ExitStack()

        # Mock the scan method to return our test device
        stack.enter_context(
            patch.object(BLEInterface, "scan", return_value=[mock_device])
        )

        def _patched_connect(
            self: BLEInterface,
            address: Optional[str] = None,
        ) -> "StressTestClient":
            """
            Attach a StressTestClient to the interface and record the connection attempt.
            
            Records the attempted connection address, ensures the StressTestClient is connected and assigned to self.client, clears the _disconnect_notified flag, and sets _reconnected_event if present.
            
            Parameters:
                address (Optional[str]): Address used for the connection; appended to the test's connect call trace.
            
            Returns:
                StressTestClient: The client instance attached to the interface.
            """
            connect_calls.append(address)
            outer_client.connect()
            self.client = outer_client
            self._disconnect_notified = False
            if hasattr(self, "_reconnected_event"):
                self._reconnected_event.set()
            return outer_client

        stack.enter_context(patch.object(BLEInterface, "connect", _patched_connect))

        iface = None
        try:
            iface = BLEInterface(
                address=None,  # Required positional argument
                noProto=True,
                auto_reconnect=True,
            )
            iface._connect_stub_calls = connect_calls
            client = cast("StressTestClient", iface.client)
            yield iface, client
        finally:
            if iface is not None:
                try:
                    iface.close()
                except Exception as e:  # noqa: BLE001 - teardown logging only
                    logging.debug(
                        "Error closing stress-test interface during cleanup: %s: %s",
                        type(e).__name__,
                        e,
                    )
            stack.close()

    # Test 1: Rapid disconnect callbacks
    with create_interface_with_auto_reconnect() as (iface, client):

        def simulate_rapid_disconnects():
            """
            Simulate a burst of rapid BLE disconnections to exercise reconnect and disconnect handling.
            
            Sends ten disconnection notifications to the test interface approximately 0.01 seconds apart to stress reconnect logic and ensure the interface handles rapid repeated disconnect events without crashing.
            """
            for _ in range(10):
                iface._on_ble_disconnect(client.bleak_client)
                time.sleep(0.01)  # Very short delay between disconnects

        # Start rapid disconnect simulation in a separate thread
        disconnect_thread = threading.Thread(target=simulate_rapid_disconnects)
        disconnect_thread.start()
        disconnect_thread.join()

    assert len(_get_connect_stub_calls(iface)) >= 2, (
        "Auto-reconnect should continue scheduling during rapid disconnects"
    )

    # Test 2: Concurrent connect/disconnect operations
    with create_interface_with_auto_reconnect() as (iface2, client2):

        def _stress_test_disconnects():
            """
            Trigger a burst of simulated BLE disconnects on iface2 to exercise auto-reconnect and disconnect handling.

            Calls iface2._on_ble_disconnect(client2.bleak_client) five times with a 5 millisecond pause between calls.
            Exceptions raised during individual disconnect attempts are suppressed and logged to allow the stress cycle to continue.
            """
            for i in range(5):
                try:
                    iface2._on_ble_disconnect(client2.bleak_client)
                    time.sleep(0.005)
                except (RuntimeError, AttributeError, KeyError) as e:
                    logging.debug(
                        "Stress test disconnect %d: %s: %s", i, type(e).__name__, e
                    )
                except Exception as e:  # noqa: BLE001 - test teardown must be resilient
                    logging.debug(
                        "Unexpected stress test disconnect %d: %s: %s",
                        i,
                        type(e).__name__,
                        e,
                    )

        # Start multiple threads for concurrent operations
        threads = []
        for _ in range(3):
            thread = threading.Thread(target=_stress_test_disconnects)
            threads.append(thread)
            thread.start()

        # Wait for all threads to complete
        for thread in threads:
            thread.join()

        assert len(_get_connect_stub_calls(iface2)) >= 1, (
            "Concurrent disconnects should still trigger reconnect attempts"
        )

    # Test 3: Stress test with connection failures
    with create_interface_with_auto_reconnect() as (iface3, client3):
        client3._should_fail_connect = True

        # Simulate disconnects that trigger reconnection attempts
        # Exceptions are expected and suppressed to continue stress testing
        for _ in range(5):
            try:
                iface3._on_ble_disconnect(client3.bleak_client)
                time.sleep(0.01)
            except Exception as e:  # noqa: BLE001 - expected during failure stress
                logging.debug(
                    "Expected failure during stress reconnect: %s: %s",
                    type(e).__name__,
                    e,
                )

        # Verify graceful handling of connection failures
        assert len(_get_connect_stub_calls(iface3)) >= 1, (
            "Failure path should still schedule reconnect attempts"
        )

    # Verify no critical errors in logs
    critical = [r for r in caplog.records if r.levelno >= logging.CRITICAL]
    assert not critical, (
        f"Critical errors found in logs: {[r.message for r in critical]}"
    )


def test_ble_client_is_connected_exception_handling(monkeypatch, caplog):
    """Test that BLEClient.is_connected handles exceptions gracefully."""
    _ = monkeypatch  # Mark as unused
    # logging already imported at top
    # BLEClient already imported at top as ble_mod.BLEClient

    # Set logging level to DEBUG to capture debug messages
    caplog.set_level(logging.DEBUG)

    class ExceptionBleakClient:
        """Mock bleak client that raises exceptions during connection checks."""

        def __init__(self, exception_type):
            """
            Initialize the test BLE client to simulate BLE failures by causing its BLE-related methods to raise the provided exception type.
            
            Parameters:
                exception_type (type): Exception class that methods on this test client will raise to simulate BLE errors (e.g., BleakError, RuntimeError, OSError).
            """
            self.exception_type = exception_type

        def is_connected(self):
            """
            Simulate a connection-state check that always fails by raising the configured exception.
            
            Raises:
                Exception: An instance of `self.exception_type` raised with the message "conn check failed".
            """
            raise self.exception_type("conn check failed")  # noqa: TRY003

    # Create BLEClient with a mock bleak client that raises exceptions
    ble_client = BLEClient.__new__(BLEClient)
    ble_client.bleak_client = ExceptionBleakClient(AttributeError)
    # Initialize error_handler since __new__ bypasses __init__
    # BLEErrorHandler already imported at top as ble_mod.BLEErrorHandler

    ble_client.error_handler = ble_mod.BLEErrorHandler()

    # Should return False and log debug message when AttributeError occurs
    result = ble_client.is_connected()
    assert result is False
    assert "Unable to read bleak connection state" in caplog.text

    # Clear caplog
    caplog.clear()

    # Test TypeError
    ble_client.bleak_client = ExceptionBleakClient(TypeError)
    result = ble_client.is_connected()
    assert result is False
    assert "Unable to read bleak connection state" in caplog.text

    # Clear caplog
    caplog.clear()

    # Test RuntimeError
    ble_client.bleak_client = ExceptionBleakClient(RuntimeError)
    result = ble_client.is_connected()
    assert result is False
    assert "Unable to read bleak connection state" in caplog.text


def test_ble_client_async_timeout_maps_to_ble_error(monkeypatch):
    """BLEClient.async_await should wrap FutureTimeoutError in BLEInterface.BLEError."""

    # BLEClient and BLEInterface already imported at top as ble_mod.BLEClient, ble_mod.BLEInterface

    client = ble_mod.BLEClient()  # address=None keeps underlying bleak client unset

    class _FakeFuture:
        def __init__(self):
            """
            Create an object that tracks cancellation state and an associated coroutine.

            Attributes:
                cancelled (bool): True if the operation has been cancelled, False otherwise.
                coro (Optional[Coroutine]): The associated coroutine, or None if none is set.
            """
            self.cancelled = False
            self.coro = None

        def result(self, _timeout=None):
            """
            Simulate a timed-out future by always raising FutureTimeoutError.
            
            Raises:
                FutureTimeoutError: Always raised to represent that the future timed out.
            """
            raise FutureTimeoutError()

        def cancel(self):
            """
            Mark the future as cancelled.
            
            Sets the instance's `cancelled` attribute to True so callers can detect that the future was cancelled.
            """
            self.cancelled = True

    fake_future = _FakeFuture()

    def _fake_async_run(coro):
        """
        Associate the given coroutine with the shared fake future used by tests.
        
        Parameters:
            coro (coroutine): Coroutine to attach to the fake future.
        
        Returns:
            fake_future: The shared fake future instance with its `coro` attribute set to `coro`.
        """
        fake_future.coro = coro
        return fake_future

    monkeypatch.setattr(client, "async_run", _fake_async_run)

    with pytest.raises(BLEInterface.BLEError) as excinfo:
        client.async_await(object(), timeout=0.01)

    assert "Async operation timed out" in str(excinfo.value)
    assert fake_future.cancelled is True

    client.close()
    if getattr(fake_future, "coro", None) is not None and hasattr(
        fake_future.coro, "close"
    ):
        fake_future.coro.close()


def test_wait_for_disconnect_notifications_exceptions(monkeypatch, caplog):
    """Test that _wait_for_disconnect_notifications handles exceptions gracefully."""
    # logging already imported at top

    # Set logging level to DEBUG to capture debug messages
    caplog.set_level(logging.DEBUG)

    # Also ensure the logger is configured to capture the actual module logger
    logger = logging.getLogger("meshtastic.ble_interface")
    logger.setLevel(logging.DEBUG)

    client = DummyClient()
    iface = _build_interface(monkeypatch, client)

    # Mock publishingThread to raise RuntimeError
    # meshtastic.ble_interface already imported at top as ble_mod

    class MockPublishingThread:
        """Mock publishingThread that raises RuntimeError in queueWork."""

        def queueWork(self, _callback):
            """
            Simulate a publishing-thread failure by always raising a RuntimeError when invoked.
            
            Parameters:
                _callback (callable): Ignored; provided to match the publishingThread.queueWork signature.
            
            Raises:
                RuntimeError: Always raised with the message "thread error in queueWork".
            """
            raise RuntimeError("thread error in queueWork")  # noqa: TRY003

    monkeypatch.setattr(ble_mod, "publishingThread", MockPublishingThread())

    # Should handle RuntimeError gracefully
    iface._wait_for_disconnect_notifications()
    assert "disconnect notification flush" in caplog.text

    # Clear caplog
    caplog.clear()

    # Mock publishingThread to raise ValueError
    class MockPublishingThread2:
        """Mock publishingThread that raises ValueError in queueWork."""

        def queueWork(self, _callback):
            """
            Indicate the publishing thread is in an invalid state by raising a ValueError.
            
            Raises:
                ValueError: Always raised with message "invalid state".
            """
            raise ValueError("invalid state")  # noqa: TRY003

    monkeypatch.setattr(ble_mod, "publishingThread", MockPublishingThread2())

    # Should handle ValueError gracefully
    iface._wait_for_disconnect_notifications()
    assert "disconnect notification flush" in caplog.text

    iface.close()


def test_drain_publish_queue_exceptions(monkeypatch, caplog):
    """Test that _drain_publish_queue handles exceptions gracefully."""
    # logging, threading, and Queue already imported at top

    # Set logging level to DEBUG to capture debug messages
    caplog.set_level(logging.DEBUG)

    client = DummyClient()
    iface = _build_interface(monkeypatch, client)

    # Create a mock queue with a runnable that raises exceptions
    class ExceptionRunnable:
        """Mock runnable that raises ValueError when called."""

        def __call__(self):
            """
            Simulate a failing callback by always raising ValueError.

            Raises
            ------
                ValueError: Always raised to indicate the mock callback failed during execution.

            """
            raise ValueError("callback failed")  # noqa: TRY003

    mock_queue = Queue()
    mock_queue.put(ExceptionRunnable())

    # Mock publishingThread with the queue
    # meshtastic.ble_interface already imported at top as ble_mod

    class MockPublishingThread:
        """Mock publishingThread with a predefined queue."""

        def __init__(self):
            """
            Create a mock publishing thread and attach an external queue to the instance.
            
            Assigns the external `mock_queue` (from the enclosing scope) to `self.queue` so callers can provide and control the queue of deferred callbacks (commonly used in tests).
            """
            self.queue = mock_queue

        def queueWork(self, _callback):
            """
            Execute a scheduled callback immediately.
            
            Parameters:
                _callback (callable | None): Callable to execute; if `None`, nothing is executed.
            
            Returns:
                The value returned by `_callback`, or `None` if `_callback` is `None`.
            """
            if _callback:
                return _callback()
            return None

    monkeypatch.setattr(ble_mod, "publishingThread", MockPublishingThread())

    # Should handle ValueError gracefully
    flush_event = threading.Event()
    iface._drain_publish_queue(flush_event)
    assert "Error in deferred publish callback" in caplog.text

    iface.close()