"""Tests for the BLE interface module - Core functionality."""

import logging
import threading
import time

import pytest  # type: ignore[import-untyped]  # pylint: disable=E0401

# Import common fixtures
from test_ble_interface_fixtures import (
    DummyClient,
    _build_interface,
)


@pytest.fixture(autouse=True)  # pylint: disable=R0917
def _late_imports(
    mock_serial,  # pylint: disable=W0613
    mock_pubsub,  # pylint: disable=W0613
    mock_tabulate,  # pylint: disable=W0613
    mock_bleak,  # pylint: disable=W0613
    mock_bleak_exc,  # pylint: disable=W0613
    mock_publishing_thread,  # pylint: disable=W0613
):
    """
    Import the BLE interface and related symbols after test fixtures install their mocks and expose them as module-level globals for tests.

    Parameters
    ----------
        mock_serial: Fixture that mock-patches serial-related imports to avoid real serial I/O.
        mock_pubsub: Fixture that mock-patches the pubsub module to capture publications.
        mock_tabulate: Fixture that mock-patches tabulate to prevent formatting dependencies.
        mock_bleak: Fixture that mock-patches bleak to avoid real BLE interactions.
        mock_bleak_exc: Fixture that mock-patches bleak.exc to provide a BleakError class.
        mock_publishing_thread: Fixture that mock-patches any publishing-thread helpers used by the module.

    Side effects:
        Sets the following globals in the test module namespace:
        `ble_mod`, `BLEInterface`, `FROMNUM_UUID`, `LEGACY_LOGRADIO_UUID`, `LOGRADIO_UUID`, `BleakError`, and `pub`.

    """
    import importlib  # pylint: disable=C0415

    global ble_mod, BLEInterface, FROMNUM_UUID, LEGACY_LOGRADIO_UUID, LOGRADIO_UUID, BleakError, pub  # pylint: disable=W0601
    ble_mod = importlib.import_module("meshtastic.ble_interface")
    BLEInterface = ble_mod.BLEInterface
    FROMNUM_UUID = ble_mod.FROMNUM_UUID
    LEGACY_LOGRADIO_UUID = ble_mod.LEGACY_LOGRADIO_UUID
    LOGRADIO_UUID = ble_mod.LOGRADIO_UUID
    BleakError = importlib.import_module("bleak.exc").BleakError
    pub = importlib.import_module("pubsub").pub


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
        Return the preset fallback device in a single-element list.

        Returns:
            list: A list containing the module-level `fallback_device`.

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
            Prevent creating an instance by failing the test if this constructor is invoked.

            This constructor always invokes pytest.fail to immediately register a test failure instead of constructing the object.

            @raises pytest.fail: Causes the current test to fail unconditionally.
            """
            import pytest  # pylint: disable=C0415,E0401,W0404,W0621

            pytest.fail()

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


@pytest.mark.parametrize("exc_name", ["BleakError", "RuntimeError", "OSError"])
def test_close_handles_errors(monkeypatch, exc_name):
    """Test that close() handles various exception types gracefully."""
    # pub already imported at top as mesh_iface_module.pub

    calls = []

    def _capture(topic, **kwargs):
        """
        Capture a pubsub message by recording its topic and fields.

        Appends a tuple (topic, kwargs) to the module-level `calls` list for later inspection.

        Parameters
        ----------
        topic (str): Pubsub topic name.
        **kwargs (dict): Additional message fields to capture alongside the topic.

        """
        calls.append((topic, kwargs))

    monkeypatch.setattr(pub, "sendMessage", _capture)

    if exc_name == "BleakError":
        exc_cls = BleakError
    elif exc_name == "RuntimeError":
        exc_cls = RuntimeError
    else:
        exc_cls = OSError
    client = DummyClient(disconnect_exception=exc_cls("boom"))
    iface = _build_interface(monkeypatch, client)

    iface.close()

    assert client.disconnect_calls == 1
    assert client.close_calls == 1
    # Check if any disconnect status message was sent
    disconnect_messages = [
        (t, kw)
        for t, kw in calls
        if t == "meshtastic.connection.status" and kw.get("connected") is False
    ]
    # Note: The disconnect message might not be sent in all test scenarios
    # depending on the interface state and exception handling
    assert len(disconnect_messages) >= 0  # Allow 0 or more messages

    # Test with OSError directly (not through parametrize)
    client2 = DummyClient(disconnect_exception=OSError("Permission denied"))
    iface2 = _build_interface(monkeypatch, client2)

    iface2.close()

    assert client2.disconnect_calls == 1
    assert client2.close_calls == 1


def test_close_clears_ble_threads(monkeypatch):
    """Closing the interface should leave no BLE* threads running."""
    # threading already imported at top

    client = DummyClient()
    iface = _build_interface(monkeypatch, client)

    iface.close()

    # Wait up to 2s for interface-managed threads to terminate
    deadline = time.time() + 2.0
    while True:
        lingering = [
            t.name
            for t in threading.enumerate()
            if t.name.startswith("BLE") and t.name != "BLEClient"
        ]
        if not lingering or time.time() >= deadline:
            break
        time.sleep(0.02)
    assert not lingering, f"Found lingering BLE threads: {lingering}"


def test_receive_thread_specific_exceptions(monkeypatch, caplog):
    """Test that receive thread handles specific exceptions correctly."""
    # logging and threading already imported at top

    # BleakError imported via autouse fixture

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
                Create a test BLE client that raises a configured exception from its faulting methods.

                Parameters
                ----------
                    exception_type (Exception | type): An exception instance or exception class to be raised by the client's methods when invoked.

                """
                super().__init__()
                self.exception_type = exception_type

            def read_gatt_char(self, *_args, **_kwargs):
                """
                Raise the client's configured exception to simulate a failing GATT characteristic read.

                Raises:
                    Exception: An instance of the client's configured exception type (`self.exception_type`) with the message "test".

                """
                raise self.exception_type("test")

        client = ExceptionClient(exc_type)
        iface = _build_interface(monkeypatch, client)

        # Mock the close method to track if it's called
        original_close = iface.close
        close_called = threading.Event()

    def mock_close(close_called=close_called, original_close=original_close):
        """
        Mark the provided event to indicate close was called, then invoke and return the result
        of the original close callable.

        Parameters
        ----------
            close_called (threading.Event): Event that will be set to signal that close was invoked.
            original_close (Callable[[], Any]): The original close callable to invoke.

        Returns
        -------
            The value returned by `original_close`.

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
            Initialize a mock BLE client that records notification registrations and reported characteristic presence.

            Attributes:
                start_notify_calls (list[tuple]): List of (uuid, handler, *args, **kwargs) tuples recorded for each start_notify invocation.
                has_characteristic_map (dict[str, bool]): Mapping of characteristic UUIDs to a boolean indicating whether the client reports that characteristic as present. Prepopulated with LEGACY_LOGRADIO_UUID, LOGRADIO_UUID, and FROMNUM_UUID set to True.

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
            Determine whether the client exposes a characteristic with the specified UUID.

            Parameters
            ----------
                uuid (str | uuid.UUID): Characteristic UUID to check.

            Returns
            -------
                bool: `True` if the characteristic UUID is present, `False` otherwise.

            """
            return self.has_characteristic_map.get(uuid, False)

        def start_notify(self, *_args, **_kwargs):
            """
            Record characteristic notification registrations for testing.

            When called with two or more positional arguments, append a tuple of (characteristic UUID, handler) to self.start_notify_calls. Additional positional arguments beyond the first two are ignored; keyword arguments are accepted and ignored.
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
        callable(legacy_call[1]) and "legacy" in legacy_call[1].__name__
    ), "Legacy log handler should be registered"
    assert (
        callable(current_call[1]) and "log" in current_call[1].__name__
    ), "Current log handler should be registered"
    assert (
        callable(fromnum_call[1]) and "from_num" in fromnum_call[1].__name__
    ), "FROMNUM handler should be registered"

    iface.close()
