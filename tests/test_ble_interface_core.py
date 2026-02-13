"""Tests for the BLE interface module - Core functionality."""

import asyncio
import inspect
import logging
import threading
import time
from types import SimpleNamespace
from typing import (
    TYPE_CHECKING,
    Any,
    Awaitable,
    Callable,
    Dict,
    List,
    Optional,
    Protocol,
    cast,
)

import pytest
from bleak.exc import BleakError
from bleak.backends.device import BLEDevice

# Import common fixtures
from test_ble_interface_fixtures import DummyClient, _build_interface

# Import meshtastic modules for use in tests
import meshtastic.interfaces.ble as ble_mod
from meshtastic.interfaces.ble import (
    FROMNUM_UUID,
    LEGACY_LOGRADIO_UUID,
    LOGRADIO_UUID,
    SERVICE_UUID,
    BLEClient,
    BLEInterface,
)
from meshtastic.interfaces.ble.connection import ConnectionValidator
from meshtastic.interfaces.ble.discovery import ConnectedStrategy, DiscoveryManager
from meshtastic.interfaces.ble.reconnection import ReconnectScheduler, ReconnectWorker
from meshtastic.interfaces.ble.state import BLEStateManager, ConnectionState

if TYPE_CHECKING:

    class _PubProtocol(Protocol):
        def sendMessage(self, topic: str, **kwargs: Any) -> None:
            """
            Publish a message to a pubsub topic.

            Parameters:
                topic (str): Topic name under which the message will be published.
                **kwargs: Key/value pairs that will be included as the message payload.
            """
            ...

    pub: _PubProtocol
else:  # pragma: no cover - import only at runtime
    from pubsub import pub


def _create_ble_device(address: str, name: str) -> BLEDevice:
    """
    Create a BLEDevice compatible with the installed bleak constructor.

    When the BLEDevice constructor accepts `details` and/or `rssi`, those arguments
    are provided (empty dict and 0 respectively) so the returned instance works
    across bleak versions.

    Returns:
        BLEDevice: A BLEDevice instance constructed with the arguments supported by the installed bleak version.
    """
    params: Dict[str, Any] = {"address": address, "name": name}
    signature = inspect.signature(BLEDevice.__init__)
    if "details" in signature.parameters:
        params["details"] = {}
    if "rssi" in signature.parameters:
        params["rssi"] = 0
    return BLEDevice(**params)


class _StrategyOverride(ConnectedStrategy):
    """
    Adapt an async callable into a ConnectedStrategy-compatible object for testing.
    """

    def __init__(
        self,
        delegate: Callable[[Optional[str], float], Awaitable[List[BLEDevice]]],
    ) -> None:
        """
        Create a ConnectedStrategy wrapper using the provided discovery coroutine.

        Parameters:
            delegate (Callable[[Optional[str], float], Awaitable[List[BLEDevice]]]):
                Async callable invoked as delegate(address, timeout) that returns a list of
                discovered BLEDevice objects for the optional address and timeout in seconds.
        """
        self._delegate = delegate

    async def discover(self, address: Optional[str], timeout: float) -> List[BLEDevice]:
        """
        Call the wrapped discovery delegate to find BLE devices for the given address and timeout.

        Parameters:
            address (Optional[str]): Bluetooth address to filter results, or `None` to allow any device.
            timeout (float): Maximum time in seconds to wait for discovery.

        Returns:
            List[BLEDevice]: Discovered BLEDevice instances matching the request.
        """
        return await self._delegate(address, timeout)


def test_find_device_returns_single_scan_result():
    """find_device should return the lone scanned device."""
    # BLEDevice and BLEInterface already imported at top as ble_mod.BLEDevice, ble_mod.BLEInterface

    iface = object.__new__(ble_mod.BLEInterface)
    scanned_device = _create_ble_device(address="11:22:33:44:55:66", name="Test Device")
    iface._discovery_manager = SimpleNamespace(
        discover_devices=lambda _address: [scanned_device]
    )

    result = ble_mod.BLEInterface.find_device(iface, None)

    assert result is scanned_device


def test_ble_package_all_uses_stable_surface():
    """`meshtastic.interfaces.ble.__all__` should expose the stable facade only."""
    assert "BLEInterface" in ble_mod.__all__
    assert "BLEClient" in ble_mod.__all__
    assert "ConnectionValidator" not in ble_mod.__all__
    assert "ThreadCoordinator" not in ble_mod.__all__


def test_state_manager_closing_only_for_disconnect():
    """is_closing should be true only while disconnecting."""
    state_manager = BLEStateManager()
    assert state_manager.is_closing is False
    # Allow transition to DISCONNECTING from DISCONNECTED (shutdown path)
    assert state_manager.transition_to(ConnectionState.DISCONNECTING) is True
    assert state_manager.is_closing is True
    assert state_manager.transition_to(ConnectionState.DISCONNECTED) is True
    assert state_manager.is_closing is False
    assert state_manager.transition_to(ConnectionState.ERROR) is True
    assert state_manager.is_closing is False


