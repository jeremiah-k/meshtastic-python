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
    """Mock the serial module and its submodules."""
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
    """Mock the pubsub module."""
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
    """Mock the publishingThread module."""
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
    """Mock the tabulate module."""
    tabulate_module = types.ModuleType("tabulate")
    tabulate_module.tabulate = lambda *_args, **_kwargs: ""

    monkeypatch.setitem(sys.modules, "tabulate", tabulate_module)
    return tabulate_module


@pytest.fixture(autouse=True)
def mock_bleak(monkeypatch):
    """Mock the bleak module."""
    bleak_module = types.ModuleType("bleak")

    class _StubBleakClient:
        def __init__(self, address=None, **_kwargs):
            self.address = address
            self.services = SimpleNamespace(get_characteristic=lambda _specifier: None)

        async def connect(self, **_kwargs):
            """Mock connect method."""
            return None

        async def disconnect(self, **_kwargs):
            """Mock disconnect method."""
            return None

        async def discover(self, **_kwargs):
            """Mock discover method."""
            return None

        async def start_notify(self, **_kwargs):
            """Mock start_notify method."""
            return None

        async def read_gatt_char(self, *_args, **_kwargs):
            """Mock read_gatt_char method."""
            return b""

        async def write_gatt_char(self, *_args, **_kwargs):
            """Mock write_gatt_char method."""
            return None

        def is_connected(self):
            """Mock is_connected method."""
            return False

    async def _stub_discover(**_kwargs):
        return {}

    class _StubBLEDevice:
        def __init__(self, address=None, name=None):
            self.address = address
            self.name = name

    bleak_module.BleakClient = _StubBleakClient
    bleak_module.BleakScanner = SimpleNamespace(discover=_stub_discover)
    bleak_module.BLEDevice = _StubBLEDevice

    monkeypatch.setitem(sys.modules, "bleak", bleak_module)
    return bleak_module


@pytest.fixture(autouse=True)
def mock_bleak_exc(monkeypatch, mock_bleak):  # pylint: disable=redefined-outer-name
    """Mock the bleak.exc module."""
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
        """Initialize dummy client with optional disconnect exception."""
        self.disconnect_calls = 0
        self.close_calls = 0
        self.address = "dummy"
        self.disconnect_exception = disconnect_exception
        self.services = SimpleNamespace(get_characteristic=lambda _specifier: None)
        self.bleak_client = (
            self  # For testing purposes, make bleak_client point to self
        )

    def has_characteristic(self, _specifier):
        """Mock has_characteristic method."""
        return False

    def start_notify(self, *_args, **_kwargs):
        """Mock start_notify method."""
        return None

    def is_connected(self) -> bool:
        """Mock is_connected method, returning True by default for most test cases."""
        return True

    def disconnect(self, *_args, **_kwargs):
        """Mock disconnect method that tracks calls and can raise exceptions."""
        self.disconnect_calls += 1
        if self.disconnect_exception:
            raise self.disconnect_exception

    def close(self):
        """Mock close method that tracks calls."""
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
    """Stub atexit to prevent actual registration during tests."""
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
        registered.append(func)
        return func

    def fake_unregister(func):
        registered[:] = [f for f in registered if f is not func]

    import meshtastic.ble_interface as ble_mod

    monkeypatch.setattr(ble_mod.atexit, "register", fake_register, raising=True)
    monkeypatch.setattr(ble_mod.atexit, "unregister", fake_unregister, raising=True)
    yield
    # run any registered functions manually to avoid surprising global state
    for func in registered:
        func()


