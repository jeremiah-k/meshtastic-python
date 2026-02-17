"""Tests for the BLE interface module - Core functionality."""

import asyncio
import logging
import threading
import time
from queue import Queue
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
    Tuple,
    cast,
)

import pytest
from bleak.backends.device import BLEDevice
from bleak.exc import BleakError

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
from meshtastic.interfaces.ble.discovery import (
    ConnectedStrategy,
    DiscoveryManager,
    _ble_device_constructor_kwargs_support,
    _filter_devices_for_target_identifier,
    _parse_scan_response,
)
from meshtastic.interfaces.ble.reconnection import ReconnectScheduler, ReconnectWorker
from meshtastic.interfaces.ble.state import BLEStateManager, ConnectionState

# Import common fixtures
from tests.test_ble_interface_fixtures import DummyClient, _build_interface

if TYPE_CHECKING:

    class _PubProtocol(Protocol):
        def sendMessage(self, topic: str, **kwargs: Any) -> None:
            """
            Publish a message to the specified pubsub topic.
            
            The provided keyword arguments are assembled into the message payload and published under the given topic name.
            
            Parameters:
                topic (str): Topic name to publish the message under.
                **kwargs: Arbitrary key/value pairs included as the message payload.
            """
            ...

    pub: _PubProtocol
else:  # pragma: no cover - import only at runtime
    from pubsub import pub


def _create_ble_device(address: str, name: str) -> BLEDevice:
    """
    Construct a BLEDevice using a constructor signature compatible with the installed bleak version.

    If the installed bleak's BLEDevice constructor accepts `details` and/or `rssi`, this function supplies an empty dict for `details` and `0` for `rssi` so the returned instance is compatible across bleak versions.

    Returns:
        BLEDevice: A BLEDevice instance constructed with the arguments supported by the installed bleak version.

    """
    params: Dict[str, Any] = {"address": address, "name": name}
    supports_details, supports_rssi = _ble_device_constructor_kwargs_support()
    if supports_details:
        params["details"] = {}
    if supports_rssi:
        params["rssi"] = 0
    return BLEDevice(**params)


class _FakeDiscoveryClient:
    """Context-manager BLE client stub used by discovery tests."""

    def __init__(
        self,
        discover_result: Dict[str, Any],
        *,
        async_await_impl: Optional[Callable[[Any, Optional[float]], Any]] = None,
    ) -> None:
        """
        Initialize the fake discovery client with a preset discovery result.
        
        Parameters:
            discover_result (Dict[str, Any]): The value to return from discovery() calls; represents the simulated scan results.
            async_await_impl (Optional[Callable[[Any, Optional[float]], Any]]): Optional function used to run/await coroutines passed to async_await(coro, timeout). If omitted, the default awaiting behavior is used.
        """
        self._discover_result = discover_result
        self._async_await_impl = async_await_impl

    def __enter__(self) -> "_FakeDiscoveryClient":
        """
        Enter the context for the fake discovery client and return the client instance.
        
        Returns:
            _FakeDiscoveryClient: The fake discovery client instance to be used inside the context manager.
        """
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> bool:
        """
        Exit the context and indicate that any exception should propagate.
        
        Parameters:
            exc_type (Any): Exception type if an exception was raised inside the context, otherwise None.
            exc (Any): Exception instance if raised, otherwise None.
            tb (Any): Traceback object if an exception was raised, otherwise None.
        
        Returns:
            bool: `False` to indicate that exceptions should not be suppressed and must be re-raised.
        """
        _ = (exc_type, exc, tb)
        return False

    def discover(self, **_kwargs: Any) -> Dict[str, Any]:
        """
        Provides the preconfigured discovery result for use in tests.
        
        Returns:
            dict: The stored discovery result dictionary that this fake discovery client will return.
        """
        return self._discover_result

    def async_await(self, coro: Any, timeout: Optional[float] = None) -> Any:
        """
        Run the given coroutine to completion using the configured await implementation or the default runner.
        
        Parameters:
            coro: The coroutine or awaitable to execute.
            timeout (float | None): Optional timeout in seconds for the await implementation to honor; may be ignored by the configured implementation.
        
        Returns:
            The value returned by the awaited coroutine.
        """
        if self._async_await_impl is not None:
            return self._async_await_impl(coro, timeout)
        return asyncio.run(coro)


def _attach_close_monitor(monkeypatch: Any, iface: BLEInterface) -> threading.Event:
    """
    Wrap iface.close so calling close sets a threading.Event and then invokes the original close.
    
    Parameters:
        monkeypatch: pytest-style monkeypatch fixture used to replace attributes on the interface.
        iface: BLEInterface whose close method will be wrapped.
    
    Returns:
        threading.Event: event that will be set when the patched close is invoked.
    """
    original_close = iface.close
    close_called = threading.Event()

    # Bind outer values into defaults so monkeypatched method keeps stable
    # references even if local names are reassigned later in the test.
    def _mock_close(
        original_close: Callable[[], Any] = original_close,
        close_called: threading.Event = close_called,
    ) -> Any:
        """
        Mark the provided close event and invoke the original close callable.
        
        Parameters:
            original_close (Callable[[], Any]): The original close function to invoke.
            close_called (threading.Event): Event to set when close is invoked.
        
        Returns:
            Any: The value returned by `original_close`.
        """
        close_called.set()
        return original_close()

    monkeypatch.setattr(iface, "close", _mock_close)
    return close_called


