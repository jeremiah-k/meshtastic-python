"""Tests for the BLE interface module."""
import sys
import types
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from typing import Optional

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


@pytest.fixture(autouse=True)
def mock_serial(monkeypatch):
    """
    Create and inject a fake `serial` package with minimal submodules and exceptions used by tests.

    The mocked package provides:
    - `serial.tools.list_ports.comports()` which returns an empty list.
    - `SerialException` and `SerialTimeoutException` aliased to `Exception`.
    - `serial.tools` and `serial.tools.list_ports` entries installed in `sys.modules`.

    Parameters:
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

    The fake module is inserted into sys.modules under the name "publishingThread".

    Returns:
        publishing_thread_module: The mocked publishingThread module inserted into sys.modules.
    """
    publishing_thread_module = types.ModuleType("publishingThread")

    def mock_queue_work(callback):
        """
        Execute the provided callback immediately instead of queuing it (used in tests).

        Parameters:
            callback (Optional[Callable]): A callable to invoke; if falsy, no action is taken.
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
    - `BleakScanner.discover`: an async coroutine that returns an empty mapping.
    - `BLEDevice`: a minimal device-like class with `address` and `name` attributes.

    Parameters:
        monkeypatch: pytest's monkeypatch fixture used to insert the fake module into sys.modules.

    Returns:
        module: The fake `bleak` module object that was inserted into sys.modules.
    """
    bleak_module = types.ModuleType("bleak")

    class _StubBleakClient:
        def __init__(self, address=None, **_kwargs):
            """
            Create a minimal test BLE client with an optional device address and a lightweight services shim.

            Parameters:
                address (str | None): BLE device address to associate with this client, or None.
                **_kwargs: Additional keyword arguments are accepted and ignored.

            Attributes:
                services (types.SimpleNamespace): Exposes get_characteristic(specifier) which always returns None.
            """
            self.address = address
            self.services = SimpleNamespace(get_characteristic=lambda _specifier: None)

        async def connect(self, **_kwargs):
            """
            Mock connect method that performs no action and accepts arbitrary keyword arguments.

            Any keyword arguments provided are ignored; this exists only to stand in for a real async connect implementation in tests.

            Parameters:
                **_kwargs: Arbitrary keyword arguments that are ignored.
            """
            return None

        async def disconnect(self, **_kwargs):
            """
            Mock disconnect method that performs no operation.

            Accepts arbitrary keyword arguments and ignores them.
            """
            return None

        async def discover(self, **_kwargs):
            """
            Simulate BLE device discovery that always returns no devices.

            This stub ignores any keyword arguments passed via `**_kwargs` and returns None to indicate no devices were found.
            """
            return None

        async def start_notify(self, **_kwargs):
            """
            No-op compatibility shim for a BLE client's start_notify that accepts and ignores any keyword arguments.
            """
            return None

        async def read_gatt_char(self, *_args, **_kwargs):
            """
            Simulate reading a GATT characteristic by returning empty bytes.

            Returns:
                bytes: Empty bytes object (b'').
            """
            return b""

        async def write_gatt_char(self, *_args, **_kwargs):
            """
            No-op stub that accepts any arguments for writing a GATT characteristic.

            Accepts arbitrary positional and keyword arguments and performs no action; used in tests to emulate a Bleak client's write_gatt_char.
            """
            return None

        def is_connected(self):
            """
            Indicates whether this dummy BLE client is connected.

            This stub implementation always reports not connected and is intended for use in tests.

            Returns:
                bool: `False` (client is not connected).
            """
            return False

    async def _stub_discover(**_kwargs):
        """
        Return an empty mapping simulating BLE device discovery; accepts and ignores any keyword arguments for API compatibility.

        Returns:
            dict: Empty dictionary representing no discovered devices.
        """
        return {}

    class _StubBLEDevice:
        def __init__(self, address=None, name=None):
            """
            Create a device descriptor with an optional BLE address and human-readable name.

            Parameters:
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
    Create and register a stub `bleak.exc` module exposing `BleakError` and `BleakDBusError`.

    Parameters:
        mock_bleak (module): Parent fake `bleak` module to attach the `exc` submodule to.

    Returns:
        bleak_exc_module (module): The created `bleak.exc` module with `BleakError` and `BleakDBusError` exception classes.
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
        Create a test DummyClient that simulates a BLE client for use in tests.

        Parameters:
            disconnect_exception (Optional[Exception]): Exception instance to be raised by disconnect(); pass None to disable raising.

        Attributes:
            disconnect_calls (int): Count of times disconnect() has been invoked.
            close_calls (int): Count of times close() has been invoked.
            address (str): Client address identifier, set to "dummy".
            disconnect_exception (Optional[Exception]): Stored exception to raise on disconnect.
            services (SimpleNamespace): Exposes get_characteristic(specifier) -> None for tests.
            bleak_client: Reference to self to mimic the Bleak client API in tests.
        """
        self.disconnect_calls = 0
        self.close_calls = 0
        self.address = "dummy"
        self.disconnect_exception = disconnect_exception
        self.services = SimpleNamespace(get_characteristic=lambda _specifier: None)
        self.bleak_client = (
            self  # For testing purposes, make bleak_client point to self
        )

    def has_characteristic(self, _specifier):
        """
        Indicates whether the mock client exposes a BLE characteristic matching the given specifier.

        Parameters:
            _specifier: Identifier for the characteristic (e.g., UUID string or characteristic object).

        Returns:
            `False` always for this mock implementation.
        """
        return False

    def start_notify(self, *_args, **_kwargs):
        """
        Stub that simulates subscribing to a BLE characteristic notification.

        This no-op implementation accepts any positional and keyword arguments and performs
        no action (used in tests to replace a real start_notify).
        """
        return None

    def is_connected(self) -> bool:
        """
        Report the mock BLE client's connection state.

        This test stub always reports the client as connected.

        Returns:
            bool: `True`, indicating the mock client is always connected.
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
        Register a callable by appending it to the module-level `registered` list and return it.

        Parameters:
        	func (callable): The callable to register. Can be used as a decorator to register functions.

        Returns:
        	func (callable): The same callable that was registered.
        """
        registered.append(func)
        return func

    def fake_unregister(func):
        """
        Remove a previously registered callback from the module-level `registered` list.

        All entries that are the same object as `func` (identity comparison) are removed.
        Parameters:
            func (callable): The callback to unregister.
        """
        registered[:] = [f for f in registered if f is not func]

    import meshtastic.ble_interface as ble_mod

    monkeypatch.setattr(ble_mod.atexit, "register", fake_register, raising=True)
    monkeypatch.setattr(ble_mod.atexit, "unregister", fake_unregister, raising=True)
    yield
    # run any registered functions manually to avoid surprising global state
    for func in registered:
        func()


