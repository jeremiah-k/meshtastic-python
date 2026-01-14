"""Tests for the BLE interface module - Core functionality."""

import asyncio
import inspect
import logging
import threading
import time
from types import SimpleNamespace
from typing import (
    Any,
    Awaitable,
    Callable,
    Dict,
    List,
    Optional,
    Protocol,
    TYPE_CHECKING,
    cast,
)

import pytest
from bleak.exc import BleakError

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
    BLEStateManager,
    BLEDevice,
    ConnectionState,
    ConnectionValidator,
    ConnectedStrategy,
    DiscoveryManager,
    ReconnectScheduler,
    ReconnectWorker,
)

if TYPE_CHECKING:
    class _PubProtocol(Protocol):
        def sendMessage(self, topic: str, **kwargs: Any) -> None:
            """
            Publish a message on the given topic with the supplied keyword arguments as message payload.
            
            Parameters:
            	topic (str): Topic name to publish the message under.
            	**kwargs (Any): Key/value pairs included in the message payload.
            """
            ...

    pub: _PubProtocol
else:  # pragma: no cover - import only at runtime
    from pubsub import pub


def _create_ble_device(address: str, name: str) -> BLEDevice:
    """
    Create a BLEDevice compatible with different bleak constructor signatures.
    
    Parameters:
        address (str): BLE address for the fake device.
        name (str): Human-readable name for the fake device.
    
    Returns:
        BLEDevice: A BLEDevice instance constructed with the arguments supported by the installed bleak version.
    """
    params: Dict[str, Any] = {"address": address, "name": name}
    signature = inspect.signature(ble_mod.BLEDevice.__init__)
    if "details" in signature.parameters:
        params["details"] = {}
    if "rssi" in signature.parameters:
        params["rssi"] = 0
    return ble_mod.BLEDevice(**params)


class _StrategyOverride(ConnectedStrategy):
    """
    Adapt an async callable into a ConnectedStrategy-compatible object for testing.
    """

    def __init__(
        self,
        delegate: Callable[[Optional[str], float], Awaitable[List[BLEDevice]]],
    ) -> None:
        """
        Initialize the strategy with a delegate coroutine that performs device discovery.
        
        Parameters:
            delegate (Callable[[Optional[str], float], Awaitable[List[BLEDevice]]]):
                Coroutine called as delegate(address, timeout) that returns a list of discovered
                BLEDevice objects for the given optional address and timeout.
        """
        self._delegate = delegate

    async def discover(self, address: Optional[str], timeout: float) -> List[BLEDevice]:
        """
        Perform device discovery by invoking the wrapped delegate with the given address and timeout.
        
        Parameters:
            address (Optional[str]): Address to filter discovery results for, or `None` to discover without address filtering.
            timeout (float): Maximum time in seconds to wait for discovery.
        
        Returns:
            List[BLEDevice]: List of discovered BLEDevice instances matching the request.
        """
        return await self._delegate(address, timeout)


def test_find_device_returns_single_scan_result(monkeypatch):
    """find_device should return the lone scanned device."""
    # BLEDevice and BLEInterface already imported at top as ble_mod.BLEDevice, ble_mod.BLEInterface

    iface = object.__new__(ble_mod.BLEInterface)
    scanned_device = _create_ble_device(address="11:22:33:44:55:66", name="Test Device")
    iface._discovery_manager = SimpleNamespace(
        discover_devices=lambda _address: [scanned_device]
    )

    result = ble_mod.BLEInterface.find_device(iface, None)

    assert result is scanned_device


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


def test_find_device_uses_connected_fallback_when_scan_empty(monkeypatch):
    """find_device should fall back to connected-device lookup when scan is empty."""
    # BLEDevice and BLEInterface already imported at top as ble_mod.BLEDevice, ble_mod.BLEInterface

    iface = object.__new__(ble_mod.BLEInterface)
    fallback_device = _create_ble_device(address="AA:BB:CC:DD:EE:FF", name="Fallback")
    iface._discovery_manager = SimpleNamespace(
        discover_devices=lambda addr: [fallback_device] if addr else []
    )

    result = BLEInterface.find_device(iface, "aa-bb-cc-dd-ee-ff")

    assert result is fallback_device


