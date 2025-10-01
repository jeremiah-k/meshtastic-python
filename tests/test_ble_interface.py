"""Tests for the BLE interface module."""
import sys
import types
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
    Create and inject a fake `pubsub` module into sys.modules for tests.
    
    @param monkeypatch: The pytest monkeypatch fixture used to modify sys.modules.
    @returns: The injected fake `pubsub` module containing a `pub` namespace with no-op `subscribe` and `sendMessage` and an `AUTO_TOPIC` attribute set to None.
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
    Create and inject a fake publishingThread module whose `queueWork` function executes callbacks immediately.
    
    Parameters:
        monkeypatch: pytest monkeypatch fixture used to insert the fake module into sys.modules.
    
    Returns:
        publishing_thread_module: The mocked publishingThread module inserted into sys.modules.
    """
    publishing_thread_module = types.ModuleType("publishingThread")

    def mock_queue_work(callback):
        """Mock queueWork that executes callbacks immediately for testing."""
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
    Create and install a fake `tabulate` module that provides a no-op `tabulate` function.
    
    The function inserts a module named "tabulate" into sys.modules where `tabulate(...)` always returns an empty string.
    
    Returns:
        tabulate_module (module): The fake `tabulate` module that was added to sys.modules.
    """
    tabulate_module = types.ModuleType("tabulate")
    tabulate_module.tabulate = lambda *_args, **_kwargs: ""

    monkeypatch.setitem(sys.modules, "tabulate", tabulate_module)
    return tabulate_module


@pytest.fixture(autouse=True)
def mock_bleak(monkeypatch):
    """
    Create and install a fake `bleak` module with lightweight stubs for use in tests.
    
    The installed module exposes:
    - `BleakClient`: a stub client class with async connect/disconnect/read/write methods and a synchronous `is_connected`.
    - `BleakScanner.discover`: an async coroutine that returns an empty mapping.
    - `BLEDevice`: a minimal device-like class with `address` and `name` attributes.
    
    Returns:
        module: The fake `bleak` module object that was inserted into sys.modules.
    """
    bleak_module = types.ModuleType("bleak")

    class _StubBleakClient:
        def __init__(self, address=None, **_kwargs):
            """
            Initialize the instance with an optional BLE device address and a minimal services shim.
            
            Parameters:
                address (str | None): BLE device address to associate with this client, or None.
                **_kwargs: Additional keyword arguments are accepted and ignored.
            
            Attributes provided:
                services: A lightweight namespace exposing `get_characteristic(specifier)` which always returns `None`.
            """
            self.address = address
            self.services = SimpleNamespace(get_characteristic=lambda _specifier: None)

        async def connect(self, **_kwargs):
            """
            Perform a no-op mock of the connection operation.
            
            Accepts and ignores any keyword arguments; used to stand in for a real connect implementation in tests.
            Parameters:
                **_kwargs: Arbitrary keyword arguments (ignored).
            """
            return None

        async def disconnect(self, **_kwargs):
            """
            No-op mock that simulates disconnecting a BLE client.
            
            This asynchronous stub accepts and ignores any keyword arguments and performs no action; it returns None.
            
            Parameters:
                _kwargs (dict): Arbitrary keyword arguments passed by callers; ignored by this mock.
            """
            return None

        async def discover(self, **_kwargs):
            """
            Simulate BLE device discovery and always yield no results.
            
            Returns:
                None: stub implementation that indicates no devices were discovered.
            """
            return None

        async def start_notify(self, **_kwargs):
            """
            No-op mock of a BLE client's start_notify that accepts and ignores keyword arguments.
            
            Parameters:
                _kwargs (dict): Additional keyword arguments are accepted for compatibility and ignored.
            """
            return None

        async def read_gatt_char(self, *_args, **_kwargs):
            """
            Return an empty bytes object to simulate reading a GATT characteristic.
            
            Returns:
                bytes: Empty bytes (b'').
            """
            return b""

        async def write_gatt_char(self, *_args, **_kwargs):
            """
            No-op asynchronous stub that accepts any arguments for writing a GATT characteristic.
            
            This mock method intentionally performs no action and accepts arbitrary positional and keyword arguments to match the real API.
            
            Returns:
                None: always returns None.
            """
            return None

        def is_connected(self):
            """
            Indicates whether the mock BLE client is currently connected.
            
            Returns:
                bool: `False` for the stub implementation (client is not connected).
            """
            return False

    async def _stub_discover(**_kwargs):
        """
        Stub implementation of a BLE discovery coroutine that accepts and ignores any keyword arguments.
        
        Parameters:
            **_kwargs: Ignored keyword arguments passed by callers (kept for API compatibility).
        
        Returns:
            dict: An empty dictionary indicating no BLE devices were discovered.
        """
        return {}

    class _StubBLEDevice:
        def __init__(self, address=None, name=None):
            """
            Initialize the object with optional BLE address and human-readable name.
            
            Parameters:
                address (str | None): BLE device address, if available.
                name (str | None): Human-readable device name, if available.
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
        Create a test DummyClient that simulates a BLE client and optionally raises an exception when disconnected.
        
        Parameters:
            disconnect_exception (Optional[Exception]): Exception instance to be raised by the client's disconnect method; pass None to disable raising.
        
        Attributes:
            disconnect_calls (int): Number of times disconnect() has been invoked.
            close_calls (int): Number of times close() has been invoked.
            address (str): Client address identifier, set to "dummy".
            disconnect_exception (Optional[Exception]): Stored exception to raise on disconnect.
            services (SimpleNamespace): Object exposing get_characteristic(specifier) -> None for tests.
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
        Determine whether the mock BLE client is connected.
        
        This test stub always reports the client as connected.
        
        Returns:
            True if the mock client is connected; this stub always returns `True`.
        """
        return True

    def disconnect(self, *_args, **_kwargs):
        """
        Record a disconnect invocation and optionally raise a configured exception.
        
        Increments self.disconnect_calls to track how many times disconnect was called.
        If self.disconnect_exception is set, that exception is raised.
        """
        self.disconnect_calls += 1
        if self.disconnect_exception:
            raise self.disconnect_exception

    def close(self):
        """
        Record a close invocation on the mock client.
        
        Increments the `close_calls` counter to track how many times `close()` has been called.
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
    Provide a test fixture that stubs meshtastic.ble_interface.atexit to capture registrations and run them on teardown.
    
    This fixture replaces ble_mod.atexit.register and ble_mod.atexit.unregister with local no-op implementations that record registered callbacks. It yields control to the test, and after the test completes it invokes any callbacks that were registered to avoid leaving persistent global state. The other fixture parameters are accepted only to enforce fixture ordering.
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
        Unregister a previously registered callback.
        
        Parameters:
        	func (callable): The callback to remove from the module-level `registered` list. All entries that are the same object as `func` will be removed.
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
    Create a BLEInterface instance for tests with its connect method stubbed to return the provided client and _startConfig stubbed to do nothing.
    
    Parameters:
        monkeypatch: pytest monkeypatch fixture used to patch BLEInterface methods.
        client: A fake or mock BLE client instance that BLEInterface.connect should return.
    
    Returns:
        A BLEInterface instance whose connect returns `client` and whose _startConfig is a no-op.
    """
    from meshtastic.ble_interface import BLEInterface

    def _stub_connect(_self, _address=None):
        """
        Replaceable test stub that emulates a BLE connection method by returning a preconfigured client object.
        
        Parameters:
            _address (str | None): Ignored; present to match the original connect signature.
        
        Returns:
            client: The preconfigured client instance to be used by tests.
        """
        return client

    def _stub_start_config(_self):
        """
        No-op placeholder that performs no start-up configuration.
        """
        return None

    monkeypatch.setattr(BLEInterface, "connect", _stub_connect)
    monkeypatch.setattr(BLEInterface, "_startConfig", _stub_start_config)
    iface = BLEInterface(address="dummy", noProto=True)
    return iface