def test_handle_disconnect_ignores_stale_callbacks(monkeypatch):
    """Stale disconnect callbacks must not clear the current active client."""
    stale_client = DummyClient()
    iface = _build_interface(monkeypatch, stale_client)

    active_client = DummyClient()
    active_client.address = "active"
    active_client.bleak_client = SimpleNamespace(address="active")
    reconnect_calls: List[bool] = []
    disconnected_calls: List[bool] = []

    monkeypatch.setattr(
        iface,
        "_schedule_auto_reconnect",
        lambda: reconnect_calls.append(True),
        raising=True,
    )
    monkeypatch.setattr(
        iface, "_disconnected", lambda: disconnected_calls.append(True), raising=True
    )

    with iface._state_lock:
        iface.client = active_client
        iface._disconnect_notified = False
        iface._state_manager.reset_to_disconnected()
        assert iface._state_manager.transition_to(ConnectionState.CONNECTING) is True
        assert iface._state_manager.transition_to(ConnectionState.CONNECTED) is True

    # Stale callback by BLEClient instance should be ignored.
    assert iface._handle_disconnect("stale-client", client=stale_client) is True
    # Stale callback by bleak client identity should also be ignored.
    assert (
        iface._handle_disconnect("stale-bleak", bleak_client=stale_client.bleak_client)
        is True
    )

    assert iface.client is active_client
    assert iface._disconnect_notified is False
    assert reconnect_calls == []
    assert disconnected_calls == []

    iface.close()


def test_transient_read_retry_uses_zero_based_delay(monkeypatch):
    """Transient read retries should pass a zero-based attempt index to policy delay."""
    iface = _build_interface(monkeypatch, DummyClient())
    delay_attempts: List[int] = []

    class StubTransientPolicy:
        def should_retry(self, attempt: int) -> bool:
            """
            Return True only for the first retry attempt.
            """
            return attempt < 1

        def get_delay(self, attempt: int) -> float:
            """
            Record the delay attempt index and return a no-op delay.
            """
            delay_attempts.append(attempt)
            return 0.0

    monkeypatch.setattr(
        "meshtastic.interfaces.ble.interface.RetryPolicy.transient_error",
        lambda: StubTransientPolicy(),
    )
    monkeypatch.setattr(
        "meshtastic.interfaces.ble.interface._sleep", lambda _delay: None
    )

    iface._read_retry_count = 0
    iface._handle_transient_read_error(BleakError("transient"))

    assert iface._read_retry_count == 1
    assert delay_attempts == [0]

    iface.close()


def test_receive_loop_outer_catch_routes_to_disconnect_handler(monkeypatch):
    """Outer receive-loop exceptions should use normal disconnect handling."""
    client = DummyClient()
    iface = _build_interface(monkeypatch, client)
    disconnect_calls: List[tuple] = []

    def raising_wait_for_event(_name: str, timeout: Optional[float] = None) -> bool:
        """
        Simulate an unexpected fatal receive-loop failure.
        """
        _ = timeout
        raise RuntimeError("fatal receive loop failure")

    def fake_handle_disconnect(
        source: str,
        client: Optional[Any] = None,
        bleak_client: Optional[Any] = None,
    ) -> bool:
        """
        Record disconnect handler invocation and stop receive loop progression.
        """
        disconnect_calls.append((source, client, bleak_client))
        iface._want_receive = False
        return False

    monkeypatch.setattr(
        iface.thread_coordinator,
        "wait_for_event",
        raising_wait_for_event,
        raising=True,
    )
    monkeypatch.setattr(
        iface, "_handle_disconnect", fake_handle_disconnect, raising=True
    )

    iface._want_receive = True
    iface._receiveFromRadioImpl()

    assert disconnect_calls
    source, disconnected_client, disconnected_bleak = disconnect_calls[0]
    assert source == "receive_thread_fatal"
    assert disconnected_client is client
    assert disconnected_bleak is None

    iface.close()


def test_start_receive_thread_skips_when_interface_closed(monkeypatch):
    """Receive thread start helper should no-op once the interface is closed."""
    client = DummyClient()
    iface = _build_interface(monkeypatch, client)
    iface.close()

    def should_not_create_thread(*_args, **_kwargs):
        raise AssertionError("create_thread should not be called after close()")

    monkeypatch.setattr(
        iface.thread_coordinator,
        "create_thread",
        should_not_create_thread,
        raising=True,
    )

    iface._start_receive_thread(name="BLEReceiveAfterClose")


def test_find_device_uses_connected_fallback_when_scan_empty():
    """find_device should fall back to connected-device lookup when scan is empty."""
    # BLEDevice and BLEInterface already imported at top as ble_mod.BLEDevice, ble_mod.BLEInterface

    iface = object.__new__(ble_mod.BLEInterface)
    fallback_device = _create_ble_device(address="AA:BB:CC:DD:EE:FF", name="Fallback")
    iface._discovery_manager = SimpleNamespace(
        discover_devices=lambda addr: [fallback_device] if addr else []
    )

    result = BLEInterface.find_device(iface, "aa-bb-cc-dd-ee-ff")

    assert result is fallback_device


def test_find_device_multiple_matches_raises():
    """Providing an address that matches multiple devices should raise BLEError."""
    # BLEDevice and BLEInterface already imported at top as ble_mod.BLEDevice, ble_mod.BLEInterface

    iface = object.__new__(ble_mod.BLEInterface)
    devices = [
        _create_ble_device(address="AA:BB:CC:DD:EE:FF", name="Meshtastic-1"),
        _create_ble_device(address="AA-BB-CC-DD-EE-FF", name="Meshtastic-2"),
    ]
    iface._discovery_manager = SimpleNamespace(discover_devices=lambda _addr: devices)

    with pytest.raises(BLEInterface.BLEError) as excinfo:
        BLEInterface.find_device(iface, "aa bb cc dd ee ff")

    assert "Multiple Meshtastic BLE peripherals found matching" in str(excinfo.value)


