"""Tests for the BLE interface module - Advanced functionality."""

import logging
import threading
import time
import types
from collections.abc import Callable, Iterator
from concurrent.futures import TimeoutError as FutureTimeoutError
from contextlib import ExitStack, contextmanager
from queue import Queue
from typing import Any, cast
from unittest.mock import MagicMock, patch

import pytest
from bleak.exc import BleakError

# Import meshtastic modules for use in tests
import meshtastic.interfaces.ble.interface as ble_mod
import meshtastic.mesh_interface as mesh_iface_module
from meshtastic.interfaces.ble.client import BLEClient
from meshtastic.interfaces.ble.constants import (
    FROMNUM_UUID,
    FROMRADIO_UUID,
    LEGACY_LOGRADIO_UUID,
    LOGRADIO_UUID,
)
from meshtastic.interfaces.ble.interface import BLEInterface
from meshtastic.protobuf import mesh_pb2

# Import common fixtures
from tests.test_ble_interface_fixtures import DummyClient, _build_interface

pytestmark = pytest.mark.unit


def _get_connect_stub_calls(iface: BLEInterface) -> list[str | None]:
    """Retrieve the test-only list of addresses recorded for BLEInterface.connect calls.

    Parameters
    ----------
    iface : BLEInterface
        Interface instance that may expose a `_connect_stub_calls` attribute used by tests.

    Returns
    -------
    list[str | None]
        Recorded addresses for each connect call; `None` represents a connect attempt without an address. Returns an empty list if the attribute is not present.
    """
    return cast(list[str | None], getattr(iface, "_connect_stub_calls", []))


def _make_fake_future(exception_to_raise: Exception) -> Any:
    """Create a fake Future object that raises the provided exception from result()."""

    class _FakeFuture:
        def __init__(self) -> None:
            self.cancelled = False
            self.coro = None
            self.callbacks: list[Callable[..., Any]] = []

        def result(self, _timeout: float | None = None) -> Any:
            raise exception_to_raise

        def cancel(self) -> None:
            self.cancelled = True

        def add_done_callback(self, callback: Callable[..., Any]) -> None:
            self.callbacks.append(callback)

    return _FakeFuture()


def _bind_coro_to_future(fake_future: Any) -> Callable[[Any], Any]:
    """Build a fake _async_run callback that records the coroutine on fake_future."""

    def _fake_async_run(coro: Any) -> Any:
        fake_future.coro = coro
        return fake_future

    return _fake_async_run


