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
import meshtastic.ble_interface as ble_mod
from meshtastic.ble_interface import (
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
    BLEError,
)

if TYPE_CHECKING:

    class _PubProtocol(Protocol):
        def sendMessage(self, topic: str, **kwargs: Any) -> None: ...

    pub: _PubProtocol
else:  # pragma: no cover - import only at runtime
    from pubsub import pub


def _create_ble_device(address: str, name: str) -> BLEDevice:
    """
    Instantiate a BLEDevice while tolerating signature differences across bleak versions.

    Parameters
    ----------
        address (str): BLE address for the fake device.
        name (str): Human-readable name for the fake device.

    Returns
    -------
        BLEDevice: Instance created using whichever keyword arguments the current bleak version expects.

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
        self._delegate = delegate

    async def discover(self, address: Optional[str], timeout: float) -> List[BLEDevice]:
        return await self._delegate(address, timeout)


class _BackgroundAsyncRunner:
    """
    Minimal async runner that executes coroutines on a background thread for testing.
    """

    def __init__(self):
        self.calls: List[Awaitable[Any]] = []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def async_await(self, coro):
        """
        Run `coro` on a dedicated event loop thread and return its result.
        """

        self.calls.append(coro)
        result: Dict[str, Any] = {"error": None, "value": None}

        def _worker():
            try:
                result["value"] = asyncio.run(coro)
            except BaseException as exc:  # pragma: no cover - test helper
                result["error"] = exc

        thread = threading.Thread(target=_worker, name="TestAsyncRunner", daemon=True)
        thread.start()
        thread.join()
        if result["error"]:
            raise result["error"]
        return result["value"]


def test_find_device_returns_single_scan_result(monkeypatch):
    """find_device should return the lone scanned device."""
    # BLEDevice and BLEInterface already imported at top as ble_mod.BLEDevice, ble_mod.BLEInterface

    iface = object.__new__(ble_mod.BLEInterface)
    scanned_device = _create_ble_device(address="11:22:33:44:55:66", name="Test Device")
    monkeypatch.setattr(ble_mod.BLEInterface, "scan", lambda: [scanned_device])

    result = ble_mod.BLEInterface.find_device(iface, None)

    assert result is scanned_device


def test_find_device_uses_connected_fallback_when_scan_empty(monkeypatch):
    """find_device should fall back to connected-device lookup when scan is empty."""
    # BLEDevice and BLEInterface already imported at top as ble_mod.BLEDevice, ble_mod.BLEInterface

    iface = object.__new__(ble_mod.BLEInterface)
    fallback_device = _create_ble_device(address="AA:BB:CC:DD:EE:FF", name="Fallback")
    monkeypatch.setattr(ble_mod.BLEInterface, "scan", lambda: [])

    def _fake_connected(_self, _address):
        """
        Return a list containing the predefined fallback_device used for connected-device lookups.

        Returns:
            list: A single-element list with the preset `fallback_device`.
        """
        return [fallback_device]

    monkeypatch.setattr(BLEInterface, "_find_connected_devices", _fake_connected)

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
    monkeypatch.setattr(BLEInterface, "scan", lambda: devices)

    with pytest.raises(BLEError) as excinfo:
        BLEInterface.find_device(iface, "aa bb cc dd ee ff")

    assert "Multiple Meshtastic BLE peripherals found matching" in str(excinfo.value)


def test_find_connected_devices_skips_private_backend_when_guard_fails(monkeypatch):
    """When the version guard disallows the fallback, the private backend is never touched."""
    # BLEInterface already imported at top as ble_mod.BLEInterface

    iface = object.__new__(ble_mod.BLEInterface)

    monkeypatch.setattr(
        "meshtastic.ble_interface._bleak_supports_connected_fallback", lambda: False
    )

    class BoomScanner:
        """Mock scanner that raises an exception when instantiated."""

        def __init__(self):
            """
            Prevent instantiation when the private-backend guard disallows it.

            Raises:
                AssertionError: Always raised with message "BleakScanner should not be instantiated when guard fails".
            """
            raise AssertionError(
                "BleakScanner should not be instantiated when guard fails"
            )

    monkeypatch.setattr("meshtastic.ble_interface.BleakScanner", BoomScanner)

    result = BLEInterface._find_connected_devices(iface, "AA:BB")

    assert result == []


def test_discovery_manager_filters_meshtastic_devices(monkeypatch):
    """DiscoveryManager should return only devices advertising the Meshtastic service UUID."""

    filtered_device = _create_ble_device("AA:BB:CC:DD:EE:FF", "Filtered")
    other_device = _create_ble_device("11:22:33:44:55:66", "Other")

    class FakeClient:
        def __enter__(self):
            """
            Enter the context manager and provide the context object.

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
            Provide a fake discovery result used by tests that maps labels to (device, advertisement) pairs.

            Returns:
                dict: A mapping with two entries:
                    - "filtered": tuple(device, advertisement) where `advertisement.service_uuids` includes `SERVICE_UUID`.
                    - "other": tuple(device, advertisement) where `advertisement.service_uuids` contains a non-Meshtastic service identifier.
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

        def async_await(self, coro):
            """
            Assert that a connected-device fallback must not be used.

            Parameters:
                coro: The coroutine that would be awaited for a connected-device fallback.

            Raises:
                AssertionError: Always raised with the message "Fallback should not be attempted when scan succeeds".
            """
            raise AssertionError("Fallback should not be attempted when scan succeeds")

        # Mock BleakScanner.discover to return our test devices
        async def mock_discover(**kwargs):
            print(f"mock_discover called with kwargs: {kwargs}")
            print(f"SERVICE_UUID: {SERVICE_UUID}")
            # Create BLEDevice objects with proper metadata
            from bleak.backends.device import BLEDevice

            filtered_device_with_metadata = BLEDevice(
                "AA:BB:CC:DD:EE:FF", "Filtered", {"props": {"UUIDs": [SERVICE_UUID]}}
            )

            other_device_with_metadata = BLEDevice(
                "11:22:33:44:55:66",
                "Other",
                {"props": {"UUIDs": ["some-other-service"]}},
            )

            print(
                f"Created devices: {filtered_device_with_metadata.details}, {other_device_with_metadata.details}"
            )

            return [filtered_device_with_metadata, other_device_with_metadata]

        monkeypatch.setattr("bleak.BleakScanner.discover", mock_discover)

        manager = DiscoveryManager()

        devices = manager.discover_devices(address=None)

        assert len(devices) == 1
        assert devices[0].address == "AA:BB:CC:DD:EE:FF"