def test_connected_strategy_skips_private_backend_when_guard_fails(monkeypatch):
    """ConnectedStrategy should not touch private backend when guard disallows it."""

    monkeypatch.setattr(
        "meshtastic.interfaces.ble.discovery._bleak_supports_connected_fallback",
        lambda: False,
    )

    class BoomScanner:
        """Mock scanner that raises an exception when instantiated."""

        def __init__(self):
            """
            Prevent instantiation of the scanner when a guard condition fails.

            Always raises an AssertionError with the message "BleakScanner should not be instantiated when guard fails".

            Raises:
                AssertionError: Indicates the scanner must not be created when the guard is false.
            """
            raise AssertionError(
                "BleakScanner should not be instantiated when guard fails"
            )

    monkeypatch.setattr("meshtastic.interfaces.ble.discovery.BleakScanner", BoomScanner)

    strategy = ConnectedStrategy()
    result = asyncio.run(strategy.discover(address="AA:BB", timeout=1.0))
    assert result == []


def test_discovery_manager_filters_meshtastic_devices(monkeypatch):
    """DiscoveryManager should return only devices advertising the Meshtastic service UUID."""

    filtered_device = _create_ble_device("AA:BB:CC:DD:EE:FF", "Filtered")
    other_device = _create_ble_device("11:22:33:44:55:66", "Other")

    class FakeClient:
        def __enter__(self):
            """
            Return the context manager instance for use with the with statement.

            Returns:
                self: The context manager instance.
            """
            return self

        def __exit__(self, exc_type, exc, tb):
            """
            Indicate that the context manager does not suppress exceptions raised inside the with-block.

            Parameters:
                exc_type (Any): The exception class raised in the with-block, or None if no exception occurred.
                exc (Any): The exception instance raised in the with-block, or None.
                tb (Any): The traceback object for the exception, or None.

            Returns:
                bool: `False` to propagate any exception raised in the with-block.
            """
            return False

        def discover(self, **_kwargs):
            """
            Provide a test discovery mapping of labeled (device, advertisement) pairs.

            Returns:
                dict: Mapping with keys "filtered" and "other". "filtered" maps to a (device, advertisement) pair whose `advertisement.service_uuids` includes `SERVICE_UUID`. "other" maps to a (device, advertisement) pair whose `advertisement.service_uuids` does not include `SERVICE_UUID`.
            """
            return {
                "filtered": (
                    filtered_device,
                    SimpleNamespace(service_uuids=[SERVICE_UUID]),
                ),
                "other": (
                    other_device,
                    SimpleNamespace(service_uuids=["some-other-service"]),
                ),
            }

        def async_await(self, coro, timeout=None):
            """
            Fail the test if a connected-device fallback coroutine is awaited.

            This test stub is used to ensure the connected-device fallback path is not invoked; calling this function always raises an AssertionError.

            Parameters:
                coro: The coroutine that would have been awaited for a connected-device fallback.
                timeout: Unused timeout parameter included for API compatibility.

            Raises:
                AssertionError: Always raised with the message "Fallback should not be attempted when scan succeeds".
            """
            raise AssertionError("Fallback should not be attempted when scan succeeds")

    monkeypatch.setattr(ble_mod, "BLEClient", lambda **_kwargs: FakeClient())

    manager = DiscoveryManager()

    devices = manager.discover_devices(address=None)

    assert len(devices) == 1
    assert devices[0].address == filtered_device.address


def test_discovery_manager_uses_connected_strategy_when_scan_empty(monkeypatch):
    """When no devices are discovered via BLE scan, DiscoveryManager should fall back to connected strategy."""

    fallback_device = _create_ble_device("AA:BB", "Fallback")

    class FakeClient:
        def __enter__(self):
            """
            Return the context manager instance for use with the with statement.

            Returns:
                self: The context manager instance.
            """
            return self

        def __exit__(self, exc_type, exc, tb):
            """
            Indicate that the context manager does not suppress exceptions raised inside the with-block.

            Parameters:
                exc_type (Any): The exception class raised in the with-block, or None if no exception occurred.
                exc (Any): The exception instance raised in the with-block, or None.
                tb (Any): The traceback object for the exception, or None.

            Returns:
                bool: `False` to propagate any exception raised in the with-block.
            """
            return False

        def discover(self, **_kwargs):
            """
            Create an empty mapping for discovered BLE devices.

            Returns:
                dict: Mapping of discovered device addresses to metadata (always empty).
            """
            return {}

        @staticmethod
        def async_await(coro, timeout=None):
            """
            Execute an awaitable to completion and return its result.

            Parameters:
                coro (Awaitable): The coroutine or awaitable to execute.
                timeout (float | None): Optional timeout value; ignored by this implementation.

            Returns:
                The value produced by the awaitable.
            """
            return asyncio.run(coro)

    monkeypatch.setattr(ble_mod, "BLEClient", lambda **_kwargs: FakeClient())

    manager = DiscoveryManager()

    async def fake_connected(address: Optional[str], timeout: float) -> List[BLEDevice]:
        """
        Return the predefined fallback device when invoked with the expected address and timeout.

        Parameters:
            address: Expected device address; must equal "AA:BB".
            timeout: Expected scan timeout; must equal ble_mod.BLEConfig.BLE_SCAN_TIMEOUT.

        Returns:
            A list containing the single `fallback_device`.
        """
        assert address == "AA:BB"
        assert timeout == ble_mod.BLEConfig.BLE_SCAN_TIMEOUT
        return [fallback_device]

    manager.connected_strategy = _StrategyOverride(fake_connected)

    devices = manager.discover_devices(address="AA:BB")

    assert devices == [fallback_device]