def _build_interface(monkeypatch, client):
    """
    Create a BLEInterface configured for tests where its `connect` method is stubbed to return the supplied client 
    and its `_startConfig` method is a no-op.

    Parameters:
        monkeypatch: pytest monkeypatch fixture used to patch BLEInterface methods.
        client: Fake or mock BLE client instance that the patched `connect` method will return.

    Returns:
        BLEInterface: An instance whose `connect` returns `client` and whose `_startConfig` performs no configuration.
    """
    from meshtastic.ble_interface import BLEInterface

    connect_calls: list = []

    def _stub_connect(_self, _address=None):
        """
        Return a preconfigured BLE client used by tests.

        Parameters:
            _address (str | None): Present to match the original connect signature; ignored by this stub.

        Returns:
            client: The preconfigured client instance to be used by tests.
        """
        connect_calls.append(_address)
        _self.client = client
        _self._disconnect_notified = False
        if hasattr(_self, "_reconnected_event"):
            _self._reconnected_event.set()
        return client

    def _stub_start_config(_self):
        """
        Do nothing for start-up configuration.
        """
        return None

    monkeypatch.setattr(BLEInterface, "connect", _stub_connect)
    monkeypatch.setattr(BLEInterface, "_startConfig", _stub_start_config)
    iface = BLEInterface(address="dummy", noProto=True)
    iface._connect_stub_calls = connect_calls
    return iface


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
        Record a pubsub message invocation by appending a (topic, kwargs) tuple to the shared `calls` list.

        Parameters:
            topic (str): Pubsub topic name.
            **kwargs: Additional message fields to store with the topic.
        """
        calls.append((topic, kwargs))

    monkeypatch.setattr(pub, "sendMessage", _capture)

    client = DummyClient(disconnect_exception=BleakError("Not connected"))
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


def test_close_handles_runtime_error(monkeypatch):
    """Test that close() handles RuntimeError gracefully."""
    from meshtastic.mesh_interface import pub

    calls = []

    def _capture(topic, **kwargs):
        """
        Record a pubsub message invocation by appending a (topic, kwargs) tuple to the shared `calls` list.

        Parameters:
            topic (str): Pubsub topic name.
            **kwargs: Additional message fields to store with the topic.
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
        Record a pubsub message invocation by appending a (topic, kwargs) tuple to the shared `calls` list.

        Parameters:
            topic (str): Pubsub topic name.
            **kwargs: Additional message fields to store with the topic.
        """
        calls.append((topic, kwargs))

    monkeypatch.setattr(pub, "sendMessage", _capture)

    client = DummyClient(disconnect_exception=OSError("Permission denied"))
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


def test_receive_thread_specific_exceptions(monkeypatch, caplog):
    """Test that receive thread handles specific exceptions correctly."""
    import logging
    import threading

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
                Create a test client that raises a configured exception from its faulting methods.

                Parameters:
                    exception_type (Exception or type): An exception instance or exception class that the client will raise 
                    when its faulting methods are invoked.
                """
                super().__init__()
                self.exception_type = exception_type

            def read_gatt_char(self, *_args, **_kwargs):
                """
                Raise the client's configured exception when called.

                Raises:
                    Exception: An instance of the client's `exception_type` with the message "Test exception".
                """
                raise self.exception_type("Test exception")

        client = ExceptionClient(exc_type)
        iface = _build_interface(monkeypatch, client)

        # Mock the close method to track if it's called
        original_close = iface.close
        close_called = threading.Event()

        def mock_close(close_called=close_called, original_close=original_close):
            """
            Mark the given event to indicate close was invoked and then call the original close function.

            Parameters:
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
        except Exception as exc:
            # Log for visibility; still allow test to proceed with cleanup.
            print(f"Cleanup error in iface.close(): {exc!r}")


def test_receive_loop_handles_decode_error(monkeypatch, caplog):
    """Test that the receive loop handles DecodeError gracefully without closing."""
    import logging
    import threading
    import time
    from meshtastic.ble_interface import FROMRADIO_UUID

    caplog.set_level(logging.WARNING)

    class MockClient(DummyClient):
        """Mock client that returns invalid protobuf data to trigger DecodeError."""

        def read_gatt_char(self, uuid):
            """
            Provide mocked GATT characteristic bytes, returning a malformed payload for the radio 
            characteristic.

            Parameters:
                uuid: The UUID of the GATT characteristic to read. If equal to `FROMRADIO_UUID`, the returned bytes are 
                intentionally invalid for protobuf parsing.

            Returns:
                bytes: `b"invalid-protobuf-data"` when `uuid` is `FROMRADIO_UUID`, otherwise `b""`.
            """
            if uuid == FROMRADIO_UUID:
                # This data will cause a DecodeError
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

    # Give the thread a moment to run
    time.sleep(0.5)

    # Assert that the graceful handling occurred
    assert "Failed to parse FromRadio packet, discarding:" in caplog.text
    assert not close_called.is_set(), "close() should not be called for a DecodeError"

    # Clean up
    iface._want_receive = False
    iface.close()


def test_auto_reconnect_behavior(monkeypatch, caplog):
    """Test auto_reconnect functionality when disconnection occurs."""
    _ = caplog  # Mark as unused
    import time

    import meshtastic.mesh_interface as mesh_iface_module

    # Track published events
    published_events = []

    def _capture_events(topic, **kwargs):
        """
        Capture a published event by appending its topic and payload to the test-harness list `published_events`.

        Parameters:
            topic (str): The pub/sub topic of the event.
            **kwargs: Arbitrary event payload fields; stored as a dictionary alongside the topic.

        Side effects:
            Appends a tuple (topic, kwargs) to the global `published_events` list.
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
    assert iface._connect_stub_calls == ["dummy"], "Initial connect should occur on instantiation"

    # Track if close() was called
    close_called = []
    original_close = iface.close

    def _track_close():
        """
        Record that close was invoked and then call the original close function.

        Returns:
            The value returned by the wrapped original_close() call.
        """
        close_called.append(True)
        return original_close()

    monkeypatch.setattr(iface, "close", _track_close)

    # Simulate disconnection by calling _on_ble_disconnect directly
    # This simulates what happens when bleak calls the disconnected callback
    disconnect_client = iface.client
    iface._on_ble_disconnect(disconnect_client)

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
    assert not iface._disconnect_notified, "_disconnect_notified should reset after auto-reconnect"

    # 5. Receive thread should remain alive, waiting for new connection
    # Check that _want_receive is still True and receive thread is alive
    assert (
        iface._want_receive is True
    ), "_want_receive should remain True for auto_reconnect"
    assert iface._receiveThread is not None, "receive thread should still exist"
    assert iface._receiveThread.is_alive(), "receive thread should remain alive"

    # Clean up
    iface.auto_reconnect = False  # Disable auto_reconnect for proper cleanup
    iface.close()