def _assert_no_fallback(message: str) -> Callable[[Any, Optional[float]], Any]:
    """
    Create a callable that raises an AssertionError with the given message when invoked.
    
    Returns:
        callable: A function taking (coro, timeout=None) that always raises AssertionError(message).
    """

    def _raise(_coro: Any, _timeout: Optional[float] = None) -> Any:
        """
        Raise an AssertionError to indicate this async-await path must not be used.
        
        The parameters are ignored; calling this function will always raise an AssertionError
        carrying the configured failure message.
        """
        raise AssertionError(message)

    return _raise


class _StrategyOverride(ConnectedStrategy):
    """
    Adapt an async callable into a ConnectedStrategy-compatible object for testing.
    """

    def __init__(
        self,
        delegate: Callable[[Optional[str], float], Awaitable[List[BLEDevice]]],
    ) -> None:
        """
        Wraps an asynchronous discovery coroutine for use as a ConnectedStrategy.

        Parameters
        ----------
            delegate (Callable[[Optional[str], float], Awaitable[List[BLEDevice]]]):
                Async callable invoked as delegate(address, timeout) that returns a list of
                discovered BLEDevice objects for the optional address and timeout in seconds.

        """
        self._delegate = delegate

    async def discover(self, address: Optional[str], timeout: float) -> List[BLEDevice]:
        """
        Delegate BLE device discovery for the given address and timeout.

        Parameters
        ----------
            address (Optional[str]): Bluetooth address to filter results, or `None` to discover any device.
            timeout (float): Maximum time in seconds to wait for discovery.

        Returns
        -------
            List[BLEDevice]: Discovered BLEDevice instances that match the request.

        """
        return await self._delegate(address, timeout)


class _ReconnectTestNotificationManager:
    """Shared notification-manager test double for reconnect worker tests."""

    def __init__(self, *, fail_on_resubscribe: bool = False) -> None:
        """
        Initialize the test notification manager used by reconnect tests.
        
        Tracks how many times cleanup is requested and records resubscription attempts.
        When `fail_on_resubscribe` is True, the manager is configured to simulate a failing
        resubscribe operation.
        
        Parameters:
            fail_on_resubscribe (bool): If True, resubscription attempts will be treated as failures.
        """
        self.cleaned = 0
        self.resubscribed: List[Tuple[Any, float]] = []
        self._fail_on_resubscribe = fail_on_resubscribe

    def _cleanup_all(self) -> None:
        """Record notification cleanup calls."""
        self.cleaned += 1

    def _resubscribe_all(self, client: Any, timeout: float) -> None:
        """
        Record a resubscription request for testing, or raise if resubscriptions are configured to fail.
        
        Parameters:
            client (Any): The client object for which resubscription was requested.
            timeout (float): The timeout (in seconds) to use for the resubscription attempt.
        
        Raises:
            AssertionError: If the test instance is configured to fail on resubscribe.
        """
        if self._fail_on_resubscribe:
            raise AssertionError("Should not resubscribe without a client")
        self.resubscribed.append((client, timeout))


class _ReconnectTestScheduler:
    """Shared reconnect-scheduler test double for reconnect worker tests."""

    def __init__(self) -> None:
        """
        Initialize the test scheduler and mark it as not cleared.
        
        Sets the `cleared` attribute to `False`. The `cleared` flag indicates whether clear_thread_reference() has been invoked.
        """
        self.cleared = False

    def clear_thread_reference(self) -> None:
        """Record that reconnect thread reference cleanup was requested."""
        self.cleared = True