def test_discovery_manager_skips_fallback_without_address(monkeypatch):
    """Connected-device fallback should not run when no address filter is provided."""

    class FakeClient:
        def __enter__(self):
            """
            Return the context manager instance for use with the with statement.

            Returns:
                self: The context manager instance.
            """
            return self

        def __exit__(self, exc_type, exc, tb):
            """
            Indicate that the context manager does not suppress exceptions raised inside the with-block.

            Parameters:
                exc_type (Any): The exception class raised in the with-block, or None if no exception occurred.
                exc (Any): The exception instance raised in the with-block, or None.
                tb (Any): The traceback object for the exception, or None.

            Returns:
                bool: `False` to propagate any exception raised in the with-block.
            """
            return False

        def discover(self, **_kwargs):
            """
            Create an empty mapping for discovered BLE devices.

            Returns:
                dict: Mapping of discovered device addresses to metadata (always empty).
            """
            return {}

        @staticmethod
        def async_await(
            coro, timeout=None
        ):  # pragma: no cover - fallback should not be hit
            """
            Execute a coroutine to completion and return its result.

            Parameters:
                coro: The coroutine to execute.
                timeout: Ignored in this fallback implementation.

            Returns:
                The value produced by the coroutine.
            """
            return asyncio.run(coro)

    monkeypatch.setattr(ble_mod, "BLEClient", lambda **_kwargs: FakeClient())

    manager = DiscoveryManager()

    fallback_called = False

    async def fake_connected(
        address: Optional[str], timeout: float
    ) -> List[BLEDevice]:  # pragma: no cover - should not run
        """
        Mark the connected-fallback as invoked for tests and return no devices.

        This test helper sets the enclosing `fallback_called` flag to True to indicate the fallback was exercised.

        Parameters:
            address (Optional[str]): Address passed to the fallback; recorded for test context but not used.
            timeout (float): Timeout value passed to the fallback; accepted but ignored.

        Returns:
            list: An empty list indicating no connected devices were found.
        """
        nonlocal fallback_called
        fallback_called = True
        return []

    manager.connected_strategy = _StrategyOverride(fake_connected)

    assert manager.discover_devices(address=None) == []
    assert fallback_called is False


def test_connection_validator_enforces_state():
    """ConnectionValidator should block connections when interface is closing or already connecting."""

    state_manager = BLEStateManager()
    validator = ConnectionValidator(
        state_manager, state_manager.lock, BLEInterface.BLEError
    )

    validator.validate_connection_request()

    assert state_manager.transition_to(ConnectionState.CONNECTING) is True
    assert state_manager.transition_to(ConnectionState.CONNECTED) is True
    assert state_manager.transition_to(ConnectionState.DISCONNECTING) is True
    with pytest.raises(BLEInterface.BLEError) as excinfo:
        validator.validate_connection_request()
    assert "closing" in str(excinfo.value)

    assert state_manager.transition_to(ConnectionState.DISCONNECTED) is True
    assert state_manager.transition_to(ConnectionState.CONNECTING) is True
    with pytest.raises(BLEInterface.BLEError) as excinfo:
        validator.validate_connection_request()
    assert "connection in progress" in str(excinfo.value)


def test_connection_validator_existing_client_checks():
    """check_existing_client should allow reuse only when the requested identifier matches."""

    state_manager = BLEStateManager()
    validator = ConnectionValidator(
        state_manager, state_manager.lock, BLEInterface.BLEError
    )
    client = DummyClient()
    client.is_connected = lambda: True

    ble_like = cast(BLEClient, client)
    assert validator.check_existing_client(ble_like, None, None, None) is True
    assert validator.check_existing_client(ble_like, "dummy", "dummy", "dummy") is True
    assert (
        validator.check_existing_client(client, "something-else", None, None) is False
    )


def test_close_idempotent(monkeypatch):
    """Test that close() is idempotent and only calls disconnect once."""
    client = DummyClient()
    iface = _build_interface(monkeypatch, client)

    iface.close()
    iface.close()
    iface.close()  # Call multiple times to ensure idempotency

    assert client.disconnect_calls == 1
    assert client.close_calls == 1


@pytest.mark.parametrize("exc_cls", [BleakError, RuntimeError, OSError])
def test_close_handles_errors(monkeypatch, exc_cls):
    """Test that close() handles various exception types gracefully."""
    # pub already imported at top as mesh_iface_module.pub

    calls = []

    def _capture(topic, **kwargs):
        """
        Record a published message by appending a (topic, kwargs) tuple to the module-level `calls` list.

        Parameters:
            topic: The pubsub topic identifier.
            **kwargs: Additional message fields to capture.
        """
        calls.append((topic, kwargs))

    monkeypatch.setattr(pub, "sendMessage", _capture)

    client = DummyClient(disconnect_exception=exc_cls("boom"))
    iface = _build_interface(monkeypatch, client)

    iface.close()

    assert client.disconnect_calls == 1
    assert client.close_calls == 1
    assert (
        sum(
            1
            for t, kw in calls
            if t == "meshtastic.connection.status" and kw.get("connected") is False
        )
        == 1
    )

    client = DummyClient(disconnect_exception=OSError("Permission denied"))
    iface = _build_interface(monkeypatch, client)

    iface.close()

    assert client.disconnect_calls == 1
    assert client.close_calls == 1


