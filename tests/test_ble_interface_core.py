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
from meshtastic.interfaces.ble.connection import ConnectionOrchestrator

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
        """
        Initialize the strategy wrapper with a delegate discovery coroutine.
        
        Parameters:
            delegate (Callable[[Optional[str], float], Awaitable[List[BLEDevice]]]):
                Async callable that performs connected-device discovery. It receives
                an optional device address and a timeout (seconds) and returns a list
                of discovered BLEDevice instances.
        """
        self._delegate = delegate

    async def discover(self, address: Optional[str], timeout: float) -> List[BLEDevice]:
        """
        Perform device discovery using the configured delegate strategy.
        
        Parameters:
            address (Optional[str]): If provided, restrict returned devices to those matching this address; pass None to return all discovered devices.
            timeout (float): Scan timeout in seconds.
        
        Returns:
            List[BLEDevice]: Discovered BLEDevice objects that match the optional address filter.
        """
        return await self._delegate(address, timeout)


class _BackgroundAsyncRunner:
    """
    Minimal async runner that executes coroutines on a background thread for testing.
    """

    def __init__(self):
        """
        Initialize the runner and prepare storage for scheduled awaitables.
        
        The instance attribute `calls` holds a list of awaitable objects (coroutines or awaitables) that have been scheduled for background execution; each entry represents a task whose result or exception will be captured when run.
        """
        self.calls: List[Awaitable[Any]] = []

    def __enter__(self):
        """
        Enter the context manager for the background async runner.
        
        Returns:
            The same BackgroundAsyncRunner instance to be used as the context manager.
        """
        return self

    def __exit__(self, exc_type, exc, tb):
        """
        Indicates the context manager does not suppress exceptions.
        
        Parameters:
            exc_type (Optional[type]): The exception class raised in the context, or None if no exception.
            exc (Optional[BaseException]): The exception instance raised in the context, or None if no exception.
            tb (Optional[types.TracebackType]): The traceback object for the exception, or None if no exception.
        
        Returns:
            bool: `False` to allow any exception raised in the with-block to propagate.
        """
        return False

    def async_await(self, coro):
        """
        Execute a coroutine on a dedicated background event-loop thread and return its result.
        
        Parameters:
            coro (coroutine): The coroutine to run on the background event loop.
        
        Returns:
            The value returned by the coroutine.
        
        Raises:
            Exception: Re-raises any exception raised by the coroutine.
        """

        self.calls.append(coro)
        result: Dict[str, Any] = {"error": None, "value": None}

        def _worker():
            """
            Run the coroutine and record its outcome in the shared `result` dictionary.
            
            If the coroutine completes successfully, store its return value under `result["value"]`.
            If it raises, store the raised exception under `result["error"]`.
            """
            try:
                result["value"] = asyncio.run(coro)
            except BaseException as exc:  # noqa: BLE001 # pragma: no cover - test helper
                result["error"] = exc

        thread = threading.Thread(target=_worker, name="TestAsyncRunner", daemon=True)
        thread.start()
        thread.join()
        if result["error"]:
            raise result["error"]
        return result["value"]


def test_find_device_returns_single_scan_result(monkeypatch):
    """find_device should return lone scanned device."""
    # BLEDevice and BLEInterface already imported at top as ble_mod.BLEDevice, ble_mod.BLEInterface

    iface = object.__new__(ble_mod.BLEInterface)
    scanned_device = _create_ble_device(address="11:22:33:44:55:66", name="Test Device")

    def mock_discover_devices(self, address):
        """
        Return a fixed single-device list for discovery mocks.
        
        This mock ignores the provided `address` and always returns the prebound `scanned_device` in a list.
        
        Parameters:
            address: Ignored address filter.
        
        Returns:
            list: A list containing the single `scanned_device` object.
        """
        return [scanned_device]

    monkeypatch.setattr(DiscoveryManager, "discover_devices", mock_discover_devices)

    result = ble_mod.BLEInterface.find_device(iface, None)

    assert result is scanned_device


