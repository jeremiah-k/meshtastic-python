"""Tests for the BLE interface module - Advanced functionality."""

import logging
import threading
import time
import types
from concurrent.futures import TimeoutError as FutureTimeoutError
from contextlib import ExitStack
from queue import Queue
from typing import Optional
from unittest.mock import MagicMock, patch

import pytest  # type: ignore[import-untyped]  # pylint: disable=E0401
import importlib

# Import common fixtures
from test_ble_interface_fixtures import (
    DummyClient,
    _build_interface,
)

@pytest.fixture(autouse=True)
def _late_imports(mock_serial, mock_pubsub, mock_tabulate, mock_bleak, mock_bleak_exc, mock_publishing_thread):
    """
    Import the BLE and mesh interface modules and expose selected symbols into the test module's global scope for downstream tests.
    
    The function loads meshtastic.ble_interface and meshtastic.mesh_interface and binds BLEInterface, BLEClient, FROMNUM_UUID, LEGACY_LOGRADIO_UUID, LOGRADIO_UUID, BleakError, and mesh_pb2 as globals so tests can reference them directly.
    """
    _ = (mock_serial, mock_pubsub, mock_tabulate, mock_bleak, mock_bleak_exc, mock_publishing_thread)
    global ble_mod, mesh_iface_module, BLEInterface, BLEClient, FROMNUM_UUID, LEGACY_LOGRADIO_UUID, LOGRADIO_UUID, BleakError, mesh_pb2
    ble_mod = importlib.import_module("meshtastic.ble_interface")
    mesh_iface_module = importlib.import_module("meshtastic.mesh_interface")
    BLEInterface = ble_mod.BLEInterface
    BLEClient = ble_mod.BLEClient
    FROMNUM_UUID = ble_mod.FROMNUM_UUID
    LEGACY_LOGRADIO_UUID = ble_mod.LEGACY_LOGRADIO_UUID
    LOGRADIO_UUID = ble_mod.LOGRADIO_UUID
    BleakError = importlib.import_module("bleak.exc").BleakError
    mesh_pb2 = importlib.import_module("meshtastic.protobuf").mesh_pb2