def test_discovery_manager_handles_running_loop_scan(monkeypatch):
    """DiscoveryManager should run scan coroutines via the async runner when an event loop is active."""

    filtered_device = SimpleNamespace(
        address="AA:BB:CC:DD:EE:FF",
        name="Filtered",
        metadata={"uuids": [SERVICE_UUID]},
    )

    async def mock_discover(**_kwargs):
        return [filtered_device]

    monkeypatch.setattr("bleak.BleakScanner.discover", mock_discover)

    runners: List[_BackgroundAsyncRunner] = []

    def _factory():
        runner = _BackgroundAsyncRunner()
        runners.append(runner)
        return runner

    manager = DiscoveryManager(ble_client_factory=_factory)

    async def _invoke():
        return manager.discover_devices(address=None)

    devices = asyncio.run(_invoke())

    assert devices == [filtered_device]
    assert len(runners) == 1
    assert len(runners[0].calls) == 1


def test_discovery_manager_handles_running_loop_fallback(monkeypatch):
    """Connected-device fallback should also execute via the async runner when an event loop is active."""

    async def mock_discover(**_kwargs):
        return []

    monkeypatch.setattr("bleak.BleakScanner.discover", mock_discover)

    fallback_device = _create_ble_device("AA:BB", "Fallback")
    runners: List[_BackgroundAsyncRunner] = []

    def _factory():
        runner = _BackgroundAsyncRunner()
        runners.append(runner)
        return runner

    manager = DiscoveryManager(ble_client_factory=_factory)

    async def fake_connected(address: Optional[str], timeout: float) -> List[BLEDevice]:
        assert address == "AA:BB"
        assert timeout == ble_mod.BLEConfig.BLE_SCAN_TIMEOUT
        return [fallback_device]

    manager.connected_strategy = _StrategyOverride(fake_connected)

    async def _invoke():
        return manager.discover_devices(address="AA:BB")

    devices = asyncio.run(_invoke())

    assert devices == [fallback_device]
    assert len(runners) == 1
    assert len(runners[0].calls) == 2


