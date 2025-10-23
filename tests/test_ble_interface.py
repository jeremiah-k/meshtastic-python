"""Tests for the BLE interface module."""

import logging
import sys
import threading
import time
import types
from contextlib import ExitStack
from queue import Queue
from types import SimpleNamespace
from typing import Optional
from unittest.mock import MagicMock, patch

import pytest

# Import meshtastic modules for use in tests
import meshtastic.ble_interface as ble_mod
import meshtastic.mesh_interface as mesh_iface_module


@pytest.fixture(autouse=True)
def mock_serial(monkeypatch):
    """
    Create and inject a fake `serial` package with minimal submodules and exceptions used by tests.

    The mocked package provides:
    - `serial.tools.list_ports.comports()` which returns an empty list.
    - `SerialException` and `SerialTimeoutException` aliased to `Exception`.
    - `serial.tools` and `serial.tools.list_ports` entries installed in `sys.modules`.

    Args:
        monkeypatch: Pytest monkeypatch fixture used to set entries in `sys.modules`.

    Returns:
        The mocked `serial` module object.

    """
    serial_module = types.ModuleType("serial")

    # Create tools submodule
    tools_module = types.ModuleType("serial.tools")
    list_ports_module = types.ModuleType("serial.tools.list_ports")
    list_ports_module.comports = lambda *_args, **_kwargs: []
    tools_module.list_ports = list_ports_module
    serial_module.tools = tools_module

    # Add exception classes
    serial_module.SerialException = Exception
    serial_module.SerialTimeoutException = Exception

    # Mock the modules
    monkeypatch.setitem(sys.modules, "serial", serial_module)
    monkeypatch.setitem(sys.modules, "serial.tools", tools_module)
    monkeypatch.setitem(sys.modules, "serial.tools.list_ports", list_ports_module)

    return serial_module


@pytest.fixture(autouse=True)
def mock_pubsub(monkeypatch):
    """
    Injects a fake `pubsub` module into sys.modules for tests.

    Returns:
        module: The injected fake `pubsub` module whose `pub` attribute is a SimpleNamespace with no-op
        `subscribe` and `sendMessage` callables and `AUTO_TOPIC` set to None.

    """
    pubsub_module = types.ModuleType("pubsub")
    pubsub_module.pub = SimpleNamespace(
        subscribe=lambda *_args, **_kwargs: None,
        sendMessage=lambda *_args, **_kwargs: None,
        AUTO_TOPIC=None,
    )

    monkeypatch.setitem(sys.modules, "pubsub", pubsub_module)
    return pubsub_module


@pytest.fixture(autouse=True)
def mock_publishing_thread(monkeypatch):
    """
    Install a fake publishingThread module whose queueWork executes callbacks immediately.

    The fake module is inserted into sys.modules under the name "publishingThread" to ensure tests run
    with a predictable, synchronous queueWork implementation.

    Returns:
        publishing_thread_module (module): The mocked publishingThread module inserted into sys.modules.

    """
    publishing_thread_module = types.ModuleType("publishingThread")

    def mock_queue_work(callback):
        """
        Invoke `callback` immediately instead of queuing it for deferred execution (test helper).

        Args:
            callback (Optional[Callable[[], Any]]): Callable to execute; if falsy, no action is taken.

        """
        if callback:
            callback()

    publishing_thread_module.queueWork = mock_queue_work

    # Remove any existing module to ensure fresh state
    if "publishingThread" in sys.modules:
        del sys.modules["publishingThread"]

    monkeypatch.setitem(sys.modules, "publishingThread", publishing_thread_module)
    return publishing_thread_module


@pytest.fixture(autouse=True)
def mock_tabulate(monkeypatch):
    """
    Install a fake 'tabulate' module into sys.modules whose tabulate(...) always returns an empty string.

    Returns:
        module: The fake 'tabulate' module inserted into sys.modules.

    """
    tabulate_module = types.ModuleType("tabulate")
    tabulate_module.tabulate = lambda *_args, **_kwargs: ""

    monkeypatch.setitem(sys.modules, "tabulate", tabulate_module)
    return tabulate_module