def test_find_device_uses_connected_fallback_when_scan_empty(monkeypatch):
    """find_device should fall back to connected-device lookup when scan is empty."""
    # BLEDevice and BLEInterface already imported at top as ble_mod.BLEDevice, ble_mod.BLEInterface

    iface = object.__new__(ble_mod.BLEInterface)
    fallback_device = _create_ble_device(address="AA:BB:CC:DD:EE:FF", name="Fallback")
    monkeypatch.setattr(DiscoveryManager, "discover_devices", lambda self, address: [])

    def _fake_connected(_self, _address):
        """
        Provide the predefined fallback device as a single-item list.
        
        Returns:
            list: A single-element list containing the module-level `fallback_device`.
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
    monkeypatch.setattr(
        DiscoveryManager, "discover_devices", lambda self, address: devices
    )

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

    async def mock_discover(**_kwargs):
        """
        Return a list of two fake BLEDevice objects for testing discovery behavior.
        
        The first device advertises Meshtastic's SERVICE_UUID and the second advertises a different UUID.
        
        Returns:
            list[BLEDevice]: Two BLEDevice instances — the first with `SERVICE_UUID` in its advertised UUIDs, the second with a non-matching service UUID.
        """
        from bleak.backends.device import BLEDevice

        filtered_device = BLEDevice(
            "AA:BB:CC:DD:EE:FF",
            "Filtered",
            {"props": {"UUIDs": [SERVICE_UUID]}},
        )
        other_device = BLEDevice(
            "11:22:33:44:55:66",
            "Other",
            {"props": {"UUIDs": ["some-other-service"]}},
        )
        return [filtered_device, other_device]

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
        """
        Provide a mocked discovery result containing a single filtered device.
        
        Parameters:
            _kwargs: Arbitrary keyword arguments accepted and ignored; present to match discovery call signature.
        
        Returns:
            A list containing the single filtered BLE device.
        """
        return [filtered_device]

    monkeypatch.setattr("bleak.BleakScanner.discover", mock_discover)

    runners: List[_BackgroundAsyncRunner] = []

    def _factory():
        """
        Create a new _BackgroundAsyncRunner and register it in the surrounding runners list.
        
        The new runner is appended to the outer-scope `runners` list as a side effect.
        
        Returns:
            _BackgroundAsyncRunner: The newly created background async runner.
        """
        runner = _BackgroundAsyncRunner()
        runners.append(runner)
        return runner

    manager = DiscoveryManager(ble_client_factory=_factory)  # type: ignore[arg-type]  # type: ignore[arg-type]

    async def fake_connected(address: Optional[str], timeout: float) -> List[BLEDevice]:
        """
        Provide a fake connected-device fallback that yields a single BLEDevice for the address "AA:BB".
        
        Parameters:
            address (Optional[str]): Expected to be "AA:BB".
            timeout (float): Expected to match ble_mod.BLEConfig.BLE_SCAN_TIMEOUT.
        
        Returns:
            List[BLEDevice]: A list containing the predefined `filtered_device`.
        """
        assert address == "AA:BB"
        assert timeout == ble_mod.BLEConfig.BLE_SCAN_TIMEOUT
        return [cast("BLEDevice", filtered_device)]

    manager.connected_strategy = _StrategyOverride(fake_connected)

    async def _invoke():
        """
        Call the discovery manager to find devices matching the address "AA:BB".
        
        Returns:
            A list of discovered devices that match the address "AA:BB".
        """
        return manager.discover_devices(address="AA:BB")

    devices = asyncio.run(_invoke())

    assert devices == [filtered_device]
    assert len(runners) == 1
    assert len(runners[0].calls) == 1


def test_discovery_manager_handles_running_loop_fallback(monkeypatch):
    """Connected-device fallback should also execute via the async runner when an event loop is active."""

    async def mock_discover(**_kwargs):
        """
        Provide an async discovery stub that always returns an empty list of devices.
        
        Parameters:
            **_kwargs: Ignored keyword arguments kept for compatibility with discovery call signatures.
        
        Returns:
            An empty list representing no discovered devices.
        """
        return []

    monkeypatch.setattr("bleak.BleakScanner.discover", mock_discover)

    fallback_device = _create_ble_device("AA:BB", "Fallback")
    runners: List[_BackgroundAsyncRunner] = []

    def _factory():
        """
        Create a new _BackgroundAsyncRunner and register it in the surrounding runners list.
        
        The new runner is appended to the outer-scope `runners` list as a side effect.
        
        Returns:
            _BackgroundAsyncRunner: The newly created background async runner.
        """
        runner = _BackgroundAsyncRunner()
        runners.append(runner)
        return runner

    manager = DiscoveryManager(ble_client_factory=_factory)  # type: ignore[arg-type]

    async def fake_connected(address: Optional[str], timeout: float) -> List[BLEDevice]:
        """
        Test helper that returns a predefined connected BLE device when given the expected address and timeout.
        
        Parameters:
            address (Optional[str]): Expected device address; must equal "AA:BB".
            timeout (float): Expected scan timeout; must equal ble_mod.BLEConfig.BLE_SCAN_TIMEOUT.
        
        Returns:
            List[BLEDevice]: A single-element list containing the predefined fallback device.
        
        Raises:
            AssertionError: If `address` is not "AA:BB" or `timeout` does not match the expected scan timeout.
        """
        assert address == "AA:BB"
        assert timeout == ble_mod.BLEConfig.BLE_SCAN_TIMEOUT
        return [fallback_device]

    manager.connected_strategy = _StrategyOverride(fake_connected)

    async def _invoke():
        """
        Call the discovery manager to find devices matching the address "AA:BB".
        
        Returns:
            A list of discovered devices that match the address "AA:BB".
        """
        return manager.discover_devices(address="AA:BB")

    devices = asyncio.run(_invoke())

    assert devices == [fallback_device]
    assert len(runners) == 1
    assert len(runners[0].calls) == 2


def test_discovery_manager_uses_connected_strategy_when_scan_empty(monkeypatch):
    """When no devices are discovered via BLE scan, DiscoveryManager should fall back to connected strategy."""

    fallback_device = _create_ble_device("AA:BB", "Fallback")

    manager = DiscoveryManager()

    async def fake_connected(address: Optional[str], timeout: float) -> List[BLEDevice]:
        """
        Provide a predetermined connected-device fallback when invoked with the expected address and timeout.
        
        Parameters:
            address (Optional[str]): Expected device address; must equal "AA:BB".
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
            Enter the context and return this context manager instance.
            
            Returns:
                self: The context manager instance.
            """
            return self

        def __exit__(self, exc_type, exc, tb):
            """
            Indicates exit of the context manager and that exceptions raised inside the with-block are not suppressed.
            
            Returns:
                bool: False to signal that any exception raised in the with-block should be propagated (not suppressed).
            """
            return False

        def discover(self, **_kwargs):
            """
            Return an empty discovery mapping of BLE devices.
            
            Accepts and ignores any keyword arguments.
            
            Returns:
                dict: Empty mapping of discovered device addresses to metadata.
            """
            return {}

        @staticmethod
        def async_await(coro):  # pragma: no cover - fallback should not be hit
            """
            Run a coroutine to completion and return its result.
            
            Parameters:
                coro (coroutine): The coroutine to run.
            
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
        Test helper coroutine that records that the connected-device fallback was invoked and returns no devices.
        
        Parameters:
            address (Optional[str]): Address to search for; present so callers can pass an address filter but not used to find devices.
            timeout (float): Maximum time to wait in seconds; accepted for API compatibility but ignored by this stub.
        
        Returns:
            List[BLEDevice]: An empty list indicating no connected devices were found.
        
        Side effects:
            Sets the enclosing `fallback_called` variable to True to signal that the fallback path was exercised in tests.
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
        validator.check_existing_client(
            cast("BLEClient", client), "something-else", None, None
        )
        is False
    )


