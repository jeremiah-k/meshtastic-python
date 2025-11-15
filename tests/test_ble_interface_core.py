"""Tests for the BLE interface module - Core functionality."""

import asyncio
import inspect
import logging
import threading
import time
from types import SimpleNamespace

import pytest  # type: ignore[import-untyped]
from bleak.exc import BleakError  # type: ignore[import-untyped]
from pubsub import pub  # type: ignore[import-untyped]

# Import common fixtures
from test_ble_interface_fixtures import DummyClient, _build_interface

# Import meshtastic modules for use in tests
import meshtastic.ble_interface as ble_mod
from meshtastic.ble_interface import (
    FROMNUM_UUID,
    LEGACY_LOGRADIO_UUID,
    LOGRADIO_UUID,
    SERVICE_UUID,
    BLEInterface,
    BLEStateManager,
    ConnectionState,
    ConnectionValidator,
    DiscoveryManager,
    ReconnectScheduler,
    ReconnectWorker,
)


def _create_ble_device(address: str, name: str):
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
    params = {"address": address, "name": name}
    signature = inspect.signature(ble_mod.BLEDevice.__init__)
    if "details" in signature.parameters:
        params["details"] = {}
    if "rssi" in signature.parameters:
        params["rssi"] = 0
    return ble_mod.BLEDevice(**params)


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
        Provide a fake connected-device lookup that always returns the predefined `fallback_device`.

        Returns
        -------
            list: A single-element list containing the preset `fallback_device`.

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

    with pytest.raises(BLEInterface.BLEError) as excinfo:
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
            Prevent instantiation by always raising an AssertionError when the private-backend guard disallows it.

            This initializer exists solely to signal that creating a BleakScanner is not permitted under the failing guard.

            Raises
            ------
                AssertionError: "BleakScanner should not be instantiated when guard fails"

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
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def discover(self, **_kwargs):
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
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def discover(self, **_kwargs):
            return {}

        @staticmethod
        def async_await(coro):
            return asyncio.run(coro)

    monkeypatch.setattr(ble_mod, "BLEClient", lambda **_kwargs: FakeClient())

    manager = DiscoveryManager()

    async def fake_connected(address, timeout):
        assert address == "AA:BB"
        assert timeout == ble_mod.BLEConfig.BLE_SCAN_TIMEOUT
        return [fallback_device]

    manager.connected_strategy = SimpleNamespace(discover=fake_connected)

    devices = manager.discover_devices(address="AA:BB")

    assert devices == [fallback_device]


def test_discovery_manager_skips_fallback_without_address(monkeypatch):
    """Connected-device fallback should not run when no address filter is provided."""

    class FakeClient:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def discover(self, **_kwargs):
            return {}

        @staticmethod
        def async_await(coro):  # pragma: no cover - fallback should not be hit
            return asyncio.run(coro)

    monkeypatch.setattr(ble_mod, "BLEClient", lambda **_kwargs: FakeClient())

    manager = DiscoveryManager()

    fallback_called = False

    async def fake_connected(address, timeout):  # pragma: no cover - should not run
        nonlocal fallback_called
        fallback_called = True
        return []

    manager.connected_strategy = SimpleNamespace(discover=fake_connected)

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
    with pytest.raises(BLEInterface.BLEError) as excinfo:
        validator.validate_connection_request()
    assert "closing" in str(excinfo.value)

    assert state_manager.transition_to(ConnectionState.DISCONNECTED) is True
    assert state_manager.transition_to(ConnectionState.CONNECTING) is True
    with pytest.raises(BLEInterface.BLEError) as excinfo:
        validator.validate_connection_request()
    assert "connection in progress" in str(excinfo.value)


def test_connection_validator_existing_client_checks(monkeypatch):
    """check_existing_client should allow reuse only when the requested identifier matches."""

    state_manager = BLEStateManager()
    validator = ConnectionValidator(state_manager, state_manager._state_lock)
    client = DummyClient()
    client.is_connected = lambda: True

    assert validator.check_existing_client(client, None, None, None) is True
    assert validator.check_existing_client(client, "dummy", "dummy", "dummy") is True
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

    # Give threads a moment to clean up
    time.sleep(0.1)

    # Check for specific BLE interface threads that should be cleaned up
    # BLEClient thread might persist in test environment, so focus on interface-managed threads
    lingering = [
        thread.name
        for thread in threading.enumerate()
        if thread.name.startswith("BLE") and thread.name != "BLEClient"
    ]
    assert not lingering, f"Found lingering BLE threads: {lingering}"