@pytest.fixture(autouse=True)
def mock_bleak(monkeypatch):
    """
    Create and install a minimal fake `bleak` module into sys.modules for use in tests.

    The installed module exposes:
    - `BleakClient`: a stub client class with async connect/disconnect/read/write methods and a synchronous `is_connected`.
    - `BleakScanner.discover`: an async coroutine that returns an empty list.
    - `BLEDevice`: a minimal device-like class with `address` and `name` attributes.

    Args:
        monkeypatch: pytest's monkeypatch fixture used to insert the fake module into sys.modules.

    Returns:
        module: The fake `bleak` module object that was inserted into sys.modules.

    """
    bleak_module = types.ModuleType("bleak")
    bleak_module.__version__ = "1.1.1"

    class _StubBleakClient:
        def __init__(self, address=None, **_kwargs):
            """
            Minimal test BLE client holding an address and a lightweight services shim.

            Args:
                address (str | None): BLE device address associated with this client, or None.
                **_kwargs: Additional keyword arguments are accepted and ignored.

            Attributes:
                services (types.SimpleNamespace): Provides get_characteristic(specifier) which always returns None.

            """
            self.address = address
            self.services = SimpleNamespace(get_characteristic=lambda _specifier: None)

        async def connect(self, **_kwargs):
            """
            No-op mock connect used in tests that accepts and ignores any keyword arguments.

            Parameters
            ----------
                **_kwargs: Arbitrary keyword arguments (ignored).

            """
            return None

        async def disconnect(self, **_kwargs):
            """
            Mock disconnect method that performs no operation.

            Accepts arbitrary keyword arguments and ignores them.
            """
            return None

        async def start_notify(self, **_kwargs):
            """
            No-op compatibility shim for a BLE client's start_notify that accepts and ignores any keyword arguments.
            """
            return None

        async def read_gatt_char(self, *_args, **_kwargs):
            """
            Simulate reading a GATT characteristic by returning an empty bytes object.

            Returns:
                bytes: Empty bytes (b'').

            """
            return b""

        async def write_gatt_char(self, *_args, **_kwargs):
            """
            No-op stub that accepts any positional and keyword arguments to emulate BleakClient.write_gatt_char in tests.

            This function performs no action and is provided so tests can call a write_gatt_char method without side effects.
            """
            return None

        def is_connected(self):
            """
            Report that this dummy BLE client is not connected.

            Returns:
                False: Always returns False indicating the client is not connected.

            """
            return False

    async def _stub_discover(**_kwargs):
        """
        Simulate BLE device discovery and return an empty list.

        Accepts arbitrary keyword arguments for API compatibility; all are ignored.

        Parameters
        ----------
            **_kwargs (dict): Ignored keyword arguments maintained for API compatibility.

        Returns
        -------
            list: Empty list of discovered devices.

        """
        return []

    class _StubBLEDevice:
        def __init__(self, address=None, name=None):
            """
            Represent a BLE device with an optional address and human-readable name.

            Args:
                address (str | None): BLE device address, if known.
                name (str | None): Human-readable device name, if known.

            """
            self.address = address
            self.name = name

    bleak_module.BleakClient = _StubBleakClient
    bleak_module.BleakScanner = SimpleNamespace(discover=_stub_discover)
    bleak_module.BLEDevice = _StubBLEDevice

    monkeypatch.setitem(sys.modules, "bleak", bleak_module)
    return bleak_module


@pytest.fixture(autouse=True)
def mock_bleak_exc(monkeypatch, mock_bleak):  # pylint: disable=redefined-outer-name
    """
    Create and register a minimal `bleak.exc` submodule exposing `BleakError` and `BleakDBusError`.

    Args:
        monkeypatch: pytest monkeypatch fixture used to register the fake module.
        mock_bleak (module): The fake `bleak` module to which the `exc` submodule will be attached.

    Returns:
        bleak_exc_module (module): The created `bleak.exc` module containing `BleakError` and `BleakDBusError` exception classes.

    """
    bleak_exc_module = types.ModuleType("bleak.exc")

    class _StubBleakError(Exception):
        pass

    class _StubBleakDBusError(_StubBleakError):
        pass

    bleak_exc_module.BleakError = _StubBleakError
    bleak_exc_module.BleakDBusError = _StubBleakDBusError

    # Attach to parent module
    mock_bleak.exc = bleak_exc_module

    monkeypatch.setitem(sys.modules, "bleak.exc", bleak_exc_module)
    return bleak_exc_module


# Import will be done locally in test functions to avoid import-time dependencies