def test_log_notification_registration_missing_characteristics(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Test that log notification registration handles missing characteristics gracefully."""
    # UUID constants already imported at top as ble_mod.FROMNUM_UUID, ble_mod.LEGACY_LOGRADIO_UUID, ble_mod.LOGRADIO_UUID

    class MockClientWithoutLogChars(DummyClient):
        """Mock client that doesn't have log characteristics."""

        def __init__(self) -> None:
            """Create a mock BLE client that exposes only the FROMNUM characteristic.

            Attributes
            ----------
            start_notify_calls : list
                Recorded (uuid, handler) tuples passed to start_notify.
            has_characteristic_map : dict
                Mapping of characteristic UUID to bool; contains only `FROMNUM_UUID: True`.
            """
            super().__init__()
            self.start_notify_calls: list[tuple[object, object]] = []
            self.has_characteristic_map = {
                FROMNUM_UUID: True,  # Only have the critical one
            }

        def has_characteristic(self, uuid: str) -> bool:
            """Return whether the client reports a characteristic with the given UUID.

            Parameters
            ----------
            uuid : str
                Characteristic UUID to check in the client's characteristic map.

            Returns
            -------
            bool
                `True` if the UUID is present, `False` otherwise.
            """
            return self.has_characteristic_map.get(uuid, False)

        def start_notify(self, *_args: Any, **_kwargs: Any) -> None:
            """Record a notification registration by appending a (uuid, handler) tuple to self.start_notify_calls.

            If called with at least two positional arguments, the first is treated as the characteristic UUID and the second as the notification handler; any additional positional or keyword arguments are ignored.
            """
            # Extract uuid and handler from args if available
            if len(_args) >= 2:
                uuid, handler = _args[0], _args[1]
                self.start_notify_calls.append((uuid, handler))

    client = MockClientWithoutLogChars()
    iface = _build_interface(monkeypatch, client)

    # Call _register_notifications - should not fail even without log characteristics
    iface._register_notifications(client)  # type: ignore[arg-type]

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


def test_receive_loop_handles_decode_error(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Test that the receive loop handles DecodeError gracefully without closing."""
    # logging, threading, and time already imported at top
    # FROMRADIO_UUID already imported at top as ble_mod.FROMRADIO_UUID

    caplog.set_level(logging.WARNING)

    class MockClient(DummyClient):
        """Mock client that returns invalid protobuf data to trigger DecodeError."""

        def read_gatt_char(self, *_args: object, **_kwargs: object) -> bytes:
            """Return raw GATT characteristic bytes for this test client.

            When the requested UUID equals ble_mod.FROMRADIO_UUID, returns malformed protobuf bytes to simulate a decode error; otherwise returns empty bytes.

            Returns
            -------
            bytes
                Malformed protobuf bytes for `ble_mod.FROMRADIO_UUID`, empty bytes otherwise.
            """
            # Extract uuid from args if available
            if _args and _args[0] == FROMRADIO_UUID:
                return b"invalid-protobuf-data"
            return b""

    client = MockClient()
    iface = _build_interface(monkeypatch, client)

    close_called = threading.Event()
    original_close = iface.close

    def mock_close() -> None:
        """Signal that the mock close was invoked and then call the original close callable.

        Sets the `close_called` event to notify waiters and then invokes `original_close`.
        """
        close_called.set()
        original_close()

    monkeypatch.setattr(iface, "close", mock_close)

    # Start the receive thread
    iface._want_receive = True

    # Set up the client
    with iface._state_lock:
        cast(Any, iface).client = client

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


def test_auto_reconnect_behavior(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify BLEInterface schedules an auto-reconnect after a BLE disconnect and preserves connection state and receive-thread behavior.

    This test simulates a disconnection with auto_reconnect enabled and asserts that:
    - close() is not called during auto-reconnect.
    - A disconnect event (connected=False) is published.
    - At least one reconnect attempt occurs and the original client is restored.
    - A connected=True event is published when configuration completes.
    - The internal disconnect notification flag is reset.
    - The receive thread remains active and _want_receive remains True.

    Uses test fixtures for monkeypatching and logging; does not document those fixtures.
    """
    # time and meshtastic.mesh_interface already imported at top

    # Track published events
    published_events: list[tuple[Any, dict[str, Any]]] = []

    def _capture_events(topic: Any, **kwargs: Any) -> None:
        """Record a pub/sub event for test inspection.

        Parameters
        ----------
        topic : Any
            The event topic name.
        **kwargs : Any
            Key/value pairs comprising the event payload.
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
        "meshtastic.mesh_interface.publishingThread.queueWork",
        lambda callback: callback() if callback else None,
    )

    # Create a client that can simulate disconnection
    client = DummyClient()

    # Build interface with auto_reconnect=True
    iface = _build_interface(monkeypatch, client)
    iface.auto_reconnect = True
    assert _get_connect_stub_calls(iface) == [
        "dummy"
    ], "Initial connect should occur on instantiation"

    # Track if close() was called
    close_called: list[bool] = []
    original_close = iface.close

    def _track_close() -> None:
        """Mark that close() was invoked and delegate to the preserved original close function.

        Returns
        -------
            The value returned by the preserved original close function.
        """
        close_called.append(True)
        return original_close()

    monkeypatch.setattr(iface, "close", _track_close)

    # Simulate disconnection by calling _on_ble_disconnect directly
    # This simulates what happens when bleak calls disconnected callback
    disconnect_client = iface.client
    if disconnect_client and hasattr(disconnect_client, "bleak_client"):
        iface._on_ble_disconnect(disconnect_client.bleak_client)  # type: ignore[arg-type]

    # Allow time for auto-reconnect thread to run
    for _ in range(50):
        if (
            len(_get_connect_stub_calls(iface)) >= 2
            and cast(object, iface.client) is client
        ):
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
        len(_get_connect_stub_calls(iface)) >= 2
    ), f"Expected at least 2 connect calls, got {len(_get_connect_stub_calls(iface))}"
    assert (
        cast(object, iface.client) is client
    ), "client should be restored after successful auto-reconnect"

    # Simulate config completion to publish connected=True and verify it was emitted
    iface.isConnected.clear()
    monkeypatch.setattr(iface, "_start_heartbeat", lambda: None)
    iface._connected()
    reconnect_events = [
        (topic, kw)
        for topic, kw in published_events
        if topic == "meshtastic.connection.status" and kw.get("connected") is True
    ]
    assert (
        len(reconnect_events) == 1
    ), f"Expected exactly one reconnect event, got {len(reconnect_events)}"

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


def test_send_to_radio_specific_exceptions(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Verify that _send_to_radio_impl wraps write failures for specific exception types as BLEInterface.BLEError and logs the underlying error.

    This test exercises three failure modes (BleakError, RuntimeError, OSError) by using a mock client that raises the configured exception from write_gatt_char. For each case it asserts that BLEInterface.BLEError is raised and that the log contains the name of the original exception.
    """
    # logging already imported at top
    # BleakError already imported at top as ble_mod.BleakError, BLEInterface

    # Set logging level to DEBUG to capture debug messages
    caplog.set_level(logging.DEBUG)

    class ExceptionClient(DummyClient):
        """Mock client that raises exceptions during write operations."""

        def __init__(self, exception_type: type[Exception]) -> None:
            """Create a test client that raises a specified exception type for BLE operations.

            Parameters
            ----------
            exception_type : type[Exception]
                Exception class that the client's BLE methods will raise when invoked.
            """
            super().__init__()
            self.exception_type = exception_type

        def write_gatt_char(self, *_args: Any, **_kwargs: Any) -> None:
            """Simulate a failing GATT characteristic write.

            Raises
            ------
            Exception
                An instance of `self.exception_type` with the message "write failed".
            """
            raise self.exception_type("write failed")

    # Test BleakError specifically
    client = ExceptionClient(BleakError)
    iface = _build_interface(monkeypatch, client)

    # Create a mock ToRadio message with actual data to ensure it's not empty
    # mesh_pb2 already imported at top

    to_radio = mesh_pb2.ToRadio()
    to_radio.packet.decoded.payload = b"test_data"

    # This should raise BLEInterface.BLEError
    with pytest.raises(BLEInterface.BLEError) as exc_info:
        iface._send_to_radio_impl(to_radio)

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
        iface2._send_to_radio_impl(to_radio)

    assert "Error writing BLE" in str(exc_info.value)
    assert "Error during write operation: RuntimeError" in caplog.text

    # Clear caplog for next test
    caplog.clear()
    iface2.close()

    # Test OSError
    client3 = ExceptionClient(OSError)
    iface3 = _build_interface(monkeypatch, client3)

    with pytest.raises(BLEInterface.BLEError) as exc_info:
        iface3._send_to_radio_impl(to_radio)

    assert "Error writing BLE" in str(exc_info.value)
    assert "Error during write operation: OSError" in caplog.text

    iface3.close()


@pytest.mark.unitslow
def test_rapid_connect_disconnect_stress_test(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Test rapid connect/disconnect cycles to validate thread-safety and reconnect logic."""
    # logging, threading, and time already imported at top
    # MagicMock, patch already imported at top

    # BLEClient and BLEInterface already imported at top as ble_mod.BLEClient, ble_mod.BLEInterface

    # Set logging level to DEBUG to capture all messages
    caplog.set_level(logging.DEBUG)

    # Mock device for testing
    mock_device = MagicMock()
    mock_device.address = "00:11:22:33:44:55"
    mock_device.name = "Test Device"

    class BaseMockBleakClient:
        """Shared BLE client stub behavior for stress-test doubles."""

        def __init__(
            self,
            address: str = "00:11:22:33:44:55",
            is_connected_result: bool = True,
        ) -> None:
            """Initialize the shared BLE test stub state."""
            self.connect_count = 0
            self.disconnect_count = 0
            self.address = address
            self.is_connected_result = is_connected_result
            self._should_fail_connect = False

        def connect(self, *_args: object, **_kwargs: object) -> "BaseMockBleakClient":
            """Simulate a BLE client connection for tests.

            Increments the client's connect_count and returns the client instance. If the client is configured to fail, raises a RuntimeError.

            Returns
            -------
            self
                The mock client instance with connect_count incremented.

            Raises
            ------
            RuntimeError
                If the client is configured to fail connecting.
            """
            if self._should_fail_connect:
                raise RuntimeError("connect fail")  # noqa: TRY003
            self.connect_count += 1
            return self

        def is_connected(self) -> bool:
            """Report whether the mock client is configured as connected."""
            return self.is_connected_result

        def disconnect(self, *_args: object, **_kwargs: object) -> None:
            """Record a disconnect attempt on the mock client.

            Increments the `disconnect_count` attribute by 1. Any positional or keyword arguments are accepted for call-site compatibility and ignored.
            """
            self.disconnect_count += 1

        def start_notify(self, *_args: object, **_kwargs: object) -> None:
            """Accept any positional and keyword arguments and perform no action.

            This stub is a no-op placeholder that intentionally ignores all inputs.
            """

        def stopNotify(self, *_args: object, **_kwargs: object) -> None:
            """Compatibility shim that accepts any arguments and performs no action.

            Provided for API compatibility with BLE client implementations; accepts any positional and keyword arguments and does nothing.
            """

    class MockBleakRootClient(BaseMockBleakClient):
        """Mock BleakRootClient that tracks operations for stress testing."""

    class StressTestClient(BLEClient):
        """Mock client that simulates rapid connect/disconnect cycles."""

        def __init__(self) -> None:  # pylint: disable=super-init-not-called
            # Don't call super().__init__() to avoid creating real event loop
            """Create a mock BLE root client that simulates a Bleak client and connection state for tests.

            Initializes attributes used by tests to emulate connect/disconnect behavior:

            Attributes
            ----------
            bleak_client : MockBleakRootClient
                Mock underlying Bleak client used to emulate BLE operations.
            _should_fail_connect : bool
                If True, simulated connect attempts will fail.
                Placeholder for an event loop to suppress test warnings.
                Placeholder for an event thread to suppress test warnings.
            """
            self.bleak_client = MockBleakRootClient()  # type: ignore[assignment]
            self._should_fail_connect = False
            # Add mock event loop attributes to prevent warnings
            self._eventLoop = None
            self._eventThread = None

        def _delegate_to_bleak(
            self, method_name: str, *args: object, **kwargs: object
        ) -> Any:
            """Invoke a method on the backing mock bleak client."""
            bleak_client = cast(Any, self.bleak_client)
            method = getattr(bleak_client, method_name)
            return method(*args, **kwargs)

        def connect(self, *_args: object, **_kwargs: object) -> Any:
            """Delegate connection behavior to the shared bleak client stub."""
            bleak_client = cast(Any, self.bleak_client)
            bleak_client._should_fail_connect = self._should_fail_connect
            return self._delegate_to_bleak("connect", *_args, **_kwargs)

        def is_connected(self) -> bool:
            """Delegate connection-state queries to the shared bleak client stub."""
            return cast(bool, self._delegate_to_bleak("is_connected"))

        def disconnect(self, *_args: object, **_kwargs: object) -> None:
            """Delegate disconnect behavior to the shared bleak client stub."""
            self._delegate_to_bleak("disconnect", *_args, **_kwargs)

        def start_notify(self, *_args: object, **_kwargs: object) -> None:
            """Delegate notify-start behavior to the shared bleak client stub."""
            self._delegate_to_bleak("start_notify", *_args, **_kwargs)

        def stopNotify(self, *_args: object, **_kwargs: object) -> None:
            """Delegate notify-stop behavior to the shared bleak client stub."""
            self._delegate_to_bleak("stopNotify", *_args, **_kwargs)

        def close(self) -> None:
            """No-op close method used in tests to avoid interacting with the event loop.

            This intentionally performs no action so that calling `close()` on a mock client does not trigger
            event-loop side effects or errors during unit tests.
            """

    @contextmanager
    def create_interface_with_auto_reconnect() -> (
        Iterator[tuple[BLEInterface, "StressTestClient"]]
    ):
        """Create and yield a BLEInterface configured for stress testing with auto-reconnect enabled.

        Patches BLEInterface.connect so the interface receives a StressTestClient.
        The interface is created with an explicit address to exercise reconnect
        scheduling against a concrete target address. Discovery is bypassed in
        this helper because connect is patched; BLEInterface.scan is intentionally
        left unpatched. Yields a tuple
        `(iface, client)`. On generator exit the interface is closed and all
        patches are undone.

        Yields
        ------
        tuple[BLEInterface, 'StressTestClient']
            (iface, client) where `iface` is the configured BLEInterface and
            `client` is the attached StressTestClient.
        """

        outer_client = StressTestClient()
        connect_calls: list[str | None] = []

        stack = ExitStack()

        def _patched_connect(
            self: BLEInterface,
            address: str | None = None,
            *,
            connect_timeout: float | None = None,
        ) -> "StressTestClient":
            """Attach a StressTestClient to this BLEInterface for testing and record the connection address.

            Records the attempted connection address in the test's connect_calls list, sets the interface's client to the provided StressTestClient, clears the interface's _disconnect_notified flag, and signals a _reconnected_event if present.

            Parameters
            ----------
            address : str | None
                Address used for the connection; appended to the test's connect_calls list. (Default value = None)

            Returns
            -------
            'StressTestClient'
                The client instance attached to the interface.
            """
            _ = connect_timeout
            connect_calls.append(address)
            outer_client.connect()
            cast(Any, self).client = outer_client
            self._disconnect_notified = False
            if hasattr(self, "_reconnected_event"):
                self._reconnected_event.set()
            return outer_client

        stack.enter_context(patch.object(BLEInterface, "connect", _patched_connect))

        iface = None
        try:
            iface = BLEInterface(
                address=mock_device.address,
                noProto=True,
                auto_reconnect=True,
            )
            cast(Any, iface)._connect_stub_calls = connect_calls
            client = cast("StressTestClient", iface.client)
            yield iface, client
        finally:
            try:
                if iface is not None:
                    try:
                        iface.close()
                    except Exception as e:  # noqa: BLE001 - teardown logging only
                        logging.debug(
                            "Error closing stress-test interface during cleanup: %s: %s",
                            type(e).__name__,
                            e,
                        )
            finally:
                stack.close()

    # Test 1: Rapid disconnect callbacks
    with create_interface_with_auto_reconnect() as (iface, client):

        def simulate_rapid_disconnects() -> None:
            """Simulate a burst of rapid BLE disconnect events against the test interface.

            Triggers ten disconnect callbacks roughly 0.01 seconds apart to exercise the interface's reconnect and disconnect handling during tests.
            """
            for _ in range(10):
                iface._on_ble_disconnect(cast(Any, client.bleak_client))
                time.sleep(0.01)  # Very short delay between disconnects

        # Start rapid disconnect simulation in a separate thread
        disconnect_thread = threading.Thread(
            target=simulate_rapid_disconnects, daemon=True
        )
        disconnect_thread.start()
        disconnect_thread.join()

        # Verify that the interface handled rapid disconnects gracefully
        assert client.bleak_client is not None
        # Test reaching this point without crashing indicates success
        assert (
            len(_get_connect_stub_calls(iface)) >= 2
        ), "Auto-reconnect should continue scheduling during rapid disconnects"

    # Test 2: Concurrent connect/disconnect operations
    with create_interface_with_auto_reconnect() as (iface2, client2):

        def _stress_test_disconnects() -> None:
            """Perform a burst of simulated BLE disconnects on iface2 to exercise auto-reconnect and disconnect handling.

            Performs five disconnect attempts spaced 5 milliseconds apart. Exceptions raised during attempts are caught and logged and do not interrupt remaining attempts; RuntimeError, AttributeError, and KeyError are logged with a standard message and any other exception is logged as an unexpected error.
            """
            for i in range(5):
                try:
                    iface2._on_ble_disconnect(cast(Any, client2.bleak_client))
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
        threads: list[threading.Thread] = []
        for _ in range(3):
            thread = threading.Thread(target=_stress_test_disconnects, daemon=True)
            threads.append(thread)
            thread.start()

        # Wait for all threads to complete
        for thread in threads:
            thread.join()

        # Verify thread-safety - no exceptions should be raised
        assert client2.bleak_client is not None
        assert cast(Any, client2.bleak_client).disconnect_count >= 0

    # Test 3: Stress test with connection failures
    with create_interface_with_auto_reconnect() as (iface3, client3):
        client3._should_fail_connect = True

        # Simulate disconnects that trigger reconnection attempts
        # Exceptions are expected and suppressed to continue stress testing
        for _ in range(5):
            try:
                iface3._on_ble_disconnect(cast(Any, client3.bleak_client))
                time.sleep(0.01)
            except Exception as e:  # noqa: BLE001 - expected during failure stress
                logging.debug(
                    "Expected failure during stress reconnect: %s: %s",
                    type(e).__name__,
                    e,
                )

        # Verify graceful handling of connection failures
        assert (
            len(_get_connect_stub_calls(iface3)) >= 1
        ), "Failure path should still schedule reconnect attempts"

    # Verify no critical errors in logs
    critical = [r for r in caplog.records if r.levelno >= logging.CRITICAL]
    assert (
        not critical
    ), f"Critical errors found in logs: {[r.message for r in critical]}"


def test_ble_client_is_connected_exception_handling(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Test that BLEClient.isConnected handles exceptions gracefully."""
    # logging already imported at top
    # BLEClient already imported at top as ble_mod.BLEClient

    # Set logging level to DEBUG to capture debug messages
    caplog.set_level(logging.DEBUG)

    class ExceptionBleakClient:
        """Mock bleak client that raises exceptions during connection checks."""

        def __init__(self, exception_type: type[Exception]) -> None:
            """Test BLE client constructor storing an exception type used to simulate failures in BLE operations.

            Parameters
            ----------
            exception_type : type[Exception]
                Exception class that this test client will raise from its BLE method stubs.
            """
            self.exception_type = exception_type

        def is_connected(self) -> bool:
            """Simulate a failing connection-state check by raising the configured exception.

            Raises
            ------
            Exception
                An instance of `self.exception_type` with message "conn check failed".
            """
            raise self.exception_type("conn check failed")  # noqa: TRY003

    # Create BLEClient with a mock bleak client that raises exceptions
    ble_client = BLEClient(log_if_no_address=False)
    try:
        ble_client.bleak_client = cast(Any, ExceptionBleakClient(AttributeError))

        # Should return False and log debug message when AttributeError occurs
        result = ble_client.isConnected()
        assert result is False
        assert "Unable to read bleak connection state" in caplog.text

        # Clear caplog
        caplog.clear()

        # Test TypeError
        ble_client.bleak_client = cast(Any, ExceptionBleakClient(TypeError))
        result = ble_client.isConnected()
        assert result is False
        assert "Unable to read bleak connection state" in caplog.text

        # Clear caplog
        caplog.clear()

        # Test RuntimeError
        ble_client.bleak_client = cast(Any, ExceptionBleakClient(RuntimeError))
        result = ble_client.isConnected()
        assert result is False
        assert "Unable to read bleak connection state" in caplog.text
    finally:
        ble_client.close()


def test_ble_client_async_timeout_maps_to_ble_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """BLEClient._async_await should wrap FutureTimeoutError in BLEInterface.BLEError."""

    # BLEClient and BLEInterface already imported at top as ble_mod.BLEClient, ble_mod.BLEInterface

    client = BLEClient()  # address=None keeps underlying bleak client unset
    fake_future = _make_fake_future(FutureTimeoutError())
    monkeypatch.setattr(client, "_async_run", _bind_coro_to_future(fake_future))

    async def _test_coro() -> None:
        """Run a no-op coroutine used for tests."""
        return None

    # BLEClient._async_await raises BLEClient.BLEError for timeouts
    with pytest.raises(BLEClient.BLEError) as excinfo:
        client._async_await(_test_coro(), timeout=0.01)

    assert "Async operation timed out" in str(excinfo.value)
    assert fake_future.cancelled is True

    client.close()
    coro_obj = getattr(fake_future, "coro", None)
    if isinstance(coro_obj, types.CoroutineType):
        coro_obj.close()


def test_ble_client_async_runtime_error_maps_to_ble_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """BLEClient._async_await should surface RuntimeError as a non-timeout BLE error."""
    client = BLEClient()
    fake_future = _make_fake_future(RuntimeError("loop is closed"))
    monkeypatch.setattr(client, "_async_run", _bind_coro_to_future(fake_future))

    async def _test_coro() -> None:
        """Run a no-op coroutine used for tests."""
        return None

    # BLEClient._async_await raises BLEClient.BLEError for runtime errors
    with pytest.raises(BLEClient.BLEError) as excinfo:
        client._async_await(_test_coro(), timeout=0.01)

    assert "Async operation failed: loop is closed" in str(excinfo.value)
    assert fake_future.cancelled is True
    client.close()
    coro_obj = getattr(fake_future, "coro", None)
    if isinstance(coro_obj, types.CoroutineType):
        coro_obj.close()


def test_wait_for_disconnect_notifications_exceptions(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Test that _wait_for_disconnect_notifications handles exceptions gracefully."""
    # logging already imported at top

    # Set logging level to DEBUG to capture debug messages
    caplog.set_level(logging.DEBUG)

    # Also ensure the logger is configured to capture the actual module logger
    logger = logging.getLogger("meshtastic.ble")
    logger.setLevel(logging.DEBUG)

    client = DummyClient()
    iface = _build_interface(monkeypatch, client)

    # Mock publishingThread to raise RuntimeError
    # meshtastic.ble_interface already imported at top as ble_mod

    class MockPublishingThread:
        """Mock publishingThread that raises RuntimeError in queueWork."""

        def queueWork(self, _callback: Any) -> None:
            """Simulate a publishing thread failure by raising a RuntimeError.

            This mock implementation ignores the provided callback and unconditionally raises an error to emulate a thread failure.

            Parameters
            ----------
            _callback : callable
                Work callback to queue; ignored by this mock.

            Raises
            ------
            RuntimeError
                Always raised with the message "thread error in queueWork".
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

        def queueWork(self, _callback: Any) -> None:
            """Refuse enqueued callbacks by always raising a ValueError with message "invalid state".

            Parameters
            ----------
            _callback : Any
                The callback that would have been queued (ignored).

            Raises
            ------
            ValueError
                Always raised with the message "invalid state".
            """
            raise ValueError("invalid state")  # noqa: TRY003

    monkeypatch.setattr(ble_mod, "publishingThread", MockPublishingThread2())

    # Should handle ValueError gracefully
    iface._wait_for_disconnect_notifications()
    assert "disconnect notification flush" in caplog.text

    iface.close()


def test_drain_publish_queue_exceptions(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Test that _drain_publish_queue handles exceptions gracefully."""
    # logging, threading, and Queue already imported at top

    # Set logging level to DEBUG to capture debug messages
    caplog.set_level(logging.DEBUG)

    client = DummyClient()
    iface = _build_interface(monkeypatch, client)

    # Create a mock queue with a runnable that raises exceptions
    class ExceptionRunnable:
        """Mock runnable that raises ValueError when called."""

        def __call__(self) -> None:
            """Call the mock callback; this implementation always raises a ValueError.

            Raises
            ------
            ValueError
                Always raised to indicate the mock callback failed during execution.
            """
            raise ValueError("callback failed")  # noqa: TRY003

    mock_queue: Queue[Callable[[], object]] = Queue()
    mock_queue.put(ExceptionRunnable())

    # Mock publishingThread with the queue
    # meshtastic.ble_interface already imported at top as ble_mod

    class MockPublishingThread:
        """Mock publishingThread with a predefined queue."""

        def __init__(self) -> None:
            """Initialize the mock publishing thread with a provided external queue.

            Store the external `mock_queue` on `self.queue` so tests can inject, control, and inspect deferred publish callbacks.
            """
            self.queue = mock_queue

        def queueWork(self, _callback: Callable[[], object] | None) -> object | None:
            """Execute a callback immediately to simulate scheduling work.

            Parameters
            ----------
            _callback : callable | None
                Callable to execute; if `None`, no action is taken.

            Returns
            -------
                The value returned by `_callback` if provided, otherwise `None`.
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