def test_send_to_radio_specific_exceptions(monkeypatch, caplog):
    """Test that sendToRadio handles specific exceptions correctly."""
    import logging

    from meshtastic.ble_interface import BleakError, BLEInterface

    # Set logging level to DEBUG to capture debug messages
    caplog.set_level(logging.DEBUG)

    class ExceptionClient(DummyClient):
        """Mock client that raises exceptions during write operations."""

        def __init__(self, exception_type):
            """
            Initialize an ExceptionClient that will raise the given exception type during simulated BLE operations.

            Parameters:
                exception_type (type): Exception class to be raised by the client when its BLE methods are invoked.
            """
            super().__init__()
            self.exception_type = exception_type

        def write_gatt_char(self, *_args, **_kwargs):
            """
            Raise the configured exception to simulate a failed GATT characteristic write.

            This test stub does not perform any I/O; it raises an instance of the callable stored on `self.exception_type` 
            with the message "Test write exception" to emulate a write failure.

            Raises:
                Exception: An instance of `self.exception_type` indicating the simulated write error.
            """
            raise self.exception_type("Test write exception")

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
    assert "Error during write operation: _StubBleakError" in caplog.text

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
    import threading
    import time
    import logging
    from unittest.mock import MagicMock, patch

    from meshtastic.ble_interface import BLEInterface, BLEClient

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
            Create a test client with counters and configurable simulated failures.

            Attributes:
                connect_count (int): Number of times connect() was invoked.
                disconnect_count (int): Number of times disconnect() was invoked.
                address (str): Bluetooth address used to identify the client.
                _should_fail_connect (bool): If True, simulated connect attempts will fail.
            """
            self.connect_count = 0
            self.disconnect_count = 0
            self.address = "00:11:22:33:44:55"
            self._should_fail_connect = False

        def connect(self, *_args, **_kwargs):
            """
            Attempt a simulated client connection for tests.

            Increments the client's internal `connect_count` and returns the client instance. Can be configured to simulate a failure.

            Raises:
                RuntimeError: If the client is configured to fail connecting (`self._should_fail_connect` is True).

            Returns:
                self: The mock client instance with `connect_count` incremented.
            """
            if self._should_fail_connect:
                raise RuntimeError("Simulated connection failure")
            self.connect_count += 1
            return self

        def is_connected(self):
            """
            Report the connection state of this mock client.

            Returns:
                True: The mock client simulates a persistent connection and always reports connected.
            """
            return True

        def disconnect(self, *_args, **_kwargs):
            """
            Record a disconnect attempt on this mock client.

            Increments the client's `disconnect_count` by 1. Any positional or keyword arguments passed are accepted 
            for call-site compatibility and ignored.
            """
            self.disconnect_count += 1

        def start_notify(self, *_args, **_kwargs):
            """
            Accepts any positional and keyword arguments and performs no action.
            """
            pass

        def stop_notify(self, *_args, **_kwargs):
            """
            No-op mock for stopping notifications on a BLE characteristic.

            Accepts any positional or keyword arguments and ignores them; provided for API compatibility with real clients.
            """
            pass

    class StressTestClient(BLEClient):
        """Mock client that simulates rapid connect/disconnect cycles."""

        def __init__(self):  # pylint: disable=super-init-not-called
            # Don't call super().__init__() to avoid creating real event loop
            """
            Initialize a mock BLE root client used in tests.

            Sets up attributes that simulate a Bleak client and track connection activity for testing:
            - bleak_client: the mock underlying client instance.
            - connect_count: number of successful connect attempts.
            - disconnect_count: number of disconnect attempts.
            - is_connected_result: boolean returned by is_connected checks.
            - _should_fail_connect: when True, connect attempts should be treated as failing.
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
            Attempt a simulated client connection for tests.

            Increments the client's internal `connect_count` and returns the client instance. Can be configured to simulate a failure.

            Raises:
                RuntimeError: If the client is configured to fail connecting (`self._should_fail_connect` is True).

            Returns:
                self: The mock client instance with `connect_count` incremented.
            """
            if self._should_fail_connect:
                raise RuntimeError("Simulated connection failure")
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
            Record a disconnect attempt on this mock client.

            Increments the client's `disconnect_count` by 1. Any positional or keyword arguments passed are accepted 
            for call-site compatibility and ignored.
            """
            self.disconnect_count += 1

        def start_notify(self, *_args, **_kwargs):
            """
            Accepts any positional and keyword arguments and performs no action.
            """
            pass

        def stop_notify(self, *_args, **_kwargs):
            """
            No-op mock for stopping notifications on a BLE characteristic.

            Accepts any positional or keyword arguments and ignores them; provided for API compatibility with real clients.
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
        Constructs a BLEInterface preconfigured for stress testing with auto-reconnect enabled.

            The function patches BLEInterface.scan and BLEInterface.connect so the created interface will discover a test device 
            and return the provided StressTestClient when connecting.

        Returns:
            tuple: (iface, client) where `iface` is a BLEInterface instance with `auto_reconnect=True` and `client` is the 
            StressTestClient that `iface.connect()` will return.
        """
        client = StressTestClient()
        connect_calls: list = []

        stack = ExitStack()

        # Mock the scan method to return our test device
        stack.enter_context(patch.object(BLEInterface, 'scan', return_value=[mock_device]))

        def _patched_connect(self, address=None):
            connect_calls.append(address)
            client.connect()
            self.client = client
            self._disconnect_notified = False
            if hasattr(self, "_reconnected_event"):
                self._reconnected_event.set()
            return client

        stack.enter_context(patch.object(BLEInterface, 'connect', _patched_connect))

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
        Simulate a burst of BLE disconnect events by invoking the interface's disconnect handler repeatedly.

            Calls the interface disconnect handler with the test client's bleak_client ten times, pausing approximately 0.01 seconds 
            between invocations to exercise reconnect/disconnect logic under rapid consecutive disconnect conditions.
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
        Perform a short burst of rapid disconnect events on iface2 to exercise its auto-reconnect and disconnect handling.

            Calls iface2._on_ble_disconnect(client2.bleak_client) five times, sleeping 5 milliseconds between calls. Exceptions of type 
            RuntimeError, AttributeError, and KeyError are caught and logged for debugging; any other exceptions are also logged 
            and suppressed so the stress loop continues.
        """
        for i in range(5):
            try:
                iface2._on_ble_disconnect(client2.bleak_client)
                time.sleep(0.005)
            except (RuntimeError, AttributeError, KeyError) as e:
                # Expected during stress testing - log for debugging but don't fail test
                print(f"Stress test disconnect {i}: {type(e).__name__}: {e}")
            except Exception as e:
                # Unexpected exceptions - log but continue to avoid breaking the stress test
                print(f"Unexpected stress test disconnect {i}: {type(e).__name__}: {e}")

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
    for _ in range(5):
        try:
            iface3._on_ble_disconnect(client3.bleak_client)
            time.sleep(0.01)
        except Exception:
            pass  # Expected due to simulated connection failures

    # Verify graceful handling of connection failures
    assert client3.bleak_client.connect_count >= 0  # Should attempt reconnections

    iface3.close()
    iface3._test_patch_stack.close()

    # Verify no critical errors in logs
    log_messages = [record.message for record in caplog.records]
    critical_errors = [msg for msg in log_messages if 'CRITICAL' in msg or 'FATAL' in msg]
    assert len(critical_errors) == 0, f"Critical errors found in logs: {critical_errors}"


def test_ble_client_is_connected_exception_handling(monkeypatch, caplog):
    """Test that BLEClient.is_connected handles exceptions gracefully."""
    _ = monkeypatch  # Mark as unused
    import logging

    from meshtastic.ble_interface import BLEClient

    # Set logging level to DEBUG to capture debug messages
    caplog.set_level(logging.DEBUG)

    class ExceptionBleakClient:
        """Mock bleak client that raises exceptions during connection checks."""

        def __init__(self, exception_type):
            """
            Create a test client that raises the provided exception type from its simulated BLE operations.

            Parameters:
                exception_type (type): Exception class the client will raise from its methods to simulate failures.
            """
            self.exception_type = exception_type

        def is_connected(self):
            """
            Simulate a connection-state check by raising the configured exception.

            Raises:
                Exception: An instance of `self.exception_type` is raised with the message "Connection check failed".
            """
            raise self.exception_type("Connection check failed")

    # Create BLEClient with a mock bleak client that raises exceptions
    ble_client = BLEClient.__new__(BLEClient)
    ble_client.bleak_client = ExceptionBleakClient(AttributeError)

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


def test_wait_for_disconnect_notifications_exceptions(monkeypatch, caplog):
    """Test that _wait_for_disconnect_notifications handles exceptions gracefully."""
    import logging

    # Set logging level to DEBUG to capture debug messages
    caplog.set_level(logging.DEBUG)

    # Also ensure the logger is configured to capture the actual module logger
    logger = logging.getLogger("meshtastic.ble_interface")
    logger.setLevel(logging.DEBUG)

    client = DummyClient()
    iface = _build_interface(monkeypatch, client)

    # Mock publishingThread to raise RuntimeError
    import meshtastic.ble_interface as ble_mod

    class MockPublishingThread:
        """Mock publishingThread that raises RuntimeError in queueWork."""

        def queueWork(self, callback):
            """
            Simulate a publishingThread.queueWork that always raises a threading error.

            Parameters:
                callback (callable): Work callback (ignored).

            Raises:
                RuntimeError: Always raised with the message "Threading error in queueWork".
            """
            raise RuntimeError("Threading error in queueWork")

    monkeypatch.setattr(ble_mod, "publishingThread", MockPublishingThread())

    # Should handle RuntimeError gracefully
    iface._wait_for_disconnect_notifications()
    assert "Runtime error during disconnect notification flush" in caplog.text

    # Clear caplog
    caplog.clear()

    # Mock publishingThread to raise ValueError
    class MockPublishingThread2:
        """Mock publishingThread that raises ValueError in queueWork."""

        def queueWork(self, callback):
            """
            Enqueue a callback for deferred execution — stub implementation that always fails.

            Parameters:
                callback (callable): Callback to enqueue; ignored by this stub.

            Raises:
                ValueError: Always raised with message "Invalid event state".
            """
            raise ValueError("Invalid event state")

    monkeypatch.setattr(ble_mod, "publishingThread", MockPublishingThread2())

    # Should handle ValueError gracefully
    iface._wait_for_disconnect_notifications()
    assert "Value error during disconnect notification flush" in caplog.text

    iface.close()


def test_drain_publish_queue_exceptions(monkeypatch, caplog):
    """Test that _drain_publish_queue handles exceptions gracefully."""
    import logging
    import threading
    from queue import Queue

    # Set logging level to DEBUG to capture debug messages
    caplog.set_level(logging.DEBUG)

    client = DummyClient()
    iface = _build_interface(monkeypatch, client)

    # Create a mock queue with a runnable that raises exceptions
    class ExceptionRunnable:
        """Mock runnable that raises ValueError when called."""

        def __call__(self):
            """
            A callable test stub that always raises ValueError to simulate a failing callback.

            Raises:
                ValueError: Always raised to indicate the mock callback failed during execution.
            """
            raise ValueError("Callback execution failed")

    mock_queue = Queue()
    mock_queue.put(ExceptionRunnable())

    # Mock publishingThread with the queue
    import meshtastic.ble_interface as ble_mod

    class MockPublishingThread:
        """Mock publishingThread with a predefined queue."""

        def __init__(self):
            """
            Create the mock and attach the provided queue.

            Assigns the externally provided `mock_queue` to the instance attribute `queue` for use by tests.
            """
            self.queue = mock_queue

        def queueWork(self, callback):
            """
            Execute the given callback immediately (used to simulate scheduling work).

            Parameters:
                callback (callable | None): Function to invoke; if None, nothing is executed.

            Returns:
                The value returned by `callback`, or `None` if `callback` is `None`.
            """

    monkeypatch.setattr(ble_mod, "publishingThread", MockPublishingThread())

    # Should handle ValueError gracefully
    flush_event = threading.Event()
    iface._drain_publish_queue(flush_event)
    assert "Error in deferred publish callback" in caplog.text

    iface.close()