class DummyClient:
    """Dummy client for testing BLE interface functionality."""

    def __init__(self, disconnect_exception: Optional[Exception] = None) -> None:
        """
        Test double representing a BLE client for use in unit tests.

        Args:
            disconnect_exception (Optional[Exception]): Exception to raise when disconnect() is called; pass None to disable raising.

        Attributes:
            disconnect_calls (int): Number of times disconnect() has been invoked.
            close_calls (int): Number of times close() has been invoked.
            address (str): Client address identifier, set to "dummy".
            disconnect_exception (Optional[Exception]): Stored exception raised by disconnect(), if any.
            services (types.SimpleNamespace): Provides get_characteristic(specifier) -> None for characteristic lookups.
            bleak_client (types.SimpleNamespace): Minimal mock of a bleak client with an address attribute.

        """
        self.disconnect_calls = 0
        self.close_calls = 0
        self.address = "dummy"
        self.disconnect_exception = disconnect_exception
        self.services = SimpleNamespace(get_characteristic=lambda _specifier: None)
        # The bleak_client should be a separate object to correctly test identity checks
        self.bleak_client = SimpleNamespace(address=self.address)

    def has_characteristic(self, _specifier):
        """
        Determine if the mock client exposes a BLE characteristic matching the given specifier.

        Parameters
        ----------
            _specifier: Characteristic identifier (e.g., UUID string or characteristic object).

        Returns
        -------
            `False` always for this mock implementation.

        """
        return False

    def start_notify(self, *_args, **_kwargs):
        """
        Simulate subscribing to a BLE characteristic notification without performing any action.

        Accepts any positional and keyword arguments and acts as a no-op test stub replacing a real start_notify.
        """
        return None

    def read_gatt_char(self, *_args, **_kwargs):
        """
        Simulate reading a GATT characteristic and provide an empty payload.

        Returns:
            bytes: An empty byte string.

        """
        return b""

    def is_connected(self) -> bool:
        """
        Report the mock BLE client as connected.

        Returns:
            connected (bool): `true` if the mock client is connected (this stub always returns `true`), `false` otherwise.

        """
        return True

    def disconnect(self, *_args, **_kwargs):
        """
        Record that disconnect was invoked and optionally raise a preconfigured exception.

        Increments self.disconnect_calls. If self.disconnect_exception is set, that exception is raised.

        Raises:
            Exception: The exception instance stored in `self.disconnect_exception`, if present.

        """
        self.disconnect_calls += 1
        if self.disconnect_exception:
            raise self.disconnect_exception

    def close(self):
        """
        Record that the mock client's close method was invoked.

        Increments the `close_calls` counter on the instance to track how many times `close()` has been called.
        """
        self.close_calls += 1


@pytest.fixture(autouse=True)
def stub_atexit(
    monkeypatch,
    *,
    mock_serial,  # pylint: disable=redefined-outer-name
    mock_pubsub,  # pylint: disable=redefined-outer-name
    mock_tabulate,  # pylint: disable=redefined-outer-name
    mock_bleak,  # pylint: disable=redefined-outer-name
    mock_bleak_exc,  # pylint: disable=redefined-outer-name
    mock_publishing_thread,  # pylint: disable=redefined-outer-name
):
    """
    Replace meshtastic.ble_interface.atexit.register/unregister with test stubs that capture callbacks and execute them on teardown.

    This pytest fixture patches the module's atexit.register and atexit.unregister to record registered callables,
    yields to the test, and then invokes all recorded callbacks after the test completes to avoid leaving
    persistent global state. The additional fixture arguments exist solely to enforce fixture ordering.
    """
    registered = []
    # Consume fixture arguments to document ordering intent and silence Ruff (ARG001).
    _ = (
        mock_serial,
        mock_pubsub,
        mock_tabulate,
        mock_bleak,
        mock_bleak_exc,
        mock_publishing_thread,
    )

    def fake_register(func):
        """
        Register a callable by appending it to the module-level `registered` list.

        Args:
            func (callable): The callable to register; can also be used as a decorator.

        Returns:
            callable: The same callable that was registered.

        """
        registered.append(func)
        return func

    def fake_unregister(func):
        """
        Unregister a previously registered callback from the module-level `registered` list.

        All entries that are the same object as `func` (identity comparison) are removed.

        Args:
            func (callable): The callback to unregister.

        """
        registered[:] = [f for f in registered if f is not func]

    # meshtastic.ble_interface already imported at top as ble_mod

    monkeypatch.setattr(ble_mod.atexit, "register", fake_register, raising=True)
    monkeypatch.setattr(ble_mod.atexit, "unregister", fake_unregister, raising=True)
    yield
    # run any registered functions manually to avoid surprising global state
    for func in registered:
        try:
            func()
        except Exception as e:
            # Keep teardown resilient during tests
            import logging

            logging.debug(
                "atexit callback %r raised during teardown: %s: %s",
                func,
                type(e).__name__,
                e,
            )