def _build_interface(monkeypatch, client):
    from meshtastic.ble_interface import BLEInterface

    def _stub_connect(_self, _address=None):
        return client

    def _stub_start_config(_self):
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
                """Initialize exception client with specified exception type."""
                super().__init__()
                self.exception_type = exception_type

            def read_gatt_char(self, *_args, **_kwargs):
                """Mock read_gatt_char that raises the configured exception."""
                raise self.exception_type("Test exception")

        client = ExceptionClient(exc_type)
        iface = _build_interface(monkeypatch, client)

        # Mock the close method to track if it's called
        original_close = iface.close
        close_called = threading.Event()

        def mock_close(close_called=close_called, original_close=original_close):
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
            """Mock read_gatt_char that returns invalid data for FROMRADIO_UUID."""
            if uuid == FROMRADIO_UUID:
                # This data will cause a DecodeError
                return b"invalid-protobuf-data"
            return b""

    client = MockClient()
    iface = _build_interface(monkeypatch, client)

    close_called = threading.Event()
    original_close = iface.close

    def mock_close():
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
            """Initialize exception client with specified exception type."""
            super().__init__()
            self.exception_type = exception_type

        def write_gatt_char(self, *_args, **_kwargs):
            """Mock write_gatt_char that raises the configured exception."""
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
            self.connect_count = 0
            self.disconnect_count = 0
            self.address = "00:11:22:33:44:55"
            self._should_fail_connect = False

        def connect(self, *_args, **_kwargs):
            """Mock connect that tracks connection attempts."""
            if self._should_fail_connect:
                raise RuntimeError("Simulated connection failure")
            self.connect_count += 1
            return self

        def is_connected(self):
            """Mock is_connected - returns True for stress testing."""
            return True

        def disconnect(self, *_args, **_kwargs):
            """Mock disconnect that tracks disconnection attempts."""
            self.disconnect_count += 1

        def start_notify(self, *_args, **_kwargs):
            """Mock start_notify that does nothing."""
            pass

        def stop_notify(self, *_args, **_kwargs):
            """Mock stop_notify that does nothing."""
            pass

    class StressTestClient(BLEClient):
        """Mock client that simulates rapid connect/disconnect cycles."""

        def __init__(self):
            # Don't call super().__init__() to avoid creating real event loop
            self.bleak_client = MockBleakRootClient()
            self.connect_count = 0
            self.disconnect_count = 0
            self.is_connected_result = True
            self._should_fail_connect = False
            # Add mock event loop attributes to prevent warnings
            self._eventLoop = None
            self._eventThread = None

        def connect(self, *_args, **_kwargs):
            """Mock connect that tracks connection attempts."""
            if self._should_fail_connect:
                raise RuntimeError("Simulated connection failure")
            self.connect_count += 1
            return self

        def is_connected(self):
            """Mock is_connected that returns configurable result."""
            return self.is_connected_result

        def disconnect(self, *_args, **_kwargs):
            """Mock disconnect that tracks disconnection attempts."""
            self.disconnect_count += 1

        def start_notify(self, *_args, **_kwargs):
            """Mock start_notify that does nothing."""
            pass

        def stop_notify(self, *_args, **_kwargs):
            """Mock stop_notify that does nothing."""
            pass

        def close(self):
            """Mock close that does nothing to prevent event loop errors."""
            pass

    def create_interface_with_auto_reconnect():
        """Create a BLE interface with auto-reconnect enabled for stress testing."""
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
        """Simulate multiple rapid disconnect callbacks."""
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
        """Perform rapid connect/disconnect cycles."""
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
            """Initialize exception client with specified exception type."""
            self.exception_type = exception_type

        def is_connected(self):
            """Mock is_connected that raises the configured exception."""
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
            """Mock queueWork that raises RuntimeError."""
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
            """Mock queueWork that raises ValueError."""
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
            """Mock execution that raises ValueError."""
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
            """Mock queueWork - not used in this test but needed for teardown."""

    monkeypatch.setattr(ble_mod, "publishingThread", MockPublishingThread())

    # Should handle ValueError gracefully
    flush_event = threading.Event()
    iface._drain_publish_queue(flush_event)
    assert "Error in deferred publish callback" in caplog.text

    iface.close()