def test_close_skips_disconnect_when_interpreter_finalizing(monkeypatch):
    """close() should avoid scheduling disconnect coroutines during finalization."""
    client = DummyClient()
    iface = _build_interface(monkeypatch, client)

    monkeypatch.setattr(
        "meshtastic.interfaces.ble.connection.sys.is_finalizing",
        lambda: True,
    )

    iface.close()

    assert client.disconnect_calls == 0
    assert client.close_calls == 1


def test_close_clears_ble_threads(monkeypatch):
    """Closing the interface should leave no BLE* threads running."""
    # threading already imported at top

    client = DummyClient()
    iface = _build_interface(monkeypatch, client)

    iface.close()

    # Poll for thread cleanup with a reasonable timeout
    max_wait_time = 1.0  # Maximum time to wait for thread cleanup
    poll_interval = 0.05  # Time between checks
    deadline = time.monotonic() + max_wait_time
    lingering = []  # Initialize to ensure it's defined outside the loop

    while time.monotonic() < deadline:
        # Check for specific BLE interface threads that should be cleaned up
        # Exclude singleton threads that persist across interface instances
        lingering = [
            thread.name
            for thread in threading.enumerate()
            if thread.name.startswith("BLE")
            and thread.name not in ("BLEClient", "BLECoroutineRunner")
        ]

        if not lingering:
            break  # No lingering threads found

        time.sleep(poll_interval)

    assert not lingering, (
        f"Found lingering BLE threads after {max_wait_time}s: {lingering}"
    )


def test_receive_thread_specific_exceptions(monkeypatch, caplog):
    """
    Verify that the BLE receive thread treats specific exceptions as fatal: it logs a fatal error message and invokes the interface's close().

    The test iterates over RuntimeError and OSError by injecting a client whose read_gatt_char raises the exception,
    triggers the receive loop, and asserts that the fatal log entry is present and that close() was called.
    """
    # logging and threading already imported at top

    # Set logging level to DEBUG to capture debug messages
    caplog.set_level(logging.DEBUG)

    # The exceptions that should be caught and handled as immediately fatal
    # Note: BleakError now goes through transient retry logic first, tested separately
    handled_exceptions = [
        RuntimeError,
        OSError,
    ]

    class ExceptionClient(DummyClient):
        """Mock client that raises specific exceptions for testing."""

        def __init__(self, exception_type):
            """
            Create a test BLE client that raises the specified exception type from methods that simulate faults.

            Parameters:
                exception_type (type or Exception): Exception class or instance to be raised by the client's faulting methods.
            """
            super().__init__()
            self.exception_type = exception_type

        def read_gatt_char(self, *_args, **_kwargs):
            """
            Raise the client's configured exception to simulate a failing GATT characteristic read.

            Raises:
                Exception: An instance of `self.exception_type` constructed with the message "test".
            """
            raise self.exception_type("test")

    for exc_type in handled_exceptions:
        # Clear caplog for each test
        caplog.clear()

        # Create a mock client that raises the specific exception
        client = ExceptionClient(exc_type)
        iface = _build_interface(monkeypatch, client)

        # Mock the close method to track if it's called
        original_close = iface.close
        close_called = threading.Event()

        def mock_close(original_close=original_close, close_called=close_called):
            """
            Signal that close was invoked and delegate to the original close callable.

            Parameters:
                original_close (callable): The original close function to invoke.
                close_called (threading.Event): Event to set to indicate close was called.

            Returns:
                Any: The value returned by `original_close()`.
            """
            close_called.set()
            return original_close()

        monkeypatch.setattr(iface, "close", mock_close)

        # Start the receive thread
        iface._want_receive = True

        # Phase 3: Use unified state lock instead of _client_lock
        with iface._state_lock:
            iface.client = client

        # Trigger the receive loop
        iface._read_trigger.set()

        # Wait for the exception to be handled and close to be called
        # Use a reasonable timeout to avoid hanging the test
        close_called.wait(timeout=5.0)

        # Check that appropriate logging occurred
        assert "Fatal error in BLE receive thread" in caplog.text
        assert close_called.is_set(), (
            f"Expected close() to be called for {exc_type.__name__}"
        )

        # Clean up
        iface._want_receive = False
        caplog.clear()
        try:
            iface.close()
        except Exception as exc:  # noqa: BLE001 - cleanup best-effort in tests
            # Log for visibility; still allow test to proceed with cleanup.
            logging.warning("Cleanup error in iface.close(): %r", exc)