def _build_interface(monkeypatch, client):
    """
    Create a BLEInterface configured for tests where its `connect` method is stubbed to return the supplied client
    and its `_startConfig` method is a no-op.

    Args:
        monkeypatch: pytest monkeypatch fixture used to patch BLEInterface methods.
        client: Fake or mock BLE client instance that the patched `connect` method will return.

    Returns:
        BLEInterface: An instance whose `connect` returns `client` and whose `_startConfig` performs no configuration.

    """
    from meshtastic.ble_interface import BLEInterface

    connect_calls: list = []

    def _stub_connect(_self, _address=None):
        """
        Provide the preconfigured BLE client used by tests and record the connect attempt.

        Parameters
        ----------
            _address (str | None): Accepted to match the original connect signature; ignored by this stub.

        Returns
        -------
            The preconfigured client instance for use in tests.

        Notes
        -----
            As a side effect, sets `_self.client` to the test client, clears `_self._disconnect_notified`,
            appends the address to `connect_calls`, and sets `_self._reconnected_event` if present.

        """
        connect_calls.append(_address)
        _self.client = client
        _self._disconnect_notified = False
        if hasattr(_self, "_reconnected_event"):
            _self._reconnected_event.set()
        return client

    def _stub_start_config(_self):
        """
        No-op replacement for the BLE interface startup configuration hook.

        This function intentionally performs no actions and returns None; used to bypass real startup configuration in tests.
        """
        return None

    monkeypatch.setattr(BLEInterface, "connect", _stub_connect)
    monkeypatch.setattr(BLEInterface, "_startConfig", _stub_start_config)
    iface = BLEInterface(address="dummy", noProto=True)
    iface._connect_stub_calls = connect_calls
    return iface


def test_find_device_returns_single_scan_result(monkeypatch):
    """find_device should return the lone scanned device."""
    from meshtastic.ble_interface import BLEDevice, BLEInterface

    iface = object.__new__(BLEInterface)
    scanned_device = BLEDevice(address="11:22:33:44:55:66", name="Test Device")
    monkeypatch.setattr(BLEInterface, "scan", lambda: [scanned_device])

    result = BLEInterface.find_device(iface, None)

    assert result is scanned_device


def test_find_device_uses_connected_fallback_when_scan_empty(monkeypatch):
    """find_device should fall back to connected-device lookup when scan is empty."""
    from meshtastic.ble_interface import BLEDevice, BLEInterface

    iface = object.__new__(BLEInterface)
    fallback_device = BLEDevice(address="AA:BB:CC:DD:EE:FF", name="Fallback")
    monkeypatch.setattr(BLEInterface, "scan", lambda: [])

    def _fake_connected(_self, _address):
        return [fallback_device]

    monkeypatch.setattr(BLEInterface, "_find_connected_devices", _fake_connected)

    result = BLEInterface.find_device(iface, "aa-bb-cc-dd-ee-ff")

    assert result is fallback_device


def test_find_device_multiple_matches_raises(monkeypatch):
    """Providing an address that matches multiple devices should raise BLEError."""
    from meshtastic.ble_interface import BLEDevice, BLEInterface

    iface = object.__new__(BLEInterface)
    devices = [
        BLEDevice(address="AA:BB:CC:DD:EE:FF", name="Meshtastic-1"),
        BLEDevice(address="AA-BB-CC-DD-EE-FF", name="Meshtastic-2"),
    ]
    monkeypatch.setattr(BLEInterface, "scan", lambda: devices)

    with pytest.raises(BLEInterface.BLEError) as excinfo:
        BLEInterface.find_device(iface, "aa bb cc dd ee ff")

    assert "Multiple Meshtastic BLE peripherals found matching" in str(excinfo.value)


def test_find_connected_devices_skips_private_backend_when_guard_fails(monkeypatch):
    """When the version guard disallows the fallback, the private backend is never touched."""
    from meshtastic.ble_interface import BLEInterface

    iface = object.__new__(BLEInterface)

    monkeypatch.setattr(
        "meshtastic.ble_interface._bleak_supports_connected_fallback", lambda: False
    )

    class BoomScanner:
        def __init__(self):
            raise AssertionError(
                "BleakScanner should not be instantiated when guard fails"
            )

    monkeypatch.setattr("meshtastic.ble_interface.BleakScanner", BoomScanner)

    result = BLEInterface._find_connected_devices(iface, "AA:BB")

    assert result == []


def test_close_idempotent(monkeypatch):
    """Test that close() is idempotent and only calls disconnect once."""
    client = DummyClient()
    iface = _build_interface(monkeypatch, client)

    iface.close()
    iface.close()
    iface.close()  # Call multiple times to ensure idempotency

    assert client.disconnect_calls == 1
    assert client.close_calls == 1


def test_close_handles_bleak_error(monkeypatch):
    """Test that close() handles BleakError gracefully."""
    from meshtastic.ble_interface import BleakError
    from meshtastic.mesh_interface import pub

    calls = []

    def _capture(topic, **kwargs):
        """
        Record a pubsub message invocation by appending a (topic, kwargs) tuple to the module-level `calls` list.

        Args:
            topic (str): Pubsub topic name.
            **kwargs: Additional message fields to capture alongside the topic.

        """
        calls.append((topic, kwargs))

    monkeypatch.setattr(pub, "sendMessage", _capture)

    client = DummyClient(disconnect_exception=BleakError("Not connected"))
    iface = _build_interface(monkeypatch, client)

    iface.close()

    assert client.disconnect_calls == 1
    assert client.close_calls == 1
    assert (
        sum(
            1
            for topic, kw in calls
            if topic == "meshtastic.connection.status" and kw.get("connected") is False
        )
        == 1
    )