def test_find_device_multiple_matches_raises(monkeypatch):
    """Providing an address that matches multiple devices should raise BLEError."""
    # BLEDevice and BLEInterface already imported at top as ble_mod.BLEDevice, ble_mod.BLEInterface

    iface = object.__new__(ble_mod.BLEInterface)
    devices = [
        _create_ble_device(address="AA:BB:CC:DD:EE:FF", name="Meshtastic-1"),
        _create_ble_device(address="AA-BB-CC-DD-EE-FF", name="Meshtastic-2"),
    ]
    iface._discovery_manager = SimpleNamespace(
        discover_devices=lambda _addr: devices
    )

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
            raise AssertionError(
                "BleakScanner should not be instantiated when guard fails"
            )

    monkeypatch.setattr(
        "meshtastic.interfaces.ble.discovery.BleakScanner", BoomScanner
    )

    strategy = ble_mod.ConnectedStrategy()
    result = asyncio.run(strategy.discover(address="AA:BB", timeout=1.0))
    assert result == []


def test_discovery_manager_filters_meshtastic_devices(monkeypatch):
    """DiscoveryManager should return only devices advertising the Meshtastic service UUID."""

    filtered_device = _create_ble_device("AA:BB:CC:DD:EE:FF", "Filtered")
    other_device = _create_ble_device("11:22:33:44:55:66", "Other")

    class FakeClient:
        def __enter__(self):
            """
            Enter the context and return the context manager instance.
            
            Returns:
                self: The context manager instance.
            """
            return self

        def __exit__(self, exc_type, exc, tb):
            """
            Indicates that the context manager does not suppress exceptions raised inside the with-block.

            Parameters:
                exc_type (type | None): The exception class raised in the with-block, or None if no exception occurred.
                exc (BaseException | None): The exception instance raised in the with-block, or None.
                tb (types.TracebackType | None): The traceback object for the exception, or None.

            Returns:
                bool: `False` to signal that any exception should be propagated (not suppressed).
            """
            return False

        def discover(self, **_kwargs):
            """
            Provide a fake discovery result for tests mapping labels to (device, advertisement) pairs.
            
            Returns:
                dict: Mapping with two entries:
                    - "filtered": (device, advertisement) where `advertisement.service_uuids` includes `SERVICE_UUID`.
                    - "other": (device, advertisement) where `advertisement.service_uuids` contains a non-Meshtastic service identifier.
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
            Assert that a connected-device fallback must not be invoked.
            
            Parameters:
                coro: The coroutine that would be awaited for a connected-device fallback.
            
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
            Enter the context and return the context manager instance.
            
            Returns:
                self: The context manager instance.
            """
            return self

        def __exit__(self, exc_type, exc, tb):
            """
            Indicates that the context manager does not suppress exceptions raised inside the with-block.

            Parameters:
                exc_type (type | None): The exception class raised in the with-block, or None if no exception occurred.
                exc (BaseException | None): The exception instance raised in the with-block, or None.
                tb (types.TracebackType | None): The traceback object for the exception, or None.

            Returns:
                bool: `False` to signal that any exception should be propagated (not suppressed).
            """
            return False

        def discover(self, **_kwargs):
            """
            Produce an empty mapping of discovered BLE devices.
            
            Returns:
                dict: Empty mapping of discovered device addresses to metadata.
            """
            return {}

        @staticmethod
        def async_await(coro, timeout=None):
            """
            Run an awaitable to completion and return its result.
            
            Parameters:
                coro (Awaitable): The coroutine or awaitable to execute.
            
            Returns:
                Any: The value returned by the awaitable.
            """
            return asyncio.run(coro)

    monkeypatch.setattr(ble_mod, "BLEClient", lambda **_kwargs: FakeClient())

    manager = DiscoveryManager()

    async def fake_connected(address: Optional[str], timeout: float) -> List[BLEDevice]:
        """
        Provide the predefined fallback device when called with the expected address and timeout.
        
        Parameters:
            address (Optional[str]): Expected device address; must be "AA:BB".
            timeout (float): Expected scan timeout; must equal ble_mod.BLEConfig.BLE_SCAN_TIMEOUT.
        
        Returns:
            List[BLEDevice]: A list containing the single `fallback_device`.
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
            Enter the context and return the context manager instance.
            
            Returns:
                self: The context manager instance.
            """
            return self

        def __exit__(self, exc_type, exc, tb):
            """
            Indicates that the context manager does not suppress exceptions raised inside the with-block.

            Parameters:
                exc_type (type | None): The exception class raised in the with-block, or None if no exception occurred.
                exc (BaseException | None): The exception instance raised in the with-block, or None.
                tb (types.TracebackType | None): The traceback object for the exception, or None.

            Returns:
                bool: `False` to signal that any exception should be propagated (not suppressed).
            """
            return False

        def discover(self, **_kwargs):
            """
            Produce an empty mapping of discovered BLE devices.
            
            Returns:
                dict: Empty mapping of discovered device addresses to metadata.
            """
            return {}

        @staticmethod
        def async_await(coro, timeout=None):  # pragma: no cover - fallback should not be hit
            """
            Run a coroutine until completion and return its result.
            
            Parameters:
                coro (coroutine): Coroutine to execute.
            
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
        Test helper coroutine that marks a connected-fallback as invoked and returns no devices.

        Parameters:
            address (str): Address to search for; used only for signaling in tests.
            timeout (float): Maximum time to wait in seconds; not used by this stub.

        Returns:
            list: An empty list indicating no connected devices were found.

        Side effects:
            Sets the enclosing `fallback_called` variable to True to indicate the fallback was exercised.
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
        state_manager, state_manager._state_lock, BLEInterface.BLEError
    )
    client = DummyClient()
    client.is_connected = lambda: True

    ble_like = cast(BLEClient, client)
    assert validator.check_existing_client(ble_like, None, None, None) is True
    assert (
        validator.check_existing_client(ble_like, "dummy", "dummy", "dummy") is True
    )
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
        Record a pubsub message invocation by appending a (topic, kwargs) tuple to the module-level `calls` list.

        Parameters
        ----------
            topic (str): Pubsub topic name.
            **kwargs: Additional message fields to capture alongside the topic.

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


def test_close_clears_ble_threads(monkeypatch):
    """Closing the interface should leave no BLE* threads running."""
    # threading already imported at top

    client = DummyClient()
    iface = _build_interface(monkeypatch, client)

    iface.close()

    # Poll for thread cleanup with a reasonable timeout
    max_wait_time = 1.0  # Maximum time to wait for thread cleanup
    poll_interval = 0.05  # Time between checks
    elapsed_time = 0.0
    lingering = []  # Initialize to ensure it's defined outside the loop

    while elapsed_time < max_wait_time:
        # Check for specific BLE interface threads that should be cleaned up
        # BLEClient thread might persist in test environment, so focus on interface-managed threads
        lingering = [
            thread.name
            for thread in threading.enumerate()
            if thread.name.startswith("BLE") and thread.name != "BLEClient"
        ]

        if not lingering:
            break  # No lingering threads found

        time.sleep(poll_interval)
        elapsed_time += poll_interval

    assert not lingering, (
        f"Found lingering BLE threads after {max_wait_time}s: {lingering}"
    )


def test_receive_thread_specific_exceptions(monkeypatch, caplog):
    """
    Verify that the BLE receive thread treats specific exceptions as fatal: it logs a "Fatal error in BLE receive thread" message and invokes the interface's close().

    The test iterates over RuntimeError, OSError, and BleakError by injecting a client whose read_gatt_char raises the exception, triggers the receive loop, and asserts that the fatal log entry is present and that close() was called.
    """
    # logging and threading already imported at top

    # BleakError already imported at top as ble_mod.BleakError

    # Set logging level to DEBUG to capture debug messages
    caplog.set_level(logging.DEBUG)

    # The exceptions that should be caught and handled as fatal
    handled_exceptions = [
        RuntimeError,
        OSError,
        BleakError,  # Should be fatal if not a disconnect
    ]

    class ExceptionClient(DummyClient):
        """Mock client that raises specific exceptions for testing."""

        def __init__(self, exception_type):
            """
            Initialize a test client that will raise a configured exception from its faulting methods.
            
            Parameters:
                exception_type (Exception | type): An exception instance or exception class to be raised when the client's faulting
                    methods are invoked.
            """
            super().__init__()
            self.exception_type = exception_type

        def read_gatt_char(self, *_args, **_kwargs):
            """
            Simulate a failing GATT characteristic read by raising the configured exception.

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
            Mark the close event and call the original close function.
            
            Sets the provided `close_called` event to signal that close was invoked, then returns the value returned by `original_close()`.
            
            Returns:
                Any: Value returned by `original_close()`.
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
            logging.warning(f"Cleanup error in iface.close(): {exc!r}")