def test_bleak_error_transient_retry_logic(monkeypatch, caplog):
    """
    Verify that BleakError in the receive thread goes through transient retry logic.

    The interface should retry on transient BleakError before giving up and closing.
    """
    caplog.set_level(logging.DEBUG)

    class BleakErrorClient(DummyClient):
        """Mock client that raises BleakError for testing retry logic."""

        def __init__(self):
            """
            Initialize the instance and set the read operation counter.

            Initializes the object via the superclass constructor and sets the `read_count`
            attribute to 0 to track the number of read operations performed.
            """
            super().__init__()
            self.read_count = 0

        def read_gatt_char(self, *_args, **_kwargs):
            """
            Simulate a GATT characteristic read that always fails with a BleakError.

            Increments the instance's read counter and then raises BleakError("transient error").

            Raises:
                BleakError: always raised with the message "transient error".
            """
            self.read_count += 1
            raise BleakError("transient error")

    client = BleakErrorClient()
    iface = _build_interface(monkeypatch, client)

    # Mock the close method to track if it's called
    original_close = iface.close
    close_called = threading.Event()

    def mock_close(original_close=original_close, close_called=close_called):
        """
        Set the provided `close_called` event and invoke the original close callable.

        This test helper marks that a close operation was requested by calling `close_called.set()` and then calls and returns the result of `original_close()` so the original close behavior still executes.

        Returns:
            The value returned by `original_close()`.
        """
        close_called.set()
        return original_close()

    monkeypatch.setattr(iface, "close", mock_close)

    # Start the receive thread
    iface._want_receive = True

    with iface._state_lock:
        iface.client = client

    # Trigger the receive loop
    iface._read_trigger.set()

    # Wait for the retry logic to exhaust and close to be called
    close_called.wait(timeout=5.0)

    # Verify retry attempts were logged
    assert "Transient BLE read error, retrying" in caplog.text
    assert "Fatal BLE read error after retries" in caplog.text

    # Verify close was called
    assert close_called.is_set()

    # Clean up
    iface._want_receive = False
    try:
        iface.close()
    except Exception as exc:  # noqa: BLE001 - cleanup best-effort in tests
        logging.warning("Cleanup error in iface.close(): %r", exc)


def test_log_notification_registration(monkeypatch):
    """Test that log notifications are properly registered for both legacy and current log UUIDs."""
    # UUID constants already imported at top as ble_mod.FROMNUM_UUID, ble_mod.LEGACY_LOGRADIO_UUID, ble_mod.LOGRADIO_UUID

    class MockClientWithLogChars(DummyClient):
        """Mock client that has log characteristics."""

        def __init__(self):
            """
            Initialize the mock client and configure notification tracking and reported characteristics.

            Attributes
            ----------
                start_notify_calls (list): Records arguments passed to each start_notify invocation.
                has_characteristic_map (dict): Maps characteristic UUIDs to booleans indicating presence;
                    initially sets LEGACY_LOGRADIO_UUID, LOGRADIO_UUID, and FROMNUM_UUID to True.
            """
            super().__init__()
            self.start_notify_calls = []
            self.has_characteristic_map = {
                LEGACY_LOGRADIO_UUID: True,
                LOGRADIO_UUID: True,
                FROMNUM_UUID: True,
            }

        def has_characteristic(self, uuid):
            """
            Return whether the client's characteristic map contains the given characteristic UUID.

            Parameters:
                uuid: Characteristic UUID to check (can be a uuid.UUID or comparable key used in the map).

            Returns:
                True if the UUID is present, False otherwise.
            """
            return self.has_characteristic_map.get(uuid, False)

        def start_notify(self, *_args, **_kwargs):
            """
            Record a notification registration by saving the characteristic UUID and its handler.

            If called with at least two positional arguments, appends a (uuid, handler) tuple to self.start_notify_calls.
            Additional positional arguments and any keyword arguments are accepted and ignored.

            Parameters:
                _args: Positional arguments where the first is the characteristic UUID and the second is the notification handler.
                _kwargs: Accepted and ignored.
            """
            # Extract uuid and handler from args if available
            if len(_args) >= 2:
                uuid, handler = _args[0], _args[1]
                self.start_notify_calls.append((uuid, handler))

    client = MockClientWithLogChars()
    iface = _build_interface(monkeypatch, client)

    # Call _register_notifications to test log notification setup
    iface._register_notifications(cast(BLEClient, client))

    # Verify that all three notifications were registered
    registered_uuids = [call[0] for call in client.start_notify_calls]

    # Should have registered both log notifications and the critical FROMNUM notification
    assert LEGACY_LOGRADIO_UUID in registered_uuids, (
        "Legacy log notification should be registered"
    )
    assert LOGRADIO_UUID in registered_uuids, (
        "Current log notification should be registered"
    )
    assert FROMNUM_UUID in registered_uuids, "FROMNUM notification should be registered"

    # Verify handlers are correctly associated
    legacy_call = next(
        call for call in client.start_notify_calls if call[0] == LEGACY_LOGRADIO_UUID
    )
    current_call = next(
        call for call in client.start_notify_calls if call[0] == LOGRADIO_UUID
    )
    fromnum_call = next(
        call for call in client.start_notify_calls if call[0] == FROMNUM_UUID
    )

    assert callable(legacy_call[1]), (
        "Legacy log notification should register a callable handler"
    )
    assert callable(current_call[1]), (
        "Current log notification should register a callable handler"
    )
    assert callable(fromnum_call[1]), (
        "FROMNUM notification should register a callable handler"
    )

    iface.close()