class _StubStateManager:
    """Minimal state manager stub for ConnectionOrchestrator tests."""

    def __init__(self):
        self.state = ConnectionState.DISCONNECTED
        self.transitions = []

    def transition_to(self, new_state, client=None):
        self.transitions.append((self.state, new_state, client))
        self.state = new_state
        return True


class _StubThreadCoordinator:
    """Track set_event calls for ConnectionOrchestrator tests."""

    def __init__(self):
        self.events: List[str] = []

    def set_event(self, name: str):
        self.events.append(name)


class _StubClientManager:
    """ClientManager stand-in that records lifecycle operations."""

    def __init__(self, *, fail_on: Optional[Dict[str, int]] = None):
        self.fail_on = dict(fail_on or {})
        self.created: List[str] = []
        self.connected = []
        self.closed = []

    def create_client(self, address, _callback):
        self.created.append(address)
        return SimpleNamespace(
            bleak_client=SimpleNamespace(address=address),
        )

    def connect_client(self, client):
        address = getattr(client.bleak_client, "address", None)
        remaining = self.fail_on.get(address, 0)
        if remaining > 0:
            self.fail_on[address] = remaining - 1
            raise RuntimeError(f"connect failure for {address}")
        self.connected.append(client)

    def safe_close_client(self, client, event=None):
        self.closed.append(client)
        if event:
            event.set()