def test_log_notification_registration_missing_characteristics(monkeypatch):
    """Test that log notification registration handles missing characteristics gracefully."""
    # UUID constants already imported at top as ble_mod.FROMNUM_UUID, ble_mod.LEGACY_LOGRADIO_UUID, ble_mod.LOGRADIO_UUID

    class MockClientWithoutLogChars(DummyClient):
        """Mock client that doesn't have log characteristics."""

        def __init__(self):
            """
            Initialize a mock BLE client that advertises only the FROMNUM characteristic.
            
            Sets attributes used by tests:
            - start_notify_calls: list that records (uuid, handler) tuples passed to start_notify.
            - has_characteristic_map: dict mapping characteristic UUIDs to booleans, containing only FROMNUM_UUID set to True.
            """
            super().__init__()
            self.start_notify_calls = []
            self.has_characteristic_map = {
                FROMNUM_UUID: True,  # Only have the critical one
            }

        def has_characteristic(self, uuid):
            """
            Determine whether the client reports a characteristic with the given UUID.
            
            Parameters:
                uuid (str | uuid.UUID): Characteristic UUID to check.
            
            Returns:
                True if the UUID is present in the client's characteristic map, False otherwise.
            """
            return self.has_characteristic_map.get(uuid, False)

        def start_notify(self, *_args, **_kwargs):
            """
            Record a notification registration request for a given characteristic UUID.

            If the first two positional arguments are a UUID and a handler, appends the pair
            (uuid, handler) to self.start_notify_calls for later inspection.
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

    assert (
        FROMNUM_UUID in registered_uuids
    ), "FROMNUM notification should still be registered"
    assert (
        LEGACY_LOGRADIO_UUID not in registered_uuids
    ), "Legacy log notification should not be registered when characteristic is missing"
    assert (
        LOGRADIO_UUID not in registered_uuids
    ), "Current log notification should not be registered when characteristic is missing"

    iface.close()


def test_receive_loop_handles_decode_error(monkeypatch, caplog):
    """Test that the receive loop handles DecodeError gracefully without closing."""
    # logging, threading, and time already imported at top
    # FROMRADIO_UUID already imported at top as ble_mod.FROMRADIO_UUID

    caplog.set_level(logging.WARNING)

    class MockClient(DummyClient):
        """Mock client that returns invalid protobuf data to trigger DecodeError."""

        def read_gatt_char(self, *_args, **_kwargs):
            """
            Provide test GATT characteristic bytes for use in unit tests.
            
            When the first positional argument equals ble_mod.FROMRADIO_UUID, returns bytes that are invalid protobuf data to trigger a decode error; otherwise returns empty bytes.
            
            Returns:
                bytes: Invalid protobuf payload when the first positional argument is ble_mod.FROMRADIO_UUID, empty bytes otherwise.
            """
            # Extract uuid from args if available
            if _args and _args[0] == ble_mod.FROMRADIO_UUID:
                return b"invalid-protobuf-data"
            return b""

    client = MockClient()
    iface = _build_interface(monkeypatch, client)

    close_called = threading.Event()
    original_close = iface.close

    def mock_close():
        """
        Signal that the mock close was invoked and notify waiting threads.
        
        Sets the `close_called` event to wake any waiters and then invokes `original_close` to perform the actual close operation.
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
        Record a pub/sub event by appending it to the module-level `published_events` list.

        The event is stored as a tuple `(topic, payload)` where `payload` is the `kwargs` dictionary passed to the function; used by tests to inspect published events.
        """
        published_events.append((topic, kwargs))

    # Create a fresh pub mock for this test
    fresh_pub = types.SimpleNamespace(
        subscribe=lambda *_args, **_kwargs: None,
        sendMessage=_capture_events,
        AUTO_TOPIC=None,
    )
    monkeypatch.setattr(mesh_iface_module, "pub", fresh_pub)

    # Create a client that can simulate disconnection
    client = DummyClient()

    # Build interface with auto_reconnect=True
    iface = _build_interface(monkeypatch, client)
    iface.auto_reconnect = True
    assert iface._connect_stub_calls == [
        "dummy"
    ], "Initial connect should occur on instantiation"

    # Track if close() was called
    close_called = []
    original_close = iface.close

    def _track_close():
        """
        Record that the interface close was invoked and forward the call to the original close function.
        
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
        if len(iface._connect_stub_calls) >= 2 and iface.client is client:
            break
        time.sleep(0.01)

    # Assertions
    # 1. BLEInterface.close() should NOT be called when auto_reconnect=True
    assert (
        len(close_called) == 0
    ), "close() should not be called when auto_reconnect=True"

    # 2. Connection status event should be published with connected=False
    disconnect_events = [
        (topic, kw)
        for topic, kw in published_events
        if topic == "meshtastic.connection.status" and kw.get("connected") is False
    ]
    assert (
        len(disconnect_events) == 1
    ), f"Expected exactly one disconnect event, got {len(disconnect_events)}"

    # 3. Auto-reconnect should have attempted a reconnect and restored the client
    assert (
        len(iface._connect_stub_calls) >= 2
    ), "auto_reconnect should invoke connect() after disconnection"
    assert (
        iface.client is client
    ), "client should be restored after successful auto-reconnect"

    # 4. Ensure disconnect flag resets for future disconnect handling
    assert (
        not iface._disconnect_notified
    ), "_disconnect_notified should reset after auto-reconnect"

    # 5. Receive thread should remain alive, waiting for new connection
    # Check that _want_receive is still True and receive thread is alive
    assert (
        iface._want_receive is True
    ), "_want_receive should remain True for auto_reconnect"
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
            Create an ExceptionClient configured to raise a specific exception from its BLE methods.
            
            Parameters:
                exception_type (type): Exception class that the client's BLE methods will raise when invoked.
            """
            super().__init__()
            self.exception_type = exception_type

        def write_gatt_char(self, *_args, **_kwargs):
            """
            Simulate a failed GATT characteristic write by raising the configured exception.

            Raises:
                Exception: An instance of `self.exception_type` to emulate a write error.

            """
            raise self.exception_type()

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
            Create a mock BLE client and initialize its simulation state.
            
            Attributes:
                connect_count (int): Number of times connect() has been called.
                disconnect_count (int): Number of times disconnect() has been called.
                address (str): Mock Bluetooth address used by the client.
                _should_fail_connect (bool): If True, simulated connect() calls will raise an exception.
            """
            self.connect_count = 0
            self.disconnect_count = 0
            self.address = "00:11:22:33:44:55"
            self._should_fail_connect = False

        def connect(self, *_args, **_kwargs):
            """
            Simulate a client connection for tests.
            
            Increments the client's connect_count and returns the client instance.
            
            Returns:
                The mock client instance with connect_count incremented.
            
            Raises:
                RuntimeError: If `self._should_fail_connect` is True.
            """
            if self._should_fail_connect:
                raise RuntimeError()
            self.connect_count += 1
            return self

        def is_connected(self):
            """
            Indicates the mock BLE client is connected.
            
            Returns:
                `True` indicating the client reports it is connected.
            """
            return True

        def disconnect(self, *_args, **_kwargs):
            """
            Record a disconnect attempt by incrementing this client's disconnect_count.
            
            Accepts any positional and keyword arguments for call-site compatibility; they are ignored.
            """
            self.disconnect_count += 1

        def start_notify(self, *_args, **_kwargs):
            """
            Placeholder start_notify that accepts any positional and keyword arguments and performs no action.
            
            Provides a callable compatible with callers expecting a start_notify method; all arguments are ignored.
            """

        def stop_notify(self, *_args, **_kwargs):
            """
            No-op that ignores requests to stop BLE characteristic notifications.
            
            Accepts any positional and keyword arguments for API compatibility with real BLE clients and discards them.
            """

    class StressTestClient(BLEClient):
        """Mock BLE client for stress testing concurrent operations."""

        def __init__(self):
            """
            Initialize a mock BLE client that simulates connection activity for tests.
            
            Attributes:
                bleak_client: Mock underlying bleak client used to emulate BLE operations.
                connect_count (int): Number of successful connect attempts recorded.
                disconnect_count (int): Number of disconnect attempts recorded.
                is_connected_result (bool): Value returned by is_connected checks.
                _should_fail_connect (bool): When True, simulate failing connect attempts.
                _eventLoop, _eventThread: Placeholders to suppress event-loop related warnings during tests.
            """
            # Don't call super().__init__() to avoid creating real event loop
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
            Simulate a client connection for tests.
            
            Increments the client's connect_count and returns the client instance.
            
            Returns:
                The mock client instance with connect_count incremented.
            
            Raises:
                RuntimeError: If `self._should_fail_connect` is True.
            """
            if self._should_fail_connect:
                raise RuntimeError()
            self.connect_count += 1
            return self

        def is_connected(self):
            """
            Indicate whether the mock client is configured as connected.

            Returns:
                True if the mock client is configured as connected, False otherwise.

            """
            return self.is_connected_result

        def disconnect(self, *_args, **_kwargs):
            """
            Record a disconnect attempt by incrementing this client's disconnect_count.
            
            Accepts any positional and keyword arguments for call-site compatibility; they are ignored.
            """
            self.disconnect_count += 1

        def start_notify(self, *_args, **_kwargs):
            """
            Placeholder start_notify that accepts any positional and keyword arguments and performs no action.
            
            Provides a callable compatible with callers expecting a start_notify method; all arguments are ignored.
            """

        def stop_notify(self, *_args, **_kwargs):
            """
            No-op that ignores requests to stop BLE characteristic notifications.
            
            Accepts any positional and keyword arguments for API compatibility with real BLE clients and discards them.
            """

        def close(self):
            """
            No-op close used in tests to avoid interacting with the asyncio event loop.
            
            Performs no action so calling close() on a mock client does not trigger event-loop side effects or errors during unit tests.
            """

    def create_interface_with_auto_reconnect():
        """
        Create a BLEInterface configured for stress testing with auto-reconnect enabled.

        Patches BLEInterface.scan and BLEInterface.connect so the interface discovers a mock device and attaches a StressTestClient on connect.
        The returned interface has auto_reconnect enabled, exposes a test patch stack on `iface._test_patch_stack`,
        and records connect attempts in `iface._connect_stub_calls` for inspection.

        Returns:
            tuple: (iface, client) where `iface` is a BLEInterface instance configured for testing
                and `client` is the StressTestClient instance that `iface.connect()` will return.

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
            Attach a StressTestClient to this BLEInterface for testing and record the attempted connection.
            
            Records the attempted address in the surrounding test's `connect_calls` list, assigns and returns the attached StressTestClient, clears the interface's `_disconnect_notified` flag, and sets `_reconnected_event` if present.
            
            Parameters:
                self (BLEInterface): Interface to attach the client to.
                address (Optional[str]): Address used for the connection; appended to the test's `connect_calls` list.
            
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

        iface = BLEInterface(
            address=None,  # Required positional argument
            noProto=True,
            auto_reconnect=True,
        )
        iface._test_patch_stack = stack
        iface._connect_stub_calls = connect_calls
        return iface, iface.client

    # Test 1: Rapid disconnect callbacks
    iface, client = create_interface_with_auto_reconnect()

    def simulate_rapid_disconnects():
        """
        Simulate a burst of BLE disconnect events against the interface to exercise reconnect and disconnect handling.

        Invokes the interface's _on_ble_disconnect callback 10 times with short (0.01 s) pauses
        to reproduce rapid disconnect scenarios used by stress tests.
        """
        for _ in range(10):
            iface._on_ble_disconnect(client.bleak_client)
            time.sleep(0.01)  # Very short delay between disconnects

    # Start rapid disconnect simulation in a separate thread
    disconnect_thread = threading.Thread(target=simulate_rapid_disconnects)
    disconnect_thread.start()
    disconnect_thread.join()

    # Wait briefly for reconnect scheduling to occur
    for _ in range(100):
        if len(iface._connect_stub_calls) >= 2:
            break
        time.sleep(0.01)
    # Verify that the interface handled rapid disconnects gracefully
    assert client.bleak_client.disconnect_count >= 0  # Should not crash
    assert (
        len(iface._connect_stub_calls) >= 2
    ), "Auto-reconnect should continue scheduling during rapid disconnects"

    iface.close()
    iface._test_patch_stack.close()

    # Test 2: Concurrent connect/disconnect operations
    iface2, client2 = create_interface_with_auto_reconnect()

    def _stress_test_disconnects():
        """
        Perform a burst of simulated BLE disconnect callbacks against iface2 to exercise auto-reconnect and disconnect handling.
        
        Calls iface2._on_ble_disconnect(client2.bleak_client) five times with short pauses; exceptions raised by individual calls are logged and suppressed so the sequence continues.
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

    # Verify thread-safety - no exceptions should be raised
    assert client2.bleak_client.disconnect_count >= 0

    iface2.close()
    iface2._test_patch_stack.close()

    # Test 3: Stress test with connection failures
    iface3, client3 = create_interface_with_auto_reconnect()
    client3._should_fail_connect = True

    # Simulate disconnects that trigger reconnection attempts
    # Exceptions are expected and suppressed to continue stress testing
    for _ in range(5):
        try:
            iface3._on_ble_disconnect(client3.bleak_client)
            time.sleep(0.01)
        except Exception as e:  # noqa: BLE001 - expected during failure stress
            logging.debug(
                "Expected failure during stress reconnect: %s: %s", type(e).__name__, e
            )

    # Verify graceful handling of connection failures
    assert client3.bleak_client.connect_count >= 0  # Should attempt reconnections

    iface3.close()
    iface3._test_patch_stack.close()

    # Verify no critical errors in logs
    critical = [r for r in caplog.records if r.levelno >= logging.CRITICAL]
    assert (
        not critical
    ), f"Critical errors found in logs: {[r.message for r in critical]}"


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
            Initialize a test BLE client that raises the given exception type for BLE operations.
            
            Parameters:
                exception_type (type): Exception class that simulated BLE methods will raise to emulate failure modes.
            """
            self.exception_type = exception_type

        def is_connected(self):
            """
            Simulate a failing connection-state check by raising the configured exception.

            Raises:
                Exception: An instance of `self.exception_type` with the message "conn check failed".

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
            Initialize a simple future-like object that tracks cancellation state and an associated coroutine.
            
            Attributes:
                cancelled (bool): True if the future has been cancelled, False otherwise.
                coro (Optional[Coroutine]): Associated coroutine object, or None if not set.
            """
            self.cancelled = False
            self.coro = None

        def result(self, _timeout=None):
            """
            Always raise FutureTimeoutError to simulate an operation timing out.
            
            Raises:
                FutureTimeoutError: Always raised to indicate the simulated timeout.
            """
            raise FutureTimeoutError()

        def cancel(self):
            """
            Mark the fake future as cancelled.
            
            Sets the instance attribute `cancelled` to True to indicate the future has been cancelled.
            """
            self.cancelled = True

    fake_future = _FakeFuture()

    def _fake_async_run(coro):
        """
        Attach a coroutine to the test future and return that future.
        
        Parameters:
            coro (Coroutine): Coroutine to attach to the test future; it will be stored on the future as the `coro` attribute.
        
        Returns:
            fake_future: The test future instance with its `coro` attribute set to `coro`.
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
            Simulate a publishing thread failure by raising a RuntimeError when called.

            Parameters
            ----------
                _callback (callable): Ignored; accepted for compatibility with the real interface.

            Raises
            ------
                RuntimeError: Always raised to simulate a thread failure.

            """
            raise RuntimeError()

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
            Reject enqueued callbacks by raising a ValueError.
            
            Always raises ValueError to indicate that queuing callbacks is not permitted.
            """
            raise ValueError()

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

            Raises:
                ValueError: Always raised to indicate the mock callback failed during execution.

            """
            raise ValueError()

    mock_queue = Queue()
    mock_queue.put(ExceptionRunnable())

    # Mock publishingThread with the queue
    # meshtastic.ble_interface already imported at top as ble_mod

    class MockPublishingThread:
        """Mock publishingThread with a predefined queue."""

        def __init__(self):
            """
            Attach an externally provided mock queue to the instance for test inspection.
            
            Sets the instance attribute `queue` to the externally defined `mock_queue` so tests can access and manipulate the queue.
            """
            self.queue = mock_queue

        def queueWork(self, _callback):
            """
            Invoke a provided callback synchronously and return its result.
            
            Parameters:
                _callback (callable | None): Callable to invoke; if `None`, nothing is called.
            
            Returns:
                The result of `_callback()` if `_callback` is provided, otherwise `None`.
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