def test_reconnect_scheduler_tracks_threads(monkeypatch):
    """ReconnectScheduler should start at most one reconnect thread and respect closing state."""

    state_manager = BLEStateManager()
    shutdown_event = threading.Event()

    class StubCoordinator:
        def __init__(self):
            """
            Initialize the instance and create an empty `created` list for recording created items.
            """
            self.created = []

        def create_thread(self, target, name, *, daemon=True, args=(), kwargs=None):
            """
            Create and record a lightweight thread-like object used in tests.

            Parameters:
                target: The callable that would be executed by the thread.
                name: The thread's name.
                daemon: Whether the thread is a daemon.
                args: Positional arguments to pass to `target`.
                kwargs: Keyword arguments to pass to `target`.

            Returns:
                A SimpleNamespace representing the thread with attributes `target`, `args`, `name`, `daemon`, `kwargs`, `started` and an `is_alive()` method; the object is appended to `self.created`.
            """
            thread = SimpleNamespace(
                target=target,
                args=args,
                name=name,
                daemon=daemon,
                kwargs=kwargs if kwargs is not None else {},
                started=False,
            )
            thread.is_alive = lambda: thread.started
            self.created.append(thread)
            return thread

        @staticmethod
        def start_thread(thread):
            """
            Mark a thread-like object's `started` attribute as True.

            Parameters:
                thread (object): A thread-like object exposing a writable `started` attribute.
            """
            thread.started = True

    worker = SimpleNamespace(attempt_reconnect_loop=lambda *_args, **_kwargs: None)
    coordinator = StubCoordinator()
    scheduler = ReconnectScheduler(
        state_manager, state_manager.lock, coordinator, worker
    )

    assert scheduler.schedule_reconnect(True, shutdown_event) is True
    assert len(coordinator.created) == 1
    assert scheduler.schedule_reconnect(True, shutdown_event) is False

    scheduler.clear_thread_reference()
    assert scheduler._reconnect_thread is None

    assert state_manager.transition_to(ConnectionState.CONNECTING) is True
    assert state_manager.transition_to(ConnectionState.CONNECTED) is True
    assert state_manager.transition_to(ConnectionState.DISCONNECTING) is True
    assert scheduler.schedule_reconnect(True, shutdown_event) is False


def test_reconnect_worker_successful_attempt():
    """ReconnectWorker should reconnect and clear thread references on success; cleanup/resubscribe are handled by the interface layer, not the worker."""

    class StubPolicy:
        def __init__(self):
            """
            Create a stub retry policy used by reconnect tests.

            Attributes:
                reset_called (bool): True if reset() has been invoked.
                _attempt_count (int): Number of connection attempts recorded.
            """
            self.reset_called = False
            self._attempt_count = 0

        def reset(self):
            """
            Reset the retry policy to its initial state.

            Sets the internal attempt counter to zero and marks that a reset occurred by setting `reset_called` to True.
            """
            self.reset_called = True
            self._attempt_count = 0

        def get_attempt_count(self):
            """
            Retrieve the policy's recorded reconnect attempt count.

            Returns:
                int: The number of reconnect attempts recorded.
            """
            return self._attempt_count

        def next_attempt(self):
            """
            Return the next retry delay and whether another retry should be attempted.

            Also increments the internal attempt counter.

            Returns:
                delay_continue (tuple): A tuple (delay_seconds, continue_retry) where
                    delay_seconds (float): Seconds to wait before the next attempt.
                    continue_retry (bool): `True` to perform another attempt, `False` to stop.
            """
            self._attempt_count += 1
            return 0.1, False

    class StubNotificationManager:
        def __init__(self):
            """
            Initialize tracking state for the stub notification manager.

            Attributes:
                cleaned (int): Number of times cleanup_all() was called.
                resubscribed (list[tuple]): Tuples recorded by resubscribe_all(client, timeout); each tuple is (client, timeout).
            """
            self.cleaned = 0
            self.resubscribed = []

        def cleanup_all(self):
            """
            Record that all notifications were cleaned.

            Increments the instance counter tracking how many cleanup operations have occurred.
            """
            self.cleaned += 1

        def resubscribe_all(self, client, timeout):
            """
            Record a BLE client resubscription along with its timeout.

            Parameters:
                client: The BLE client instance that was resubscribed.
                timeout: Notification resubscription timeout in seconds.
            """
            self.resubscribed.append((client, timeout))

    class StubScheduler:
        def __init__(self):
            """
            Initialize the instance and set its cleared flag.

            Attributes:
                cleared (bool): True when the instance has been cleared; initially False.
            """
            self.cleared = False

        def clear_thread_reference(self):
            """
            Clear the scheduler's reference to an active reconnect thread.

            Marks the scheduler as not holding an active reconnect thread so a new reconnect thread may be scheduled.
            """
            self.cleared = True

    class DummyInterface:
        BLEError = RuntimeError

        def __init__(self):
            """
            Create a minimal stub interface used by reconnect-related tests.

            Provides lightweight test doubles for reconnect policy, notification manager, state manager, and scheduler, and records calls to connect.

            Attributes:
                _reconnect_policy (StubPolicy): Controls retry/backoff behavior for reconnect attempts.
                _notification_manager (StubNotificationManager): Tracks cleanup and resubscribe calls.
                _state_manager (SimpleNamespace): Exposes `is_closing` (bool) to simulate shutdown state.
                _reconnect_scheduler (StubScheduler): Manages reconnect thread references and clearing.
                auto_reconnect (bool): Whether automatic reconnect attempts are enabled.
                is_connection_closing (bool): Simulates an in-progress connection close.
                is_connection_connected (bool): Simulates an active connection state.
                address (str): Device address used for connect attempts.
                client: Placeholder BLE client object.
                connect_calls (list): Records addresses passed to `connect` for test assertions.
            """
            self._reconnect_policy = StubPolicy()
            self._notification_manager = StubNotificationManager()
            self._state_manager = SimpleNamespace(is_closing=False)
            self._reconnect_scheduler = StubScheduler()
            self.auto_reconnect = True
            self.is_connection_closing = False
            self.is_connection_connected = False
            self.address = "addr"
            self.client = object()
            self.connect_calls = []

        def connect(self, address):
            """
            Record a connection attempt for the given device address.

            Parameters:
                address (Any): Bluetooth address or device identifier that was attempted; appended to the instance's `connect_calls` list.
            """
            self.connect_calls.append(address)

    iface = DummyInterface()
    worker = ReconnectWorker(iface, iface._reconnect_policy)
    worker.attempt_reconnect_loop(True, threading.Event())

    assert iface.connect_calls == ["addr"]
    assert iface._notification_manager.cleaned == 0
    assert len(iface._notification_manager.resubscribed) == 0
    assert iface._reconnect_policy.reset_called is True
    assert iface._reconnect_scheduler.cleared is True


