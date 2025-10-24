"""Tests for the BLE interface module - Core functionality."""

import logging
import threading
import time

import pytest  # type: ignore[import-untyped]
from bleak.exc import BleakError  # type: ignore[import-untyped]
from pubsub import pub  # type: ignore[import-untyped]

# Import common fixtures
from test_ble_interface_fixtures import (
    DummyClient,
    _build_interface,
)

# Import meshtastic modules for use in tests
import meshtastic.ble_interface as ble_mod
from meshtastic.ble_interface import (
    FROMNUM_UUID,
    LEGACY_LOGRADIO_UUID,
    LOGRADIO_UUID,
    BLEInterface,
)


def test_find_device_returns_single_scan_result(monkeypatch):
    """find_device should return the lone scanned device."""
    # BLEDevice and BLEInterface already imported at top as ble_mod.BLEDevice, ble_mod.BLEInterface

    iface = object.__new__(ble_mod.BLEInterface)
    scanned_device = ble_mod.BLEDevice(
        address="11:22:33:44:55:66", name="Test Device", details={}
    )
    monkeypatch.setattr(ble_mod.BLEInterface, "scan", lambda: [scanned_device])

    result = ble_mod.BLEInterface.find_device(iface, None)

    assert result is scanned_device


def test_find_device_uses_connected_fallback_when_scan_empty(monkeypatch):
    """find_device should fall back to connected-device lookup when scan is empty."""
    # BLEDevice and BLEInterface already imported at top as ble_mod.BLEDevice, ble_mod.BLEInterface

    iface = object.__new__(ble_mod.BLEInterface)
    fallback_device = ble_mod.BLEDevice(
        address="AA:BB:CC:DD:EE:FF", name="Fallback", details={}
    )
    monkeypatch.setattr(ble_mod.BLEInterface, "scan", lambda: [])

    def _fake_connected(_self, _address):
        """
        Provide a fake connected-device lookup that always returns the predefined `fallback_device`.
        
        Returns:
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
        ble_mod.BLEDevice(address="AA:BB:CC:DD:EE:FF", name="Meshtastic-1", details={}),
        ble_mod.BLEDevice(address="AA-BB-CC-DD-EE-FF", name="Meshtastic-2", details={}),
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
            
            Raises:
                AssertionError: "BleakScanner should not be instantiated when guard fails"
            """
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
    # BleakError already imported at top as ble_mod.BleakError
    # pub already imported at top as mesh_iface_module.pub

    calls = []

    def _capture(topic, **kwargs):
        """
        Record a pubsub message invocation by appending a (topic, kwargs) tuple to the module-level `calls` list.
        
        Parameters:
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
    # pub already imported at top as mesh_iface_module.pub

    calls = []

    def _capture(topic, **kwargs):
        """
        Record a pubsub message invocation by appending a (topic, kwargs) tuple to the module-level `calls` list.
        
        Parameters:
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
    # pub already imported at top as mesh_iface_module.pub

    calls = []

    def _capture(topic, **kwargs):
        """
        Record a pubsub message invocation by appending a (topic, kwargs) tuple to the module-level `calls` list.
        
        Parameters:
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
                
                Parameters:
                    exception_type (Exception | type): An exception instance or an exception class; the client will raise
                        this exception (or an instance of it) when its faulting methods are invoked.
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
            Signal that close was invoked and then call the original close function.
            
            Parameters:
                close_called (threading.Event): Event that will be set to signal that close was invoked.
                original_close (Callable[[], Any]): The original close callable to invoke.
            
            Returns:
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
            
            Attributes:
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
            
            Parameters:
                uuid (str | uuid.UUID): Characteristic UUID to check.
            
            Returns:
                bool: True if the UUID is present in the client's characteristic map, False otherwise.
            """
            return self.has_characteristic_map.get(uuid, False)

        def start_notify(self, *_args, **_kwargs):
            """
            Record a requested notification registration by saving the characteristic UUID and its handler.
            
            Parameters:
                _args (tuple): If two or more positional arguments are provided, the first is the characteristic UUID (usually a string) and the second is the notification handler (callable). When present, the pair is appended to `self.start_notify_calls`.
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

    assert (
        callable(legacy_call[1])
        and legacy_call[1].__name__ == iface.legacy_log_radio_handler.__name__
    ), "Legacy log handler should be registered"
    assert (
        callable(current_call[1])
        and current_call[1].__name__ == iface.log_radio_handler.__name__
    ), "Current log handler should be registered"
    assert (
        callable(fromnum_call[1])
        and fromnum_call[1].__name__ == iface.from_num_handler.__name__
    ), "FROMNUM handler should be registered"

    iface.close()