def test_find_device_returns_single_scan_result() -> None:
    """find_device should return the lone scanned device."""
    # BLEDevice and BLEInterface already imported at top as ble_mod.BLEDevice, ble_mod.BLEInterface

    # Intentional constructor bypass: inject a controlled _discovery_manager
    # without running BLEInterface.__init__ side effects.
    iface = object.__new__(ble_mod.BLEInterface)
    scanned_device = _create_ble_device(address="11:22:33:44:55:66", name="Test Device")
    iface._discovery_manager = SimpleNamespace(  # type: ignore[assignment]
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


def test_ble_package_and_legacy_facade_exports_match():
    """Package BLE exports should match the legacy meshtastic.ble_interface facade."""
    import meshtastic.ble_interface as legacy_ble_mod

    assert set(ble_mod.__all__) == set(legacy_ble_mod.__all__)


def test_ble_device_constructor_support_probe_shape():
    """BLEDevice constructor support probe should return a bool/bool tuple."""
    supports = _ble_device_constructor_kwargs_support()

    assert isinstance(supports, tuple)
    assert len(supports) == 2
    assert all(isinstance(flag, bool) for flag in supports)


def test_state_manager_closing_only_for_disconnect():
    """is_closing should be true only while disconnecting."""
    state_manager = BLEStateManager()
    assert state_manager.is_closing is False
    # DISCONNECTED -> DISCONNECTING is not allowed (semantically incorrect:
    # you can't "begin disconnecting" from an already-disconnected state).
    # The proper path is through a connected/active state first.
    assert state_manager.transition_to(ConnectionState.CONNECTING) is True
    assert state_manager.is_closing is False
    assert state_manager.transition_to(ConnectionState.DISCONNECTING) is True
    assert state_manager.is_closing is True
    assert state_manager.transition_to(ConnectionState.DISCONNECTED) is True
    assert state_manager.is_closing is False
    # ERROR state should also not be "closing"
    assert state_manager.transition_to(ConnectionState.ERROR) is True
    assert state_manager.is_closing is False


def test_state_manager_allows_error_to_disconnecting_shutdown():
    """State manager should support ERROR -> DISCONNECTING for deterministic close paths."""
    state_manager = BLEStateManager()

    assert state_manager.transition_to(ConnectionState.CONNECTING) is True
    assert state_manager.transition_to(ConnectionState.ERROR) is True
    assert state_manager.transition_to(ConnectionState.DISCONNECTING) is True
    assert state_manager.is_closing is True
    assert state_manager.transition_to(ConnectionState.DISCONNECTED) is True
    assert state_manager.is_closing is False


def test_ble_interface_defaults_auto_reconnect_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """BLEInterface should default auto_reconnect to False."""
    iface = _build_interface(monkeypatch, DummyClient(), start_receive_thread=False)
    assert iface.auto_reconnect is False
    iface.close()


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
        iface.client = active_client  # type: ignore[assignment]
        iface._disconnect_notified = False
        iface._state_manager.reset_to_disconnected()
        assert iface._state_manager.transition_to(ConnectionState.CONNECTING) is True
        assert iface._state_manager.transition_to(ConnectionState.CONNECTED) is True

    # Stale callback by BLEClient instance should be ignored.
    assert iface._handle_disconnect("stale-client", client=stale_client) is True  # type: ignore[arg-type]
    # Stale callback by bleak client identity should also be ignored.
    assert (
        iface._handle_disconnect("stale-bleak", bleak_client=stale_client.bleak_client)  # type: ignore[arg-type]
        is True
    )

    assert iface.client is active_client
    assert iface._disconnect_notified is False
    assert reconnect_calls == []
    assert disconnected_calls == []

    iface.close()


def test_concurrent_connect_and_disconnect_do_not_deadlock(monkeypatch, clear_registry):
    """
    Concurrent connect/disconnect should complete without deadlocking under address-lock contention.

    This test forces connect() to hold the per-address lock while _handle_disconnect()
    runs, then releases connect to ensure both operations complete.
    """
    _ = clear_registry
    import meshtastic.interfaces.ble.interface as ble_iface_mod

    target_address = "AA:BB:CC:DD:EE:01"
    initial_client = DummyClient()
    initial_client.address = target_address
    initial_client.bleak_client = SimpleNamespace(address=target_address)

    connected_client = DummyClient()
    connected_client.address = target_address
    connected_client.bleak_client = SimpleNamespace(address=target_address)

    real_connect = BLEInterface.connect

    def _init_connect_stub(
        iface: BLEInterface, _address: Optional[str] = None
    ) -> DummyClient:
        """
        Prepare the given BLEInterface for tests by installing and returning a pre-existing DummyClient and marking the interface as connected.
        
        Parameters:
        	iface (BLEInterface): The interface whose client and connection state will be configured.
        	_address (Optional[str]): Ignored; present for compatibility with call sites that pass an address.
        
        Returns:
        	DummyClient: The dummy client instance that was attached to the interface.
        """
        _ = _address
        with iface._state_lock:
            iface.client = initial_client  # type: ignore[assignment]
            iface._disconnect_notified = False
            iface._state_manager.reset_to_disconnected()
            iface._state_manager.transition_to(ConnectionState.CONNECTED)
        return initial_client

    monkeypatch.setattr(BLEInterface, "connect", _init_connect_stub, raising=True)
    monkeypatch.setattr(
        BLEInterface,
        "_start_receive_thread",
        lambda _self, *, name: None,
        raising=True,
    )
    monkeypatch.setattr(BLEInterface, "_startConfig", lambda _self: None, raising=True)

    iface = BLEInterface(address=target_address, noProto=True, auto_reconnect=False)
    monkeypatch.setattr(BLEInterface, "connect", real_connect, raising=True)

    with iface._state_lock:
        iface.client = None
        iface._disconnect_notified = False
        iface._connection_alias_key = None
        iface._state_manager.reset_to_disconnected()

    connect_waiting = threading.Event()
    allow_connect = threading.Event()
    establish_called = threading.Event()
    thread_errors: "Queue[tuple[str, Exception]]" = Queue()

    def _gate_check_stub(addr_key: Optional[str], owner: Optional[Any] = None) -> bool:
        """
        Block test caller until the test releases a connection gate and record that the gate was reached.
        
        Parameters:
            addr_key (Optional[str]): Address key that must be provided (asserted non-None); used to identify the gated connection.
            owner (Optional[Any]): Ignored; present to match the gate-check signature.
        
        Returns:
            bool: `False` always.
        
        Raises:
            AssertionError: If `addr_key` is None or if waiting for the test to release the gate times out (12 seconds).
        """
        _ = owner
        assert addr_key is not None
        connect_waiting.set()
        if not allow_connect.wait(timeout=12.0):
            raise AssertionError("Timed out waiting to release connect gate check")
        return False

    def _establish_connection_stub(*_args: Any, **_kwargs: Any) -> DummyClient:
        """
        Simulate a successful connection for tests by transitioning the interface state to CONNECTING then CONNECTED.
        
        Also sets the `establish_called` event to signal completion.
        
        Returns:
            connected_client (DummyClient): A DummyClient instance representing the established connection.
        """
        with iface._state_lock:
            assert iface._state_manager.transition_to(ConnectionState.CONNECTING)
            assert iface._state_manager.transition_to(ConnectionState.CONNECTED)
        establish_called.set()
        return connected_client

    monkeypatch.setattr(
        ble_iface_mod,
        "_is_currently_connected_elsewhere",
        _gate_check_stub,
        raising=True,
    )
    monkeypatch.setattr(
        iface._connection_orchestrator,
        "establish_connection",
        _establish_connection_stub,
        raising=True,
    )
    monkeypatch.setattr(iface, "_register_notifications", lambda _client: None)
    monkeypatch.setattr(iface, "_connected", lambda: None)
    monkeypatch.setattr(iface, "_disconnected", lambda: None)

    def _connect_worker() -> None:
        """
        Invoke the interface's connect routine for the configured target address and capture any exception raised.
        
        If an exception occurs, record a tuple ("connect", exc) into the `thread_errors` queue for later inspection by tests.
        """
        try:
            iface.connect(target_address)
        except Exception as exc:  # noqa: BLE001 - test captures thread errors
            thread_errors.put(("connect", exc))

    def _disconnect_worker() -> None:
        """
        Invoke the interface's disconnect handler in a thread and capture any exception for test inspection.
        
        Calls iface._handle_disconnect("concurrency-test"). If an exception is raised, places a ("disconnect", exception) tuple into the thread_errors queue so test code can observe thread failures.
        """
        try:
            iface._handle_disconnect("concurrency-test")
        except Exception as exc:  # noqa: BLE001 - test captures thread errors
            thread_errors.put(("disconnect", exc))

    connect_thread = threading.Thread(target=_connect_worker, daemon=True)
    disconnect_thread = threading.Thread(target=_disconnect_worker, daemon=True)
    try:
        connect_thread.start()
        assert connect_waiting.wait(timeout=12.0), "connect() did not reach gate check"

        disconnect_thread.start()
        allow_connect.set()

        connect_thread.join(timeout=12.0)
        disconnect_thread.join(timeout=12.0)

        assert (
            establish_called.is_set()
        ), "connect() did not run connection establishment"
        assert not connect_thread.is_alive(), "connect() thread appears deadlocked"
        assert not disconnect_thread.is_alive(), "disconnect thread appears deadlocked"

        if not thread_errors.empty():
            where, exc = thread_errors.get_nowait()
            pytest.fail(f"{where} thread raised {type(exc).__name__}: {exc}")
    finally:
        allow_connect.set()
        if connect_thread.is_alive():
            connect_thread.join(timeout=1.0)
        if disconnect_thread.is_alive():
            disconnect_thread.join(timeout=1.0)
        iface.close()


def test_transient_read_retry_uses_zero_based_delay(monkeypatch):
    """Transient read retries should pass a zero-based attempt index to policy delay."""
    iface = _build_interface(monkeypatch, DummyClient())
    delay_attempts: List[int] = []

    class StubTransientPolicy:
        def should_retry(self, attempt: int) -> bool:
            """
            Decides whether to perform another retry based on the zero-based attempt index.
            
            Parameters:
                attempt (int): Zero-based retry attempt index (0 for the first attempt).
            
            Returns:
                bool: `True` if `attempt` is less than 1, `False` otherwise.
            """
            return attempt < 1

        def get_delay(self, attempt: int) -> float:
            """
            Record the retry attempt index and return a zero-second retry delay.
            
            Appends the zero-based `attempt` index to the surrounding test's `delay_attempts` list.
            
            Parameters:
                attempt (int): Zero-based retry attempt index to record.
            
            Returns:
                float: Delay in seconds (always 0.0).
            """
            delay_attempts.append(attempt)
            return 0.0

    iface._transient_read_policy = StubTransientPolicy()  # type: ignore[assignment]
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
    disconnect_calls: List[Tuple[str, Optional[Any], Optional[Any]]] = []

    def raising_wait_for_event(_name: str, timeout: Optional[float] = None) -> bool:
        """
        Simulate a fatal receive-loop failure by always raising a RuntimeError.

        Raises:
            RuntimeError: Always raised to emulate an unexpected fatal error in the receive loop.

        """
        _ = timeout
        raise RuntimeError("fatal receive loop failure")

    def fake_handle_disconnect(
        source: str,
        client: Optional[Any] = None,
        bleak_client: Optional[Any] = None,
    ) -> bool:
        """
        Record the disconnect invocation and stop the receive loop.
        
        Returns:
            `False` indicating the handler did not handle the disconnect.
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
        """
        Fail if thread creation is attempted after the interface has been closed.
        
        Raises:
            AssertionError: Always raised with the message "create_thread should not be called after close()".
        """
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

    # Intentional constructor bypass for isolated find_device() behavior.
    iface = object.__new__(ble_mod.BLEInterface)
    fallback_device = _create_ble_device(address="AA:BB:CC:DD:EE:FF", name="Fallback")
    iface._discovery_manager = SimpleNamespace(  # type: ignore[assignment]
        discover_devices=lambda addr: [fallback_device] if addr else []
    )

    result = BLEInterface.find_device(iface, "aa-bb-cc-dd-ee-ff")

    assert result is fallback_device


def test_find_device_multiple_matches_raises():
    """Providing an address that matches multiple devices should raise BLEError."""
    # BLEDevice and BLEInterface already imported at top as ble_mod.BLEDevice, ble_mod.BLEInterface

    # Intentional constructor bypass for isolated find_device() behavior.
    iface = object.__new__(ble_mod.BLEInterface)
    devices = [
        _create_ble_device(address="AA:BB:CC:DD:EE:FF", name="Meshtastic-1"),
        _create_ble_device(address="AA-BB-CC-DD-EE-FF", name="Meshtastic-2"),
    ]
    iface._discovery_manager = SimpleNamespace(discover_devices=lambda _addr: devices)  # type: ignore[assignment]

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
            Prevent creating the scanner when the guard condition fails.
            
            Raises:
                AssertionError: Always raised with message "BleakScanner should not be instantiated when guard fails".
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

    discover_result = {
        "filtered": (
            filtered_device,
            SimpleNamespace(service_uuids=[SERVICE_UUID]),
        ),
        "other": (
            other_device,
            SimpleNamespace(service_uuids=["some-other-service"]),
        ),
    }
    monkeypatch.setattr(
        ble_mod,
        "BLEClient",
        lambda **_kwargs: _FakeDiscoveryClient(
            discover_result,
            async_await_impl=_assert_no_fallback(
                "Fallback should not be attempted when scan succeeds"
            ),
        ),
    )

    manager = DiscoveryManager()

    devices = manager.discover_devices(address=None)

    assert len(devices) == 1
    assert devices[0].address == filtered_device.address


def test_discovery_manager_uses_connected_strategy_when_scan_empty(monkeypatch):
    """When no devices are discovered via BLE scan, DiscoveryManager should fall back to connected strategy."""

    fallback_device = _create_ble_device("AA:BB", "Fallback")

    monkeypatch.setattr(
        ble_mod,
        "BLEClient",
        lambda **_kwargs: _FakeDiscoveryClient({}),
    )

    manager = DiscoveryManager()

    async def fake_connected(address: Optional[str], timeout: float) -> List[BLEDevice]:
        """
        Return the predefined fallback BLE device when invoked with the expected address and timeout.
        
        Parameters:
            address (Optional[str]): Expected device address; should be "AA:BB".
            timeout (float): Expected scan timeout; should equal ble_mod.BLEConfig.BLE_SCAN_TIMEOUT.
        
        Returns:
            List[BLEDevice]: A list containing the single fallback device.
        """
        assert address == "AA:BB"
        assert timeout == ble_mod.BLEConfig.BLE_SCAN_TIMEOUT
        return [fallback_device]

    manager.connected_strategy = _StrategyOverride(fake_connected)

    devices = manager.discover_devices(address="AA:BB")

    assert devices == [fallback_device]


def test_discovery_manager_skips_fallback_without_address(monkeypatch):
    """Connected-device fallback should not run when no address filter is provided."""

    monkeypatch.setattr(
        ble_mod,
        "BLEClient",
        lambda **_kwargs: _FakeDiscoveryClient({}),
    )

    manager = DiscoveryManager()

    fallback_called = False

    async def fake_connected(
        address: Optional[str], timeout: float
    ) -> List[BLEDevice]:  # pragma: no cover - should not run
        """
        Mark the connected-fallback as invoked for tests and return an empty list.
        
        Sets the enclosing `fallback_called` flag to True to indicate the fallback was exercised.
        Parameters:
            address (Optional[str]): Address passed to the fallback; accepted but ignored.
            timeout (float): Timeout value passed to the fallback; accepted but ignored.
        
        Returns:
            list: An empty list indicating no connected devices were found.
        """
        nonlocal fallback_called
        fallback_called = True
        return []

    manager.connected_strategy = _StrategyOverride(fake_connected)

    assert manager.discover_devices(address=None) == []


def test_discovery_manager_filters_targeted_scan_to_whitelist_match(monkeypatch):
    """Targeted discovery should keep only exact address/name matches."""
    target_device = _create_ble_device("AA:BB:CC:DD:EE:FF", "Target")
    other_meshtastic_device = _create_ble_device("11:22:33:44:55:66", "Other")

    discover_result = {
        "target": (
            target_device,
            SimpleNamespace(service_uuids=[]),
        ),
        "other": (
            other_meshtastic_device,
            SimpleNamespace(service_uuids=[SERVICE_UUID]),
        ),
    }
    monkeypatch.setattr(
        ble_mod,
        "BLEClient",
        lambda **_kwargs: _FakeDiscoveryClient(
            discover_result,
            async_await_impl=_assert_no_fallback(
                "Fallback should not run for targeted whitelist match"
            ),
        ),
    )

    manager = DiscoveryManager()
    devices = manager.discover_devices(address="AA:BB:CC:DD:EE:FF")

    assert devices == [target_device]


def test_parse_scan_response_prefers_exact_name_before_normalized_match():
    """Targeted scan should prefer an exact name match over normalized-name candidates."""
    exact_name_device = _create_ble_device("AA:BB:CC:DD:EE:FF", "My Device")
    normalized_only_device = _create_ble_device("11:22:33:44:55:66", "my device")

    response = {
        "exact": (exact_name_device, SimpleNamespace(service_uuids=[])),
        "normalized": (normalized_only_device, SimpleNamespace(service_uuids=[])),
    }

    devices = _parse_scan_response(response, whitelist_address="My Device")

    assert devices == [exact_name_device]


def test_filter_devices_rejects_ambiguous_normalized_name_matches():
    """Name matching should reject ambiguous normalized-name collisions."""
    devices = [
        _create_ble_device("AA:BB:CC:DD:EE:FF", "My Device"),
        _create_ble_device("11:22:33:44:55:66", "my device"),
    ]

    matches = _filter_devices_for_target_identifier(devices, "MY DEVICE")

    assert matches == []


def test_discovery_manager_destructor_does_not_close_client():
    """DiscoveryManager.__del__ should avoid active client close I/O during GC."""

    class StubDiscoveryClient:
        def __init__(self):
            """
            Initialize the test stub and reset its close-call counter.
            
            Sets the `close_calls` attribute to 0; tests increment this counter when the stub's `close()` is invoked to verify that discovery clients are not closed unexpectedly.
            """
            self.close_calls = 0

        def close(self):
            """
            Record that the client's close method was invoked by incrementing an internal call counter.
            
            This method exists for tests to track how many times close() was called on the object by incrementing the `close_calls` attribute.
            """
            self.close_calls += 1

    manager = DiscoveryManager()
    client = StubDiscoveryClient()
    manager._client = cast(BLEClient, client)

    manager.__del__()

    assert client.close_calls == 0
    assert manager._client is None


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
        validator.check_existing_client(client, "something-else", None, None) is False  # type: ignore[arg-type]
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
        Record a published pubsub message for test inspection.
        
        Appends (topic, kwargs) to the module-level `calls` list.
        
        Parameters:
            topic (str): Pubsub topic identifier.
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

    assert (
        not lingering
    ), f"Found lingering BLE threads after {max_wait_time}s: {lingering}"


@pytest.mark.parametrize("exc_type", [RuntimeError, OSError])
def test_receive_thread_specific_exceptions(monkeypatch, caplog, exc_type):
    """
    Verify that the BLE receive thread treats specific exceptions as fatal: it logs a fatal error message and invokes the interface's close().

    The test injects a client whose read_gatt_char raises the given exception type,
    triggers the receive loop, and asserts that the fatal log entry is present and that close() was called.
    """
    # logging and threading already imported at top

    # Set logging level to DEBUG to capture debug messages
    caplog.set_level(logging.DEBUG)

    class ExceptionClient(DummyClient):
        """Mock client that raises specific exceptions for testing."""

        def __init__(self, exception_type):
            """
            Create a test BLE client configured to raise the given exception from its faulting methods.
            
            Parameters:
                exception_type (type | Exception): Exception class or exception instance that the client will raise when its faulting methods are invoked.
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

    caplog.clear()

    client = ExceptionClient(exc_type)
    iface = _build_interface(monkeypatch, client, start_receive_thread=False)
    close_called = _attach_close_monitor(monkeypatch, iface)

    # Exercise the receive loop synchronously for deterministic assertions.
    iface._want_receive = True
    with iface._state_lock:
        iface.client = client  # type: ignore[assignment]

    iface._read_trigger.set()
    iface._receiveFromRadioImpl()

    assert "Fatal error in BLE receive thread" in caplog.text
    assert (
        close_called.is_set()
    ), f"Expected close() to be called for {exc_type.__name__}"

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
            Initialize the instance and set the read operation counter to 0.
            """
            super().__init__()
            self.read_count = 0

        def read_gatt_char(self, *_args, **_kwargs):
            """
            Simulate a GATT characteristic read that increments self.read_count and always fails.
            
            Increments self.read_count and then raises a BleakError with the message "transient error".
            
            Raises:
                BleakError: Always raised with message "transient error".
            """
            self.read_count += 1
            raise BleakError("transient error")

    client = BleakErrorClient()
    iface = _build_interface(monkeypatch, client, start_receive_thread=False)
    close_called = _attach_close_monitor(monkeypatch, iface)

    iface._want_receive = True

    with iface._state_lock:
        iface.client = client  # type: ignore[assignment]

    iface._read_trigger.set()
    iface._receiveFromRadioImpl()

    assert "Transient BLE read error, retrying" in caplog.text
    assert "Fatal BLE read error after retries" in caplog.text
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
            Initialize the mock BLE client and its notification/characteristic tracking.
            
            Attributes:
                start_notify_calls (list): Recorded calls to start_notify as tuples of the arguments passed.
                has_characteristic_map (dict): Maps characteristic UUID strings to booleans indicating presence. Initially sets
                    LEGACY_LOGRADIO_UUID, LOGRADIO_UUID, and FROMNUM_UUID to True.
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
            Determine whether the client exposes a GATT characteristic identified by the given UUID.
            
            Parameters:
                uuid (uuid.UUID or hashable): Characteristic UUID or key used to look up the client's characteristic map.
            
            Returns:
                bool: `True` if the UUID is present in the client's characteristic map, `False` otherwise.
            """
            return self.has_characteristic_map.get(uuid, False)

        def start_notify(self, *_args, **_kwargs):
            """
            Record a notification registration by saving the characteristic UUID and its handler.
            
            If called with at least two positional arguments, treats the first as the characteristic UUID and the second as the notification handler, and appends the pair to self.start_notify_calls. Any additional positional or keyword arguments are accepted and ignored.
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
    assert (
        LEGACY_LOGRADIO_UUID in registered_uuids
    ), "Legacy log notification should be registered"
    assert (
        LOGRADIO_UUID in registered_uuids
    ), "Current log notification should be registered"
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

    assert callable(
        legacy_call[1]
    ), "Legacy log notification should register a callable handler"
    assert callable(
        current_call[1]
    ), "Current log notification should register a callable handler"
    assert callable(
        fromnum_call[1]
    ), "FROMNUM notification should register a callable handler"

    iface.close()


def test_close_unsubscribes_tracked_notifications(monkeypatch):
    """close() should best-effort stop tracked notifications before client teardown."""
    client = DummyClient()
    iface = _build_interface(monkeypatch, client, start_receive_thread=False)

    iface._register_notifications(cast(BLEClient, client))
    assert len(iface._notification_manager) > 0

    iface.close()

    assert FROMNUM_UUID in client.stop_notify_calls


def test_reconnect_scheduler_tracks_threads():
    """ReconnectScheduler should start at most one reconnect thread and respect closing state."""

    state_manager = BLEStateManager()
    shutdown_event = threading.Event()

    class StubCoordinator:
        def __init__(self):
            """
            Initialize the instance and prepare storage for items created during tests.
            
            Creates an empty `created` list used to record items that this helper constructs.
            """
            self.created = []

        def create_thread(self, target, name, *, daemon=True, args=(), kwargs=None):
            """
            Create a lightweight thread-like SimpleNamespace, record it in self.created, and return it.
            
            Parameters:
                target (callable): The callable intended to run when the thread is started.
                name (str): Identifier for the thread-like object.
                daemon (bool): Whether the thread-like object is considered a daemon.
                args (tuple): Positional arguments associated with the target.
                kwargs (dict | None): Keyword arguments associated with the target; treated as {} when None.
            
            Returns:
                SimpleNamespace: An object with attributes `target`, `args`, `name`, `daemon`, `kwargs`, and `started`, plus an `is_alive()` callable that returns whether `started` is True.
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
                thread (object): Thread-like object with a writable `started` attribute.
            """
            thread.started = True

    worker = SimpleNamespace(attempt_reconnect_loop=lambda *_args, **_kwargs: None)
    coordinator = StubCoordinator()
    scheduler = ReconnectScheduler(  # noqa: PLR0913  # type: ignore[arg-type]
        state_manager,
        state_manager.lock,
        coordinator,  # type: ignore[arg-type]
        worker,  # type: ignore[arg-type]
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
            Initialize the stub retry policy used by reconnect tests.
            
            Sets initial state for test assertions.
            
            Attributes:
                reset_called: True if reset() has been invoked.
                _attempt_count: Number of connection attempts recorded.
            """
            self.reset_called = False
            self._attempt_count = 0

        def reset(self):
            """
            Reset the retry policy to its initial state.
            
            Sets the internal attempt counter to 0 and records that a reset occurred by setting `reset_called` to True.
            """
            self.reset_called = True
            self._attempt_count = 0

        def get_attempt_count(self):
            """
            Get the number of reconnect attempts recorded by this policy.
            
            Returns:
                int: The number of reconnect attempts recorded.
            """
            return self._attempt_count

        def next_attempt(self):
            """
            Determine the delay before the next retry and whether another attempt should be made.
            
            Increments the internal attempt counter as a side effect.
            
            Returns:
                tuple: (delay_seconds, continue_retry)
                    delay_seconds (float): Seconds to wait before the next attempt.
                    continue_retry (bool): `True` to perform another attempt, `False` otherwise.
            """
            self._attempt_count += 1
            return 0.1, False

    class DummyInterface:
        BLEError = RuntimeError

        def __init__(self):
            """
            Create a minimal stub interface for reconnect-related tests.
            
            Initializes lightweight test doubles and records connect invocations.
            
            Attributes:
                _reconnect_policy (StubPolicy): Retry/backoff policy used by reconnect attempts.
                _notification_manager (_ReconnectTestNotificationManager): Tracks cleanup and resubscribe requests.
                _state_manager (types.SimpleNamespace): Exposes `is_closing` (bool) to simulate shutdown state.
                _reconnect_scheduler (_ReconnectTestScheduler): Manages reconnect thread reference and clearing.
                auto_reconnect (bool): Whether automatic reconnect attempts are enabled.
                is_connection_closing (bool): Simulates an in-progress connection close.
                is_connection_connected (bool): Simulates an active connection state.
                address (str): Device address used for connect attempts.
                client: Placeholder BLE client object.
                connect_calls (list): Records addresses passed to `connect` for assertions in tests.
            """
            self._reconnect_policy = StubPolicy()
            self._notification_manager = _ReconnectTestNotificationManager()
            self._state_manager = SimpleNamespace(is_closing=False)
            self._reconnect_scheduler = _ReconnectTestScheduler()
            self.auto_reconnect = True
            self.is_connection_closing = False
            self.is_connection_connected = False
            self.address = "addr"
            self.client = object()
            self.connect_calls = []

        def connect(self, address):
            """
            Record that a connection was attempted for the given device address by appending it to this instance's `connect_calls` list.
            
            Parameters:
                address (str): Bluetooth address or device identifier that was attempted and will be appended to `connect_calls`.
            """
            self.connect_calls.append(address)

    iface = DummyInterface()
    worker = ReconnectWorker(iface, iface._reconnect_policy)  # type: ignore[arg-type]
    worker.attempt_reconnect_loop(True, threading.Event())

    assert iface.connect_calls == ["addr"]
    assert iface._notification_manager.cleaned == 0
    assert len(iface._notification_manager.resubscribed) == 0
    assert iface._reconnect_policy.reset_called is True
    assert iface._reconnect_scheduler.cleared is True


def test_reconnect_worker_respects_retry_limits(monkeypatch):
    """
    Ensure ReconnectWorker respects the retry policy and stops after the allowed attempts when connect continuously fails.

    Simulates an interface whose connect() always raises and a LimitedPolicy that permits a single retry. Verifies that:
    - connect() is attempted the expected number of times (2 attempts),
    - no notification resubscriptions occur,
    - the retry delay from the policy (0.25) was waited once,
    - the reconnect policy was reset,
    - the reconnect scheduler cleared its thread reference.
    """

    sleep_calls = []

    # Mock shutdown_event.wait to capture the sleep delay instead of actually waiting
    def mock_wait(timeout=None):
        """
        Simulate waiting for a shutdown event while recording requested timeouts.

        Records the provided timeout value into the surrounding `sleep_calls` list when not None, and always returns `False` to indicate the wait timed out (not interrupted by a shutdown/notification).

        Parameters
        ----------
            timeout (float | None): Duration in seconds to wait; if None, no value is recorded.

        Returns
        -------
            bool: `False` to indicate a timeout (i.e., the wait was not interrupted).

        """
        if timeout is not None:
            sleep_calls.append(timeout)
        # Return False to simulate timeout (not interrupted by shutdown)
        return False

    class LimitedPolicy:
        def __init__(self):
            """
            Initialize a stub reconnect policy for tests, resetting counters and flags.
            
            Attributes:
                reset_called (bool): True if reset() has been invoked.
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
            Return the delay before the next retry and whether another retry should be attempted.
            
            Returns:
                tuple: (delay_seconds, continue_flag)
                    delay_seconds (float): Seconds to wait before the next retry (0.25).
                    continue_flag (bool): `True` if another retry should be attempted for the current policy cycle, `False` otherwise.
            """
            self.attempts += 1
            return 0.25, self.attempts < 2

    class FailingInterface:
        BLEError = RuntimeError

        def __init__(self):
            """
            Initialize a minimal stub interface used by reconnect tests.

            Attributes:
                _reconnect_policy (LimitedPolicy): Policy controlling reconnect attempts.
                _notification_manager (_ReconnectTestNotificationManager): Manages notification cleanup and resubscription.
                _state_manager (SimpleNamespace): Runtime state flags (contains `is_closing`).
                _reconnect_scheduler (_ReconnectTestScheduler): Scheduler that manages reconnect threads.
                auto_reconnect (bool): Whether automatic reconnect attempts are enabled.
                is_connection_closing (bool): Indicates an in-progress connection close.
                is_connection_connected (bool): Indicates whether the interface is currently connected.
                address (str): Remote device address used for connection attempts.
                client: Placeholder for the BLE client instance (initially None).
                connect_attempts (int): Counter of connect() invocation attempts.

            """
            self._reconnect_policy = LimitedPolicy()
            self._notification_manager = _ReconnectTestNotificationManager(
                fail_on_resubscribe=True
            )
            self._state_manager = SimpleNamespace(is_closing=False)
            self._reconnect_scheduler = _ReconnectTestScheduler()
            self.auto_reconnect = True
            self.is_connection_closing = False
            self.is_connection_connected = False
            self.address = "addr"
            self.client = None
            self.connect_attempts = 0

        def connect(self, *_args, **_kwargs):
            """
            Simulate a failing connection attempt for tests and record the attempt.
            
            Increments the instance's `connect_attempts` counter and raises an error to emulate a failed connection.
            
            Raises:
                self.BLEError: raised with message "boom".
            """
            self.connect_attempts += 1
            raise self.BLEError("boom")

    iface = FailingInterface()
    worker = ReconnectWorker(iface, iface._reconnect_policy)  # type: ignore[arg-type]
    shutdown_event = threading.Event()
    monkeypatch.setattr(shutdown_event, "wait", mock_wait)

    worker.attempt_reconnect_loop(True, shutdown_event)

    assert iface.connect_attempts == 2
    assert iface._notification_manager.cleaned == 0
    assert sleep_calls == [0.25]
    assert iface._reconnect_policy.reset_called is True
    assert iface._reconnect_scheduler.cleared is True