def test_reconnect_worker_respects_retry_limits(monkeypatch):
    """ReconnectWorker should obey retry policy decisions when connect keeps failing."""

    sleep_calls = []

    # Mock shutdown_event.wait to capture the sleep delay instead of actually waiting
    def mock_wait(timeout=None):
        if timeout is not None:
            sleep_calls.append(timeout)
        # Return False to simulate timeout (not interrupted by shutdown)
        return False

    class LimitedPolicy:
        def __init__(self):
            """
            Initialize the stub policy with counters used by tests.

            Attributes
            ----------
                reset_called (bool): True if `reset()` has been invoked.
                attempts (int): Number of connection attempts recorded.
            """
            self.reset_called = False
            self.attempts = 0

        def reset(self):
            """
            Mark the retry policy as reset and clear its attempt counter.

            Sets the internal `reset_called` flag to True and resets `attempts` to 0.
            """
            self.reset_called = True
            self.attempts = 0

        def get_attempt_count(self):
            """
            Report the number of reconnect attempts recorded by the policy.

            Returns:
                int: The number of attempts recorded so far.
            """
            return self.attempts

        def next_attempt(self):
            """
            Compute the delay before the next retry and whether to attempt another retry.

            Returns:
                tuple: (delay_seconds, continue_flag)
                    - delay_seconds (float): Delay in seconds before the next retry (0.25).
                    - continue_flag (bool): `True` to perform another retry, `False` to stop.
            """
            self.attempts += 1
            return 0.25, self.attempts < 2

    class StubNotificationManager:
        def __init__(self):
            """
            Create a new instance and set up the cleanup counter.

            Attributes:
                cleaned (int): Number of cleanup operations performed; starts at 0.
            """
            self.cleaned = 0

        def cleanup_all(self):
            """
            Record that all notifications were cleaned.

            Increments the instance counter tracking how many cleanup operations have occurred.
            """
            self.cleaned += 1

        def resubscribe_all(self, *_args, **_kwargs):  # pragma: no cover - no client
            """
            Indicate that resubscription must not be attempted when no client is available.

            Raises:
                AssertionError: Always raised with the message "Should not resubscribe without a client".
            """
            raise AssertionError("Should not resubscribe without a client")

    class StubScheduler:
        def __init__(self):
            """
            Initialize the instance and set its cleared state.

            The `cleared` attribute indicates whether the instance has been cleared; it is initialized to False.
            """
            self.cleared = False

        def clear_thread_reference(self):
            """
            Clear the scheduler's reference to an active reconnect thread.

            Marks the scheduler as not holding an active reconnect thread so a new reconnect thread may be scheduled.
            """
            self.cleared = True

    class FailingInterface:
        BLEError = RuntimeError

        def __init__(self):
            """
            Initialize a minimal stub interface used by reconnect tests.

            Attributes:
                _reconnect_policy (LimitedPolicy): Policy controlling reconnect attempts.
                _notification_manager (StubNotificationManager): Manages notification cleanup and resubscription.
                _state_manager (SimpleNamespace): Runtime state flags (contains `is_closing`).
                _reconnect_scheduler (StubScheduler): Scheduler that manages reconnect threads.
                auto_reconnect (bool): Whether automatic reconnect attempts are enabled.
                is_connection_closing (bool): Indicates an in-progress connection close.
                is_connection_connected (bool): Indicates whether the interface is currently connected.
                address (str): Remote device address used for connection attempts.
                client: Placeholder for the BLE client instance (initially None).
                connect_attempts (int): Counter of connect() invocation attempts.
            """
            self._reconnect_policy = LimitedPolicy()
            self._notification_manager = StubNotificationManager()
            self._state_manager = SimpleNamespace(is_closing=False)
            self._reconnect_scheduler = StubScheduler()
            self.auto_reconnect = True
            self.is_connection_closing = False
            self.is_connection_connected = False
            self.address = "addr"
            self.client = None
            self.connect_attempts = 0

        def connect(self, *_args, **_kwargs):
            """
            Simulate a failing connect attempt for testing.

            Increments self.connect_attempts and then raises a BLEError with message "boom".

            Raises:
                self.BLEError: always raised with the message "boom".
            """
            self.connect_attempts += 1
            raise self.BLEError("boom")

    iface = FailingInterface()
    worker = ReconnectWorker(iface, iface._reconnect_policy)
    shutdown_event = threading.Event()
    monkeypatch.setattr(shutdown_event, "wait", mock_wait)

    worker.attempt_reconnect_loop(True, shutdown_event)

    assert iface.connect_attempts == 2
    assert iface._notification_manager.cleaned == 0
    assert sleep_calls == [0.25]
    assert iface._reconnect_policy.reset_called is True
    assert iface._reconnect_scheduler.cleared is True
