"""Common fixtures for BLE interface tests."""

import logging
import sys
import types
from types import SimpleNamespace
from typing import Optional

import pytest

# Import meshtastic modules for use in tests
import meshtastic.ble_interface as ble_mod
from meshtastic.ble_interface import (
    BLEInterface,
)


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
            # logging already imported at top

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
    # BLEInterface already imported at top as ble_mod.BLEInterface

    connect_calls: list = []

    def _stub_connect(
        _self: BLEInterface, _address: Optional[str] = None
    ) -> "DummyClient":
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

    def _stub_start_config(_self: BLEInterface) -> None:
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