def test_log_notification_registration(monkeypatch):
    """Test that log notifications are properly registered for both legacy and current log UUIDs."""
    # UUID constants already imported at top as ble_mod.FROMNUM_UUID, ble_mod.LEGACY_LOGRADIO_UUID, ble_mod.LOGRADIO_UUID

    class MockClientWithLogChars(DummyClient):
        """Mock client that has log characteristics."""

        def __init__(self):
            """
            Initialize the mock client and configure notification tracking and reported characteristics.
            
            Attributes:
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
            Determine whether the client's characteristic map contains the specified characteristic UUID.
            
            Parameters:
                uuid (str | uuid.UUID): Characteristic UUID to check.
            
            Returns:
                bool: `True` if the UUID is present, `False` otherwise.
            """
            return self.has_characteristic_map.get(uuid, False)

        def start_notify(self, *_args, **_kwargs):
            """
            Record a notification registration request by saving the characteristic UUID and its handler.
            
            Parameters:
                _args (tuple): If at least two positional arguments are provided, the first is treated as the characteristic UUID
                    and the second as the notification handler; the pair is appended to `self.start_notify_calls`.
                _kwargs (dict): Accepted and ignored.
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

        def create_thread(self, target, args, name, daemon):
            """
            Create a lightweight thread-like object used by tests to track created threads.

            Parameters:
                target (callable): The callable that would be run by the thread.
                args (tuple): Arguments to pass to `target`.
                name (str): The thread's name.
                daemon (bool): Whether the thread is a daemon.

            Returns:
                thread (SimpleNamespace): A thread-like object with attributes `target`, `args`, `name`, `daemon`, `started` and an `is_alive()` method. The object is also appended to `self.created`.
            """
            thread = SimpleNamespace(
                target=target,
                args=args,
                name=name,
                daemon=daemon,
                started=False,
            )
            thread.is_alive = lambda: thread.started
            self.created.append(thread)
            return thread

        @staticmethod
        def start_thread(thread):
            """
            Mark a thread-like object's `started` attribute as True.
            """
            thread.started = True

    worker = SimpleNamespace(attempt_reconnect_loop=lambda *_args, **_kwargs: None)
    coordinator = StubCoordinator()
    scheduler = ReconnectScheduler(
        state_manager, state_manager._state_lock, coordinator, worker
    )

    assert scheduler.schedule_reconnect(True, shutdown_event) is True
    assert len(coordinator.created) == 1
    assert scheduler.schedule_reconnect(True, shutdown_event) is False

    stub_thread = coordinator.created[0]
    monkeypatch.setattr(ble_mod, "current_thread", lambda: stub_thread)
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
            Initialize the stub retry policy used by reconnect tests.
            
            Attributes:
                reset_called (bool): True if reset() has been invoked.
                _attempt_count (int): Number of connection attempts recorded.
            """
            self.reset_called = False
            self._attempt_count = 0

        def reset(self):
            """
            Reset the policy state for retry attempts.

            Sets the internal attempt counter to zero and records that a reset occurred by setting `reset_called` to True.
            """
            self.reset_called = True
            self._attempt_count = 0

        def get_attempt_count(self):
            """
            Get the number of reconnect attempts recorded by the policy.
            
            Returns:
                int: The number of reconnect attempts recorded.
            """
            return self._attempt_count

        def next_attempt(self):
            """
            Provide the next retry delay and whether another retry should be attempted.
            
            Also increments the internal attempt counter.
            
            Returns:
                delay_continue (tuple): A tuple containing:
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
                resubscribed (list): List of tuples recorded by resubscribe_all(client, timeout); each tuple is (client, timeout).
            """
            self.cleaned = 0
            self.resubscribed = []

        def cleanup_all(self):
            """
            Record that all notifications have been cleaned.
            
            Increments the instance's `cleaned` counter to indicate a cleanup operation occurred.
            """
            self.cleaned += 1

        def resubscribe_all(self, client, timeout):
            """
            Record that a BLE client has been resubscribed with a specific timeout.
            
            Parameters:
                client: The BLE client instance that was resubscribed.
                timeout (float): Notification resubscription timeout in seconds.
            """
            self.resubscribed.append((client, timeout))

    class StubScheduler:
        def __init__(self):
            """
            Create a new instance and initialize its cleared state.
            
            Attributes:
                cleared: True when the instance has been cleared; initialized to False.
            """
            self.cleared = False

        def clear_thread_reference(self):
            """
            Mark that the scheduler no longer holds a reference to an active reconnect thread.
            
            Sets an internal flag allowing a new reconnect thread to be scheduled.
            """
            self.cleared = True

    class DummyInterface:
        BLEError = RuntimeError

        def __init__(self):
            """
            Create a minimal stub interface used by reconnect-related tests.
            
            Attributes:
                _reconnect_policy (StubPolicy): Controls retry/backoff behavior for reconnect attempts.
                _notification_manager (StubNotificationManager): Tracks cleanup and resubscribe calls.
                _state_manager (SimpleNamespace): Contains at least `is_closing` (bool) indicating shutdown state.
                _reconnect_scheduler (StubScheduler): Manages reconnect thread references.
                auto_reconnect (bool): Whether automatic reconnect attempts are enabled.
                is_connection_closing (bool): Simulates an in-progress connection close.
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
            self.address = "addr"
            self.client = object()
            self.connect_calls = []

        def connect(self, address):
            """
            Record a connection attempt for the specified device address.
            
            Parameters:
                address (str): Bluetooth address or device identifier used for the connection attempt.
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
    monkeypatch.setattr(ble_mod, "_sleep", lambda delay: sleep_calls.append(delay))

    class LimitedPolicy:
        def __init__(self):
            """
            Initialize the stub policy with counters used by tests.
            
            Attributes:
                reset_called (bool): True if `reset()` has been invoked.
                attempts (int): Number of connection attempts recorded.
            """
            self.reset_called = False
            self.attempts = 0

        def reset(self):
            """
            Mark the policy as reset and clear its retry attempt counter.
            
            Sets the `reset_called` flag to True and sets `attempts` to 0.
            """
            self.reset_called = True
            self.attempts = 0

        def get_attempt_count(self):
            """
            Report the number of reconnect attempts recorded by the policy.

            Returns:
                int: Number of attempts made so far.
            """
            return self.attempts

        def next_attempt(self):
            """
            Determine the delay before the next retry and whether another retry should occur.
            
            Returns:
                (delay_seconds, continue_flag): `delay_seconds` is 0.25. `continue_flag` is `True` if the internal attempt count after incrementing is less than 2, `False` otherwise.
            """
            self.attempts += 1
            return 0.25, self.attempts < 2

    class StubNotificationManager:
        def __init__(self):
            """
            Initialize the instance and set the cleanup counter.
            
            Attributes:
                cleaned (int): Count of cleanup operations performed; initialized to 0.
            """
            self.cleaned = 0

        def cleanup_all(self):
            """
            Record that all notifications have been cleaned.
            
            Increments the instance's `cleaned` counter to indicate a cleanup operation occurred.
            """
            self.cleaned += 1

        def resubscribe_all(self, *_args, **_kwargs):  # pragma: no cover - no client
            """
            Raise an AssertionError indicating resubscription must not be attempted without a client.
            
            This function always raises an AssertionError with the message "Should not resubscribe without a client".
            
            Raises:
                AssertionError: Always raised with the message "Should not resubscribe without a client".
            """
            raise AssertionError("Should not resubscribe without a client")

    class StubScheduler:
        def __init__(self):
            """
            Create a new instance and initialize its cleared state.
            
            Attributes:
                cleared: True when the instance has been cleared; initialized to False.
            """
            self.cleared = False

        def clear_thread_reference(self):
            """
            Mark that the scheduler no longer holds a reference to an active reconnect thread.
            
            Sets an internal flag allowing a new reconnect thread to be scheduled.
            """
            self.cleared = True

    class FailingInterface:
        BLEError = RuntimeError

        def __init__(self):
            """
            Create a minimal stub interface used by reconnect tests.

            Attributes:
                _reconnect_policy: Policy object controlling reconnect attempts (LimitedPolicy instance).
                _notification_manager: Notification manager used to cleanup/resubscribe (StubNotificationManager instance).
                _state_manager: SimpleNamespace with runtime state flags (contains `is_closing`).
                _reconnect_scheduler: Scheduler responsible for managing reconnect threads (StubScheduler instance).
                auto_reconnect (bool): Whether automatic reconnect attempts are enabled.
                is_connection_closing (bool): Flag indicating an in-progress connection close.
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
            self.address = "addr"
            self.client = None
            self.connect_attempts = 0

        def connect(self, *_args, **_kwargs):
            """
            Simulate a failing connect that records an attempt and always raises a BLEError.
            
            Increments self.connect_attempts by 1, then raises self.BLEError("boom").
            
            Raises:
                self.BLEError: always raised with the message "boom".
            """
            self.connect_attempts += 1
            raise self.BLEError("boom")

    iface = FailingInterface()
    worker = ReconnectWorker(iface, iface._reconnect_policy)
    worker.attempt_reconnect_loop(True, threading.Event())

    assert iface.connect_attempts == 2
    assert iface._notification_manager.cleaned == 0
    assert sleep_calls == [0.25]
    assert iface._reconnect_policy.reset_called is True
    assert iface._reconnect_scheduler.cleared is True