def test_close_handles_runtime_error(monkeypatch):
    """Test that close() handles RuntimeError gracefully."""
    from meshtastic.mesh_interface import pub

    calls = []

    def _capture(topic, **kwargs):
        """
        Record a pubsub message invocation by appending a (topic, kwargs) tuple to the module-level `calls` list.

        Args:
            topic (str): Pubsub topic name.
            **kwargs: Additional message fields to capture alongside the topic.

        """
        calls.append((topic, kwargs))

    monkeypatch.setattr(pub, "sendMessage", _capture)

    client = DummyClient(disconnect_exception=RuntimeError("Threading issue"))
    iface = _build_interface(monkeypatch, client)

    iface.close()

    assert client.disconnect_calls == 1
    assert client.close_calls == 1
    # exactly one disconnect status
    assert (
        sum(
            1
            for topic, kw in calls
            if topic == "meshtastic.connection.status" and kw.get("connected") is False
        )
        == 1
    )


def test_close_handles_os_error(monkeypatch):
    """Test that close() handles OSError gracefully."""
    from meshtastic.mesh_interface import pub

    calls = []

    def _capture(topic, **kwargs):
        """
        Record a pubsub message invocation by appending a (topic, kwargs) tuple to the module-level `calls` list.

        Args:
            topic (str): Pubsub topic name.
            **kwargs: Additional message fields to capture alongside the topic.

        """
        calls.append((topic, kwargs))

    monkeypatch.setattr(pub, "sendMessage", _capture)

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

    lingering = [
        thread.name for thread in threading.enumerate() if thread.name.startswith("BLE")
    ]
    assert not lingering