def test_discovery_manager_uses_connected_strategy_when_scan_empty(monkeypatch):
    """When no devices are discovered via BLE scan, DiscoveryManager should fall back to connected strategy."""

    fallback_device = _create_ble_device("AA:BB", "Fallback")

    class FakeClient:
        def __enter__(self):
            """
            Enter the context manager and provide the context object.

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
            Return an empty device discovery mapping.

            This stubbed discover method accepts and ignores any keyword arguments and always
            returns an empty dict representing no discovered BLE devices.

            Returns:
                dict: An empty mapping of discovered device addresses to metadata.
            """
            return {}

        @staticmethod
        def async_await(coro):
            """
            Execute a coroutine until completion and return its result.

            Parameters:
                coro (Awaitable): The coroutine or awaitable to run to completion.

            Returns:
                Any: The value returned by the coroutine.
            """
            return asyncio.run(coro)

    monkeypatch.setattr(ble_mod, "BLEClient", lambda **_kwargs: FakeClient())

    manager = DiscoveryManager()

    async def fake_connected(address: Optional[str], timeout: float) -> List[BLEDevice]:
        """
        Return a list containing the predefined fallback device when invoked with the expected address and timeout.

        Parameters:
            address (str): Expected device address; must be "AA:BB".
            timeout (float|int): Expected scan timeout; must equal ble_mod.BLEConfig.BLE_SCAN_TIMEOUT.

        Returns:
            list: A list containing the single `fallback_device`.
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
            Enter the context manager and provide the context object.

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
            Return an empty device discovery mapping.

            This stubbed discover method accepts and ignores any keyword arguments and always
            returns an empty dict representing no discovered BLE devices.

            Returns:
                dict: An empty mapping of discovered device addresses to metadata.
            """
            return {}

        @staticmethod
        def async_await(coro):  # pragma: no cover - fallback should not be hit
            """
            Execute a coroutine to completion and return its result.

            Parameters:
                coro (coroutine): The coroutine to execute.

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


def test_connection_validator_enforces_state(monkeypatch):
    """ConnectionValidator should block connections when interface is closing or already connecting."""

    state_manager = BLEStateManager()
    validator = ConnectionValidator(state_manager, state_manager._state_lock)

    validator.validate_connection_request()

    assert state_manager.transition_to(ConnectionState.CONNECTING) is True
    assert state_manager.transition_to(ConnectionState.CONNECTED) is True
    assert state_manager.transition_to(ConnectionState.DISCONNECTING) is True
    with pytest.raises(BLEError) as excinfo:
        validator.validate_connection_request()
    assert "closing" in str(excinfo.value)

    assert state_manager.transition_to(ConnectionState.DISCONNECTED) is True
    assert state_manager.transition_to(ConnectionState.CONNECTING) is True
    with pytest.raises(BLEError) as excinfo:
        validator.validate_connection_request()
    assert "connection in progress" in str(excinfo.value)


def test_connection_validator_existing_client_checks(monkeypatch):
    """check_existing_client should allow reuse only when the requested identifier matches."""

    state_manager = BLEStateManager()
    validator = ConnectionValidator(state_manager, state_manager._state_lock)
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

    for exc_type in handled_exceptions:
        # Clear caplog for each test
        caplog.clear()

        # Create a mock client that raises the specific exception
        class ExceptionClient(DummyClient):
            """Mock client that raises specific exceptions for testing."""

            def __init__(self, exception_type):
                """
                Create a test client configured to raise a specific exception from its faulting methods.

                Parameters
                ----------
                    exception_type (Exception | type): An exception instance or an exception class; the client will raise
                        this exception (or an instance of it) when its faulting methods are invoked.

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

        client = ExceptionClient(exc_type)
        iface = _build_interface(monkeypatch, client)

        # Mock the close method to track if it's called
        original_close = iface.close
        close_called = threading.Event()

        def mock_close(original_close=original_close, close_called=close_called):
            """
            Mark the close event and invoke the original close function.

            Sets the `close_called` event to signal that close was invoked, then calls and returns the result of `original_close()`.

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
            Initialize the mock client and set up notification tracking and characteristic availability.

            Attributes
            ----------
                start_notify_calls (list): Records the arguments of each start_notify invocation.
                has_characteristic_map (dict): Maps characteristic UUIDs to booleans indicating whether the
                    client reports that characteristic is present. By default, includes
                    LEGACY_LOGRADIO_UUID, LOGRADIO_UUID, and FROMNUM_UUID set to True.

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
            Check if the client's characteristic map contains the given characteristic UUID.

            Parameters:
                uuid (str | uuid.UUID): Characteristic UUID to check.

            Returns:
                bool: True if the UUID is present in the client's characteristic map, False otherwise.
            """
            return self.has_characteristic_map.get(uuid, False)

        def start_notify(self, *_args, **_kwargs):
            """
            Record a requested notification registration by saving the characteristic UUID and its handler.

            Parameters
            ----------
                _args (tuple): If two or more positional arguments are provided, the first is the characteristic UUID
                (usually a string) and the second is the notification handler (callable). When present,
                the pair is appended to `self.start_notify_calls`.
                _kwargs (dict): Additional keyword arguments are accepted but ignored by this test helper.

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
            Set the thread's `started` attribute to True.

            Parameters:
                thread: A thread-like object; its `started` attribute will be set to `True`.
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


def test_reconnect_worker_successful_attempt(monkeypatch):
    """ReconnectWorker should clean up, reconnect, resubscribe, and clear thread references on success."""

    class StubPolicy:
        def __init__(self):
            """
            Initialize the stub retry policy used by reconnect tests.

            Attributes:
                reset_called (bool): True if `reset()` has been invoked.
                _attempt_count (int): Counter of how many connection attempts have been recorded.
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
                attempt_count (int): The number of reconnect attempts that have been made.
            """
            return self._attempt_count

        def next_attempt(self):
            """
            Provide the next retry delay and whether to continue retry attempts; increments the internal attempt counter.

            Returns:
                tuple: (delay_seconds, continue_retry)
                delay_seconds (float): Seconds to wait before the next attempt.
                continue_retry (bool): True to perform another attempt, False to stop.
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
            Mark all notifications as cleaned.

            Increments an internal counter that tracks how many times cleanup has been performed.
            """
            self.cleaned += 1

        def resubscribe_all(self, client, timeout):
            """
            Record that the given client was resubscribed with the specified timeout.

            Parameters:
                client: The BLE client instance that was resubscribed.
                timeout (float): The notification resubscription timeout in seconds.
            """
            self.resubscribed.append((client, timeout))

    class StubScheduler:
        def __init__(self):
            """
            Initialize the instance and set its cleared state.

            Attributes:
                cleared (bool): Indicates whether the instance has been cleared; initially False.
            """
            self.cleared = False

        def clear_thread_reference(self):
            """
            Mark that the reconnect thread reference has been cleared.

            Sets an internal flag indicating the scheduler no longer holds a reference to an active reconnect thread, allowing a new reconnect thread to be created.
            """
            self.cleared = True

    class DummyInterface:
        BLEError = RuntimeError

        def __init__(self):
            """
            Initialize a minimal stub interface used by reconnect-related tests.

            Attributes:
                _reconnect_policy: StubPolicy instance controlling retry/backoff behavior.
                _notification_manager: StubNotificationManager instance used to track cleanup/resubscribe calls.
                _state_manager: SimpleNamespace with at least an `is_closing` boolean indicating shutdown state.
                _reconnect_scheduler: StubScheduler instance responsible for managing reconnect thread references.
                auto_reconnect (bool): Whether automatic reconnect attempts are enabled.
                is_connection_closing (bool): Flag used to simulate an in-progress connection close.
                address (str): The device address that connect attempts will use.
                client: Placeholder for a BLE client object.
                connect_calls (list): Records addresses passed to `connect` for assertion in tests.
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
            Record a connection attempt for the given address.

            Parameters:
                address (str): Bluetooth address or identifier to connect to.
            """
            self.connect_calls.append(address)

    iface = DummyInterface()
    worker = ReconnectWorker(iface, iface._reconnect_policy)
    worker.attempt_reconnect_loop(True, threading.Event())

    assert iface.connect_calls == ["addr"]
    assert iface._notification_manager.cleaned == 1
    assert len(iface._notification_manager.resubscribed) == 1
    timeout_used = iface._notification_manager.resubscribed[0][1]
    assert timeout_used == ble_mod.BLEConfig.NOTIFICATION_START_TIMEOUT
    assert iface._reconnect_policy.reset_called is True
    assert iface._reconnect_scheduler.cleared is True


def test_reconnect_worker_respects_retry_limits(monkeypatch):
    """ReconnectWorker should obey retry policy decisions when connect keeps failing."""

    sleep_calls = []
    monkeypatch.setattr(ble_mod, "_sleep", lambda delay: sleep_calls.append(delay))

    class LimitedPolicy:
        def __init__(self):
            """
            Initialize the stub policy used in tests and set initial counters.

            Attributes:
                reset_called (bool): Indicates whether `reset()` has been called.
                attempts (int): Count of connection attempts recorded by the stub.
            """
            self.reset_called = False
            self.attempts = 0

        def reset(self):
            """
            Mark the policy as reset and clear its attempt counter.

            Sets `reset_called` to True and resets `attempts` to 0.
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
            Compute the next retry delay and whether another attempt should be made.

            Returns:
                (delay_seconds, continue_flag): A tuple where `delay_seconds` is 0.25 and `continue_flag` is `True` if the internal attempt count after incrementing is less than 2, `False` otherwise.
            """
            self.attempts += 1
            return 0.25, self.attempts < 2

    class StubNotificationManager:
        def __init__(self):
            """
            Initialize the instance and set the cleaned counter.

            Attributes:
                cleaned (int): Number of cleanup operations performed, initialized to 0.
            """
            self.cleaned = 0

        def cleanup_all(self):
            """
            Mark all notifications as cleaned.

            Increments an internal counter that tracks how many times cleanup has been performed.
            """
            self.cleaned += 1

        def resubscribe_all(self, *_args, **_kwargs):  # pragma: no cover - no client
            """
            Raise an error when resubscribe_all is called without an active client.

            This function unconditionally raises an AssertionError to indicate that resubscription logic
            must not be invoked when no client is present.

            Raises:
                AssertionError: Always raised with the message "Should not resubscribe without a client".
            """
            raise AssertionError("Should not resubscribe without a client")

    class StubScheduler:
        def __init__(self):
            """
            Initialize the instance and set its cleared state.

            Attributes:
                cleared (bool): Indicates whether the instance has been cleared; initially False.
            """
            self.cleared = False

        def clear_thread_reference(self):
            """
            Mark that the reconnect thread reference has been cleared.

            Sets an internal flag indicating the scheduler no longer holds a reference to an active reconnect thread, allowing a new reconnect thread to be created.
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
            Simulated failing connect method that records a connection attempt and then raises a BLEError.

            Increments the instance attribute `connect_attempts` by one as a side effect, then raises `self.BLEError("boom")`.

            Raises:
                self.BLEError: Always raised with the message "boom".
            """
            self.connect_attempts += 1
            raise self.BLEError("boom")

    iface = FailingInterface()
    worker = ReconnectWorker(iface, iface._reconnect_policy)
    worker.attempt_reconnect_loop(True, threading.Event())

    assert iface.connect_attempts == 2
    assert iface._notification_manager.cleaned == 2
    assert sleep_calls == [0.25]
    assert iface._reconnect_policy.reset_called is True
    assert iface._reconnect_scheduler.cleared is True