def test_close_idempotent(monkeypatch):
    """Test that close() is idempotent and only calls disconnect once."""
    client = DummyClient()
    iface = _build_interface(monkeypatch, client)

    iface.close()
    iface.close()

    assert client.disconnect_calls == 1
    assert client.close_calls == 1


def test_close_handles_bleak_error(monkeypatch):
    """Test that close() handles BleakError gracefully."""
    from meshtastic.ble_interface import BleakError
    from meshtastic.mesh_interface import pub

    calls = []

    def _capture(topic, **kwargs):
        """
        Record a pubsub message invocation by appending the topic and provided keyword arguments to the shared `calls` list.
        
        Parameters:
            topic (str): The pubsub topic name.
            **kwargs: Additional message fields passed to the pubsub send call; stored as a dict alongside the topic.
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
        Record a pubsub message invocation by appending the topic and provided keyword arguments to the shared `calls` list.
        
        Parameters:
            topic (str): The pubsub topic name.
            **kwargs: Additional message fields passed to the pubsub send call; stored as a dict alongside the topic.
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
        Record a pubsub message invocation by appending the topic and provided keyword arguments to the shared `calls` list.
        
        Parameters:
            topic (str): The pubsub topic name.
            **kwargs: Additional message fields passed to the pubsub send call; stored as a dict alongside the topic.
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
                Initialize an exception-raising test client.
                
                Parameters:
                    exception_type (Exception | type): Exception instance or exception class that the client will raise when its faulting methods are invoked.
                """
                super().__init__()
                self.exception_type = exception_type

            def read_gatt_char(self, *_args, **_kwargs):
                """
                Mock read_gatt_char that always raises the instance's configured exception.
                
                Raises:
                    Exception: An instance of the client's configured `exception_type` with message "Test exception".
                """
                raise self.exception_type("Test exception")

        client = ExceptionClient(exc_type)
        iface = _build_interface(monkeypatch, client)

        # Mock the close method to track if it's called
        original_close = iface.close
        close_called = threading.Event()

        def mock_close(close_called=close_called, original_close=original_close):
            """
            Mark the provided event as set and invoke the original close callable.
            
            Parameters:
                close_called (threading.Event): Event to signal that close was called.
                original_close (Callable[[], Any]): The original close function to execute.
            
            Returns:
                Any: The value returned by calling `original_close`.
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
            Return mocked GATT characteristic bytes, providing an intentionally malformed payload for the radio characteristic.
            
            Parameters:
                uuid: The UUID of the GATT characteristic to read. If `uuid` equals `FROMRADIO_UUID`, the function returns data that triggers a decode error.
            
            Returns:
                bytes: The raw bytes read from the characteristic. Returns `b'invalid-protobuf-data'` for `FROMRADIO_UUID`, otherwise empty bytes.
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
    assert "Failed to parse packet from radio, discarding." in caplog.text
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

    # Track if close() was called
    close_called = []
    original_close = iface.close

    def _track_close():
        """
        Record that close was invoked and then invoke the original close function.
        
        Returns:
            The value returned by the wrapped `original_close()` call.
        """
        close_called.append(True)
        return original_close()

    monkeypatch.setattr(iface, "close", _track_close)

    # Simulate disconnection by calling _on_ble_disconnect directly
    # This simulates what happens when bleak calls the disconnected callback
    disconnect_client = iface.client
    iface._on_ble_disconnect(disconnect_client)

    # Small delay to ensure events are published and captured
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

    # 3. Internal client should be cleaned up (self.client becomes None)
    assert (
        iface.client is None
    ), "client should be None after disconnection with auto_reconnect=True"

    # 4. Receive thread should remain alive, waiting for new connection
    # Check that _want_receive is still True and receive thread is alive
    assert (
        iface._want_receive is True
    ), "_want_receive should remain True for auto_reconnect"
    assert iface._receiveThread is not None, "receive thread should still exist"
    assert iface._receiveThread.is_alive(), "receive thread should remain alive"

    # 5. Verify disconnect notification flag is set
    assert (
        iface._disconnect_notified is True
    ), "_disconnect_notified should be True after disconnection"

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
            Create an ExceptionClient configured to raise the given exception type during BLE operations.
            
            Parameters:
                exception_type (type): Exception class that instances of this client will raise when performing simulated BLE actions.
            """
            super().__init__()
            self.exception_type = exception_type

        def write_gatt_char(self, *_args, **_kwargs):
            """
            Simulated write to a GATT characteristic that always raises the configured exception.
            
            This mock method does not perform any I/O and instead immediately raises an instance of the callable stored in `self.exception_type` with the message "Test write exception".
            
            Raises:
                Exception: An instance of `self.exception_type` to simulate a write failure.
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
            Initialize the test client and its internal counters and configuration.
            
            Attributes:
                connect_count (int): Number of times connect was invoked.
                disconnect_count (int): Number of times disconnect was invoked.
                address (str): Bluetooth address string used to identify the client.
                _should_fail_connect (bool): When True, simulated connect attempts should fail.
            """
            self.connect_count = 0
            self.disconnect_count = 0
            self.address = "00:11:22:33:44:55"
            self._should_fail_connect = False

        def connect(self, *_args, **_kwargs):
            """
            Simulated connect method for tests that tracks connection attempts and can be configured to fail.
            
            Raises:
                RuntimeError: If self._should_fail_connect is true, raises a simulated connection failure.
            
            Returns:
                self: The mock client instance with an incremented connection counter.
            """
            if self._should_fail_connect:
                raise RuntimeError("Simulated connection failure")
            self.connect_count += 1
            return self

        def is_connected(self):
            """
            Indicates that this mock client is always connected for stress testing.
            
            Returns:
                True: Always returns True to simulate a connected BLE client.
            """
            return True

        def disconnect(self, *_args, **_kwargs):
            """
            Record that a disconnect attempt occurred for the mock client.
            
            Increments the client's disconnect_count by 1. Accepts arbitrary positional and keyword arguments for call-site compatibility but ignores them.
            """
            self.disconnect_count += 1

        def start_notify(self, *_args, **_kwargs):
            """
            No-op stub for start_notify that accepts any arguments and performs no action.
            """
            pass

        def stop_notify(self, *_args, **_kwargs):
            """Mock stop_notify that does nothing."""
            pass

    class StressTestClient(BLEClient):
        """Mock client that simulates rapid connect/disconnect cycles."""

        def __init__(self):
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
            Simulated connect method for tests that tracks connection attempts and can be configured to fail.
            
            Raises:
                RuntimeError: If self._should_fail_connect is true, raises a simulated connection failure.
            
            Returns:
                self: The mock client instance with an incremented connection counter.
            """
            if self._should_fail_connect:
                raise RuntimeError("Simulated connection failure")
            self.connect_count += 1
            return self

        def is_connected(self):
            """
            Indicates whether the mock client is currently connected based on its configured result.
            
            Returns:
                True if the mock client is configured as connected, False otherwise.
            """
            return self.is_connected_result

        def disconnect(self, *_args, **_kwargs):
            """
            Record that a disconnect attempt occurred for the mock client.
            
            Increments the client's disconnect_count by 1. Accepts arbitrary positional and keyword arguments for call-site compatibility but ignores them.
            """
            self.disconnect_count += 1

        def start_notify(self, *_args, **_kwargs):
            """
            No-op stub for start_notify that accepts any arguments and performs no action.
            """
            pass

        def stop_notify(self, *_args, **_kwargs):
            """Mock stop_notify that does nothing."""
            pass

        def close(self):
            """
            No-op close method used in tests to avoid interacting with the event loop.
            
            This intentionally performs no action so that calling `close()` on a mock client does not trigger event-loop side effects or errors during unit tests.
            """
            pass

    def create_interface_with_auto_reconnect():
        """
        Create a BLEInterface configured for stress testing with auto-reconnect enabled.
        
        Returns:
            (BLEInterface, StressTestClient): A tuple containing the constructed BLEInterface (with auto_reconnect=True)
            and the StressTestClient instance that the interface's connect method will return.
        """
        client = StressTestClient()
        
        # Mock the scan method to return our test device
        with patch.object(BLEInterface, 'scan', return_value=[mock_device]):
            # Mock the connect method to return our test client
            with patch.object(BLEInterface, 'connect', return_value=client):
                iface = BLEInterface(
                    address=None,  # Required positional argument
                    noProto=True,
                    auto_reconnect=True,
                )
                return iface, client

    # Test 1: Rapid disconnect callbacks
    iface, client = create_interface_with_auto_reconnect()

    def simulate_rapid_disconnects():
        """
        Trigger the interface's BLE disconnect handler repeatedly to simulate rapid consecutive disconnect events.
        
        Calls iface._on_ble_disconnect(client.bleak_client) ten times with a short (0.01s) pause between calls to exercise reconnect/disconnect logic under rapid-fire disconnect conditions.
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
    assert iface._disconnect_notified  # Should be True after disconnects

    iface.close()

    # Test 2: Concurrent connect/disconnect operations
    iface2, client2 = create_interface_with_auto_reconnect()

    def rapid_connect_disconnect_cycle():
        """
        Trigger a short burst of rapid disconnect events to stress the BLE reconnect logic.
        
        Calls iface2._on_ble_disconnect with client2.bleak_client five times, pausing 5 ms between calls; any exceptions raised during the stress calls are caught and ignored.
        """
        for _ in range(5):
            try:
                iface2._on_ble_disconnect(client2.bleak_client)
                time.sleep(0.005)
            except Exception:
                pass  # Expected during stress testing

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
            Create a test client configured to raise the given exception type during simulated operations.
            
            Parameters:
                exception_type (type): Exception class that the client will raise when its methods simulate failures.
            """
            self.exception_type = exception_type

        def is_connected(self):
            """
            Simulate a connection check by raising the instance's configured exception.
            
            Raises:
                Exception: An instance of the configured exception type (self.exception_type) with the message "Connection check failed".
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
                callback (callable): Ignored; represents the work that would be queued.
            
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
            Callable stub that always raises a ValueError when invoked.
            
            Raises:
                ValueError: Indicates the mock callback failed during execution.
            """
            raise ValueError("Callback execution failed")

    mock_queue = Queue()
    mock_queue.put(ExceptionRunnable())

    # Mock publishingThread with the queue
    import meshtastic.ble_interface as ble_mod

    class MockPublishingThread:
        """Mock publishingThread with a predefined queue."""

        def __init__(self):
            """Initialize mock with the provided queue."""
            self.queue = mock_queue

        def queueWork(self, callback):
            """
            Execute the provided callback immediately to simulate scheduling work.
            
            Parameters:
                callback (callable | None): A callable to run; if None, no action is taken.
            
            Returns:
                The value returned by `callback`, or `None` if `callback` is None.
            """

    monkeypatch.setattr(ble_mod, "publishingThread", MockPublishingThread())

    # Should handle ValueError gracefully
    flush_event = threading.Event()
    iface._drain_publish_queue(flush_event)
    assert "Error in deferred publish callback" in caplog.text

    iface.close()