class _StubValidator:
    """Track validate_connection_request invocations."""

    def __init__(self):
        self.calls = 0

    def validate_connection_request(self):
        self.calls += 1


def test_connection_orchestrator_reuses_known_address(monkeypatch):
    """ConnectionOrchestrator should skip discovery when cached address matches the target."""

    known_address = "DD:DD:13:27:74:29"
    def _fail_find_device(*_args, **_kwargs):
        raise AssertionError("find_device should not be called for cached address")

    interface = SimpleNamespace(
        _known_device_address=known_address,
        find_device=_fail_find_device,
    )
    validator = _StubValidator()
    state_manager = _StubStateManager()
    thread_coordinator = _StubThreadCoordinator()
    client_manager = _StubClientManager()

    orchestrator = ConnectionOrchestrator(
        interface,
        validator,
        client_manager,
        DiscoveryManager(),
        state_manager,
        threading.RLock(),
        thread_coordinator,
    )

    notifications: List[SimpleNamespace] = []

    def _register(client):
        notifications.append(client)

    connected_calls = []

    def _connected():
        connected_calls.append(True)

    client = orchestrator.establish_connection(
        known_address,
        known_address,
        _register,
        _connected,
        lambda *_args, **_kwargs: None,
    )

    assert client in notifications
    assert client_manager.created == [known_address]
    assert client_manager.connected == [client]
    assert client_manager.closed == []
    assert thread_coordinator.events == ["reconnected_event"]
    assert validator.calls == 1
    # State manager should transition through CONNECTING -> CONNECTED
    assert state_manager.transitions[0][1] == ConnectionState.CONNECTING
    assert state_manager.transitions[-1][1] == ConnectionState.CONNECTED