def test_receive_thread_specific_exceptions(monkeypatch, caplog):
    """Test that receive thread handles specific exceptions correctly."""
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
                Raise the client's configured exception to simulate a failing GATT characteristic read.

                Raises
                ------
                    Exception: An instance of the client's `exception_type` with the message "test".

                """
                raise self.exception_type("test")

        client = ExceptionClient(exc_type)
        iface = _build_interface(monkeypatch, client)

        # Mock the close method to track if it's called
        original_close = iface.close
        close_called = threading.Event()

        def mock_close(close_called=close_called, original_close=original_close):
            """
            Signal that close was invoked and then call the original close function.

            Parameters
            ----------
                close_called (threading.Event): Event that will be set to signal that close was invoked.
                original_close (Callable[[], Any]): The original close callable to invoke.

            Returns
            -------
                Any: The value returned by `original_close`.

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
        assert (
            close_called.is_set()
        ), f"Expected close() to be called for {exc_type.__name__}"

        # Clean up
        iface._want_receive = False
        try:
            iface.close()
        except Exception as exc:  # noqa: BLE001 - cleanup best-effort in tests
            # Log for visibility; still allow test to proceed with cleanup.
            print(f"Cleanup error in iface.close(): {exc!r}")


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
            Determine whether the client's characteristic map contains the given characteristic UUID.

            Parameters
            ----------
                uuid (str | uuid.UUID): Characteristic UUID to check.

            Returns
            -------
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
    iface._register_notifications(client)

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


def test_reconnect_scheduler_tracks_threads(monkeypatch):
    """ReconnectScheduler should start at most one reconnect thread and respect closing state."""

    state_manager = BLEStateManager()
    shutdown_event = threading.Event()

    class StubCoordinator:
        def __init__(self):
            self.created = []

        def create_thread(self, target, args, name, daemon):
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
            self.reset_called = False
            self._attempt_count = 0

        def reset(self):
            self.reset_called = True
            self._attempt_count = 0

        def get_attempt_count(self):
            return self._attempt_count

        def next_attempt(self):
            self._attempt_count += 1
            return 0.1, False

    class StubNotificationManager:
        def __init__(self):
            self.cleaned = 0
            self.resubscribed = []

        def cleanup_all(self):
            self.cleaned += 1

        def resubscribe_all(self, client, timeout):
            self.resubscribed.append((client, timeout))

    class StubScheduler:
        def __init__(self):
            self.cleared = False

        def clear_thread_reference(self):
            self.cleared = True

    class DummyInterface:
        BLEError = RuntimeError

        def __init__(self):
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
            self.connect_calls.append(address)

    iface = DummyInterface()
    worker = ReconnectWorker(iface)
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
            self.reset_called = False
            self.attempts = 0

        def reset(self):
            self.reset_called = True
            self.attempts = 0

        def get_attempt_count(self):
            return self.attempts

        def next_attempt(self):
            self.attempts += 1
            return 0.25, self.attempts < 2

    class StubNotificationManager:
        def __init__(self):
            self.cleaned = 0

        def cleanup_all(self):
            self.cleaned += 1

        def resubscribe_all(self, *_args, **_kwargs):  # pragma: no cover - no client
            raise AssertionError("Should not resubscribe without a client")

    class StubScheduler:
        def __init__(self):
            self.cleared = False

        def clear_thread_reference(self):
            self.cleared = True

    class FailingInterface:
        BLEError = RuntimeError

        def __init__(self):
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
            self.connect_attempts += 1
            raise self.BLEError("boom")

    iface = FailingInterface()
    worker = ReconnectWorker(iface)
    worker.attempt_reconnect_loop(True, threading.Event())

    assert iface.connect_attempts == 2
    assert iface._notification_manager.cleaned == 2
    assert sleep_calls == [0.25]
    assert iface._reconnect_policy.reset_called is True
    assert iface._reconnect_scheduler.cleared is True