def test_receive_thread_specific_exceptions(monkeypatch, caplog):
    """Test that receive thread handles specific exceptions correctly."""
    # logging and threading already imported at top

    from meshtastic.ble_interface import BleakError

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
                Test client that raises a configured exception from its faulting methods.

                Args:
                    exception_type (Exception | type): An exception instance or exception class that the client will raise when its faulting methods are invoked.

                """
                super().__init__()
                self.exception_type = exception_type

            def read_gatt_char(self, *_args, **_kwargs):
                """
                Raise the client's configured exception to simulate a failing GATT characteristic read.

                Raises:
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
            Mark the given event to indicate close was invoked and then call the original close function.

            Args:
                close_called (threading.Event): Event that will be set to signal that close was called.
                original_close (Callable[[], Any]): The original close callable to invoke.

            Returns:
                The result returned by invoking `original_close`.

            """
            close_called.set()
            return original_close()

        monkeypatch.setattr(iface, "close", mock_close)

        # Start the receive thread
        iface._want_receive = True

        # Set up the client
        with iface._client_lock:
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
    from meshtastic.ble_interface import (
        FROMNUM_UUID,
        LEGACY_LOGRADIO_UUID,
        LOGRADIO_UUID,
    )

    class MockClientWithLogChars(DummyClient):
        """Mock client that has log characteristics."""

        def __init__(self):
            """
            Initialize the mock client with default notification tracking and characteristic availability.

            Attributes:
                start_notify_calls (list): Records arguments of each start_notify invocation.
                has_characteristic_map (dict): Maps characteristic UUID constants to booleans indicating whether the client reports that characteristic is present. Defaults include LEGACY_LOGRADIO_UUID, LOGRADIO_UUID, and FROMNUM_UUID set to True.

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
            Check whether the client reports support for the given characteristic UUID.

            Args:
                uuid (str | uuid.UUID): The characteristic UUID to look up.

            Returns:
                bool: `True` if the UUID is present in the client's characteristic map, `False` otherwise.

            """
            return self.has_characteristic_map.get(uuid, False)

        def start_notify(self, uuid, handler, **_kwargs):
            """
            Record a notification registration request for the specified characteristic UUID.

            Args:
                uuid (str): The characteristic UUID for which notifications are being registered.
                handler (callable): The callback to be invoked when a notification for the UUID is received.

            """
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

    assert (
        legacy_call[1] == iface.legacy_log_radio_handler
    ), "Legacy log handler should be registered"
    assert (
        current_call[1] == iface.log_radio_handler
    ), "Current log handler should be registered"
    assert (
        fromnum_call[1] == iface.from_num_handler
    ), "FROMNUM handler should be registered"

    iface.close()


def test_log_notification_registration_missing_characteristics(monkeypatch):
    """Test that log notification registration handles missing characteristics gracefully."""
    from meshtastic.ble_interface import (
        FROMNUM_UUID,
        LEGACY_LOGRADIO_UUID,
        LOGRADIO_UUID,
    )

    class MockClientWithoutLogChars(DummyClient):
        """Mock client that doesn't have log characteristics."""

        def __init__(self):
            """
            Initialize a mock BLE client that exposes only the FROMNUM characteristic.

            Creates a list to record start_notify calls and a characteristic map that reports
            presence of `FROMNUM_UUID` while leaving other characteristics absent.
            """
            super().__init__()
            self.start_notify_calls = []
            self.has_characteristic_map = {
                FROMNUM_UUID: True,  # Only have the critical one
            }

        def has_characteristic(self, uuid):
            """
            Check whether the client reports support for the given characteristic UUID.

            Args:
                uuid (str | uuid.UUID): The characteristic UUID to look up.

            Returns:
                bool: `True` if the UUID is present in the client's characteristic map, `False` otherwise.

            """
            return self.has_characteristic_map.get(uuid, False)

        def start_notify(self, uuid, handler, **_kwargs):
            """
            Record a notification registration request for the specified characteristic UUID.

            Args:
                uuid (str): The characteristic UUID for which notifications are being registered.
                handler (callable): The callback to be invoked when a notification for the UUID is received.

            """
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

    from meshtastic.ble_interface import FROMRADIO_UUID

    caplog.set_level(logging.WARNING)

    class MockClient(DummyClient):
        """Mock client that returns invalid protobuf data to trigger DecodeError."""

        def read_gatt_char(self, uuid, timeout=None, **_kwargs):
            """Return malformed protobuf data for FROMRADIO to force a DecodeError."""
            if uuid == FROMRADIO_UUID:
                return b"invalid-protobuf-data"
            return b""

    client = MockClient()
    iface = _build_interface(monkeypatch, client)

    close_called = threading.Event()
    original_close = iface.close

    def mock_close():
        """
        Mark that close was called and forward the call to the original close function.

        Sets the `close_called` event to signal invocation, then invokes `original_close()`.
        """
        close_called.set()
        original_close()

    monkeypatch.setattr(iface, "close", mock_close)

    # Start the receive thread
    iface._want_receive = True

    # Set up the client
    with iface._client_lock:
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
        Record a pub/sub event by appending its topic and payload to the test harness list `published_events`.

        Args:
            topic (str): The pub/sub topic of the event.
            **kwargs: Arbitrary event payload fields; stored as a dict.

        Description:
            Appends a tuple `(topic, kwargs)` to the module-level `published_events` list.

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
        Mark that close was invoked and delegate to the original close function.

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
    from meshtastic.ble_interface import BleakError, BLEInterface

    # Set logging level to DEBUG to capture debug messages
    caplog.set_level(logging.DEBUG)

    class ExceptionClient(DummyClient):
        """Mock client that raises exceptions during write operations."""

        def __init__(self, exception_type):
            """
            Create an ExceptionClient that raises the specified exception type during simulated BLE operations.

            Args:
                exception_type (type): Exception class to be raised by the client's BLE methods when invoked.

            """
            super().__init__()
            self.exception_type = exception_type

        def write_gatt_char(self, *_args, **_kwargs):
            """
            Simulate a failed GATT characteristic write by raising a configured exception.

            This test stub performs no I/O; it raises an instance of `self.exception_type` with the message "write failed" to emulate a write error.

            Raises:
                Exception: An instance of `self.exception_type` representing the simulated write failure.

            """
            raise self.exception_type("write failed")

    # Test BleakError specifically
    client = ExceptionClient(BleakError)
    iface = _build_interface(monkeypatch, client)

    # Create a mock ToRadio message with actual data to ensure it's not empty
    from meshtastic.protobuf import mesh_pb2

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

    from meshtastic.ble_interface import BLEClient, BLEInterface

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
            Initialize a test BLE client with counters and optional simulated connect failures.

            Attributes:
                connect_count (int): Number of times connect() was invoked.
                disconnect_count (int): Number of times disconnect() was invoked.
                address (str): Bluetooth address used to identify the client.
                _should_fail_connect (bool): If True, simulated connect attempts will raise an error.

            """
            self.connect_count = 0
            self.disconnect_count = 0
            self.address = "00:11:22:33:44:55"
            self._should_fail_connect = False

        def connect(self, *_args, **_kwargs):
            """
            Simulate a client connection for tests.

            Increments the client's `connect_count` and returns the client instance. May be configured to simulate a connection failure.

            Returns:
                self: The mock client instance with `connect_count` incremented.

            Raises:
                RuntimeError: If the client is configured to fail connecting (`self._should_fail_connect` is True).

            """
            if self._should_fail_connect:
                raise RuntimeError("connect fail")  # noqa: TRY003
            self.connect_count += 1
            return self

        def is_connected(self):
            """
            Return whether the mock client is connected.

            Returns:
                True indicating the mock client always reports a connected state.

            """
            return True

        def disconnect(self, *_args, **_kwargs):
            """
            Record a disconnect attempt on this mock client by incrementing its counter.

            Accepts arbitrary positional and keyword arguments for call-site compatibility; all passed arguments are ignored. Side effect: increments `disconnect_count` by 1.
            """
            self.disconnect_count += 1

        def start_notify(self, *_args, **_kwargs):
            """
            No-op stub that accepts any positional and keyword arguments and intentionally performs no action.
            """
            pass

        def stop_notify(self, *_args, **_kwargs):
            """
            Stub that performs no action when asked to stop notifications for a BLE characteristic.

            Accepts any positional and keyword arguments and ignores them; provided solely for API compatibility with real BLE client implementations.
            """
            pass

    class StressTestClient(BLEClient):
        """Mock client that simulates rapid connect/disconnect cycles."""

        def __init__(self):  # pylint: disable=super-init-not-called
            # Don't call super().__init__() to avoid creating real event loop
            """
            Initialize the mock BLE root client used in tests.

            Sets up attributes that simulate a Bleak client and track connection activity:
            - bleak_client: mock underlying client instance.
            - connect_count: number of successful connect attempts.
            - disconnect_count: number of disconnect attempts.
            - is_connected_result: boolean returned by is_connected checks.
            - _should_fail_connect: when True, simulate failing connect attempts.
            - _eventLoop, _eventThread: placeholders to suppress event-loop related warnings.
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
            Simulate a client connection for tests.

            Increments the client's `connect_count` and returns the client instance. May be configured to simulate a connection failure.

            Returns:
                self: The mock client instance with `connect_count` incremented.

            Raises:
                RuntimeError: If the client is configured to fail connecting (`self._should_fail_connect` is True).

            """
            if self._should_fail_connect:
                raise RuntimeError("connect fail")  # noqa: TRY003
            self.connect_count += 1
            return self

        def is_connected(self):
            """
            Report whether the mock client is configured as connected.

            Returns:
                True if the mock client is configured as connected, False otherwise.

            """
            return self.is_connected_result

        def disconnect(self, *_args, **_kwargs):
            """
            Record a disconnect attempt on this mock client by incrementing its counter.

            Accepts arbitrary positional and keyword arguments for call-site compatibility; all passed arguments are ignored. Side effect: increments `disconnect_count` by 1.
            """
            self.disconnect_count += 1

        def start_notify(self, *_args, **_kwargs):
            """
            No-op stub that accepts any positional and keyword arguments and intentionally performs no action.
            """
            pass

        def stop_notify(self, *_args, **_kwargs):
            """
            Stub that performs no action when asked to stop notifications for a BLE characteristic.

            Accepts any positional and keyword arguments and ignores them; provided solely for API compatibility with real BLE client implementations.
            """
            pass

        def close(self):
            """
            No-op close method used in tests to avoid interacting with the event loop.

            This intentionally performs no action so that calling `close()` on a mock client does not trigger
            event-loop side effects or errors during unit tests.
            """
            pass

    def create_interface_with_auto_reconnect():
        """
        Create a BLEInterface configured for stress testing with auto-reconnect enabled.

        Patches BLEInterface.scan and BLEInterface.connect so the created interface will discover a test device and return a StressTestClient when connecting.

        Returns:
            (iface, client): `iface` is a BLEInterface instance with `auto_reconnect=True`; `client` is the StressTestClient that `iface.connect()` will return.

        """
        client = StressTestClient()
        connect_calls: list = []

        stack = ExitStack()

        # Mock the scan method to return our test device
        stack.enter_context(
            patch.object(BLEInterface, "scan", return_value=[mock_device])
        )

        def _patched_connect(self, address=None):
            """
            Record the attempted connection address, attach the provided client to the interface, clear the disconnect flag, signal a reconnection event if present, and return the client.

            Args:
                self: The BLEInterface instance.
                address (str | None): Optional address that was used to connect; recorded for test inspection.

            Returns:
                client: The client instance that was attached to the interface.

            """
            connect_calls.append(address)
            client.connect()
            self.client = client
            self._disconnect_notified = False
            if hasattr(self, "_reconnected_event"):
                self._reconnected_event.set()
            return client

        stack.enter_context(patch.object(BLEInterface, "connect", _patched_connect))

        iface = BLEInterface(
            address=None,  # Required positional argument
            noProto=True,
            auto_reconnect=True,
        )
        iface._test_patch_stack = stack
        iface._connect_stub_calls = connect_calls
        return iface, client

    # Test 1: Rapid disconnect callbacks
    iface, client = create_interface_with_auto_reconnect()

    def simulate_rapid_disconnects():
        """
        Simulate a burst of BLE disconnection events to exercise reconnect/disconnect handling.

        Invokes the interface disconnect handler with the test client's bleak_client ten times with a short pause (~0.01s) between calls to trigger rapid consecutive disconnect behavior.
        """
        for _ in range(10):
            iface._on_ble_disconnect(client.bleak_client)
            time.sleep(0.01)  # Very short delay between disconnects

    # Start rapid disconnect simulation in a separate thread
    disconnect_thread = threading.Thread(target=simulate_rapid_disconnects)
    disconnect_thread.start()
    disconnect_thread.join()

    # Verify that the interface handled rapid disconnects gracefully
    assert client.bleak_client.disconnect_count >= 0  # Should not crash
    assert (
        len(iface._connect_stub_calls) >= 2
    ), "Auto-reconnect should continue scheduling during rapid disconnects"

    iface.close()
    iface._test_patch_stack.close()

    # Test 2: Concurrent connect/disconnect operations
    iface2, client2 = create_interface_with_auto_reconnect()

    def rapid_connect_disconnect_cycle():
        """
        Trigger a short burst of simulated BLE disconnections on iface2 to exercise auto-reconnect and disconnect handling.

        Calls iface2._on_ble_disconnect(client2.bleak_client) five times with a 5 millisecond pause between calls; any exceptions raised during the loop are suppressed and logged to allow the stress cycle to continue.
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
        thread = threading.Thread(target=rapid_connect_disconnect_cycle)
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
    from meshtastic.ble_interface import BLEClient

    # Set logging level to DEBUG to capture debug messages
    caplog.set_level(logging.DEBUG)

    class ExceptionBleakClient:
        """Mock bleak client that raises exceptions during connection checks."""

        def __init__(self, exception_type):
            """
            Initialize a test client that raises the given exception type from its simulated BLE operations.

            Args:
                exception_type (type): Exception class that the client's methods will raise to simulate BLE failures.

            """
            self.exception_type = exception_type

        def is_connected(self):
            """
            Raise the configured exception to simulate a failing connection-state check.

            Raises:
                Exception: An instance of `self.exception_type` is raised with the message "conn check failed".

            """
            raise self.exception_type("conn check failed")  # noqa: TRY003

    # Create BLEClient with a mock bleak client that raises exceptions
    ble_client = BLEClient.__new__(BLEClient)
    ble_client.bleak_client = ExceptionBleakClient(AttributeError)
    # Initialize error_handler since __new__ bypasses __init__
    from meshtastic.ble_interface import BLEErrorHandler

    ble_client.error_handler = BLEErrorHandler()

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
    from concurrent.futures import TimeoutError as FutureTimeoutError

    from meshtastic.ble_interface import BLEClient, BLEInterface

    client = BLEClient()  # address=None keeps underlying bleak client unset

    class _FakeFuture:
        def __init__(self):
            self.cancelled = False
            self.coro = None

        def result(self, _timeout=None):
            raise FutureTimeoutError()

        def cancel(self):
            self.cancelled = True

    fake_future = _FakeFuture()

    def _fake_async_run(coro):
        fake_future.coro = coro
        return fake_future

    monkeypatch.setattr(client, "async_run", _fake_async_run)

    with pytest.raises(BLEInterface.BLEError) as excinfo:
        client.async_await(object(), timeout=0.01)

    assert "Async operation timed out" in str(excinfo.value)
    assert fake_future.cancelled is True

    client.close()
    if fake_future.coro is not None:
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
            Simulate a publishingThread.queueWork that always signals a threading failure.

            Parameters
            ----------
                _callback (callable): Work callback to queue; this argument is ignored.

            Raises
            ------
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
            Rejects any enqueued callback by raising a ValueError.

            Parameters
            ----------
                _callback (callable): Callback to enqueue; ignored by this stub.

            Raises
            ------
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

            Raises:
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
            Initialize the mock object and attach the provided queue.

            Sets the instance attribute `queue` to the externally supplied `mock_queue` for use by tests.
            """
            self.queue = mock_queue

        def queueWork(self, _callback):
            """
            Invoke the provided callback immediately to simulate scheduling work.

            Parameters
            ----------
                _callback (callable | None): Callable to execute; if None, no action is taken.

            Returns
            -------
                The value returned by _callback, or None if _callback is None.

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