def test_connection_orchestrator_falls_back_after_direct_failure(monkeypatch):
    """Direct reconnect failures should trigger discovery fallback."""

    known_address = "DD:DD:13:27:74:29"
    fallback_device = SimpleNamespace(address=known_address, name="Relay")
    find_calls: List[Optional[str]] = []

    def _find_device(address):
        find_calls.append(address)
        return fallback_device

    interface = SimpleNamespace(
        _known_device_address=known_address,
        find_device=_find_device,
    )
    validator = _StubValidator()
    state_manager = _StubStateManager()
    thread_coordinator = _StubThreadCoordinator()
    client_manager = _StubClientManager(fail_on={known_address: 1})

    orchestrator = ConnectionOrchestrator(
        interface,
        validator,
        client_manager,
        DiscoveryManager(),
        state_manager,
        threading.RLock(),
        thread_coordinator,
    )

    client = orchestrator.establish_connection(
        known_address,
        known_address,
        lambda *_args, **_kwargs: None,
        lambda: None,
        lambda *_args, **_kwargs: None,
    )

    assert len(client_manager.created) == 2  # direct + discovery attempts
    assert client_manager.closed  # direct attempt cleaned up
    assert find_calls == [known_address]
    assert client_manager.connected == [client]
    assert thread_coordinator.events == ["reconnected_event"]
    assert validator.calls == 1


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
        Record a pubsub message invocation for test capture by appending (topic, kwargs) to the module-level `calls` list.
        
        Parameters:
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
                Create a test client that raises the provided exception from its faulting methods.
                
                Parameters:
                    exception_type (Exception | type): An exception instance or exception class; the client will raise the instance as-is if given an instance, or will instantiate and raise it when given a class.
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
            Signal that close was invoked and delegate to the provided close function.
            
            Sets the `close_called` event to indicate a close was requested, then calls and returns the result of `original_close()`.
            
            Returns:
                The result returned by `original_close()`.
            """
            close_called.set()
            return original_close()

        monkeypatch.setattr(iface, "close", mock_close)

        # Start the receive thread
        iface._want_receive = True

        # Phase 3: Use unified state lock instead of _client_lock
        with iface._state_lock:
            iface.client = cast("BLEClient", client)

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
            Create a mock BLE client that tracks notification registrations and reports available characteristics.
            
            Attributes:
                start_notify_calls (list): Recorded arguments for each start_notify invocation.
                has_characteristic_map (dict): Mapping of characteristic UUID -> bool indicating whether the
                    client reports that characteristic is present. Initially contains
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
            Determine whether the client has a characteristic with the given UUID.
            
            Parameters:
                uuid: Characteristic UUID to check (string or uuid.UUID).
            
            Returns:
                `True` if the UUID is present in the client's characteristic map, `False` otherwise.
            """
            return self.has_characteristic_map.get(uuid, False)

        def start_notify(self, *_args, **_kwargs):
            """
            Record a requested notification registration by saving the characteristic UUID and its handler.
            
            Parameters:
                _args (tuple): If two or more positional arguments are provided, the first is the characteristic UUID (usually a string)
                    and the second is the notification handler (callable). When present, the pair is appended to `self.start_notify_calls`.
                _kwargs (dict): Additional keyword arguments are accepted and ignored.
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
            Create a new instance and initialize an empty list for tracking created items.
            
            The instance attribute `created` is set to an empty list and is intended to record items produced by this object.
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
            Mark a thread-like object as started by setting its `started` attribute.
            
            Parameters:
                thread: Thread-like object whose `started` attribute will be set.
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
    # Mock current_thread in the reconnect module where it's actually used
    import meshtastic.interfaces.ble.reconnect as reconnect_mod

    monkeypatch.setattr(reconnect_mod, "current_thread", lambda: stub_thread)
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
            Reset the retry policy to its initial state.
            
            Sets the attempt counter to zero and records that a reset occurred by setting `reset_called` to True.
            """
            self.reset_called = True
            self._attempt_count = 0

        def get_attempt_count(self):
            """
            Return the number of reconnect attempts recorded by the policy.
            
            Returns:
                int: The number of reconnect attempts.
            """
            return self._attempt_count

        def next_attempt(self):
            """
            Advance the retry state and provide the next retry delay and whether further attempts should be made.
            
            Increments the internal attempt counter as a side effect.
            
            Returns:
                tuple: (`delay_seconds`, `continue_retry`)
                `delay_seconds` (float): Seconds to wait before the next attempt.
                `continue_retry` (bool): `True` to perform another attempt, `False` to stop.
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
            
            Increments the internal cleanup counter (self.cleaned) to record that a cleanup run occurred.
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
            Create a new instance and mark it as not cleared.
            
            Initializes the instance attribute `cleared` to False; this flag indicates whether the instance has been cleared.
            """
            self.cleared = False

        def clear_thread_reference(self):
            """
            Record that the scheduler no longer holds a reconnect thread reference.
            
            Sets an internal flag allowing the scheduler to start a new reconnect thread.
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
            self._reconnect_policy = cast(ble_mod.ReconnectPolicy, StubPolicy())
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
            Record a connection attempt for the specified Bluetooth address.
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
    assert cast("StubPolicy", iface._reconnect_policy).reset_called is True
    assert iface._reconnect_scheduler.cleared is True


def test_reconnect_worker_respects_retry_limits(monkeypatch):
    """ReconnectWorker should obey retry policy decisions when connect keeps failing."""

    sleep_calls = []
    # Mock _sleep in the reconnect module where it's actually used
    import meshtastic.interfaces.ble.reconnect as reconnect_mod

    monkeypatch.setattr(
        reconnect_mod, "_sleep", lambda delay: sleep_calls.append(delay)
    )

    class LimitedPolicy:
        def __init__(self):
            """
            Initialize the stub retry policy for tests and set initial counters.
            
            Attributes:
                reset_called (bool): True after `reset()` is invoked.
                attempts (int): Number of connection attempts recorded by the stub.
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
            Get the number of reconnect attempts recorded by the policy.
            
            Returns:
                int: The number of attempts made so far.
            """
            return self.attempts

        def next_attempt(self):
            """
            Calculate the next retry delay and indicate whether another attempt should be performed.
            
            Returns:
                (delay_seconds, continue_flag): Tuple where `delay_seconds` is 0.25, and `continue_flag` is `True` if the internal attempt count after incrementing is less than 2, `False` otherwise.
            """
            self.attempts += 1
            return 0.25, self.attempts < 2

    class StubNotificationManager:
        def __init__(self):
            """
            Create a new instance and initialize the cleanup counter.
            
            Initializes the instance attribute `cleaned` to 0, the number of cleanup operations performed.
            """
            self.cleaned = 0

        def cleanup_all(self):
            """
            Mark all notifications as cleaned.
            
            Increments the internal cleanup counter (self.cleaned) to record that a cleanup run occurred.
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
            Create a new instance and mark it as not cleared.
            
            Initializes the instance attribute `cleared` to False; this flag indicates whether the instance has been cleared.
            """
            self.cleared = False

        def clear_thread_reference(self):
            """
            Record that the scheduler no longer holds a reconnect thread reference.
            
            Sets an internal flag allowing the scheduler to start a new reconnect thread.
            """
            self.cleared = True

    class FailingInterface:
        BLEError = RuntimeError

        def __init__(self):
            """
            Create a minimal stub interface used by reconnect tests.
            
            Attributes:
                _reconnect_policy: ReconnectPolicy controlling retry behaviour (LimitedPolicy instance).
                _notification_manager: Notification manager used to cleanup and resubscribe (StubNotificationManager instance).
                _state_manager: SimpleNamespace with runtime state flags; provides `is_closing`.
                _reconnect_scheduler: Scheduler that tracks reconnect thread references (StubScheduler instance).
                auto_reconnect (bool): Whether automatic reconnect attempts are enabled.
                is_connection_closing (bool): True while a connection close is in progress.
                address (str): Remote device address used for connection attempts.
                client: Placeholder for the BLE client instance (initially None).
                connect_attempts (int): Counter of connect() invocation attempts.
            """
            self._reconnect_policy = cast(ble_mod.ReconnectPolicy, LimitedPolicy())
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
            Record a connection attempt by incrementing `connect_attempts`, then raise a BLEError.
            
            Increments the instance attribute `connect_attempts` by one as a side effect.
            
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
    assert cast("LimitedPolicy", iface._reconnect_policy).reset_called is True
    assert iface._reconnect_scheduler.cleared is True
