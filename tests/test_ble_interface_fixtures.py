"""Common fixtures for BLE interface tests."""

import logging
import sys
import types
from types import SimpleNamespace
from typing import Any, Optional, TYPE_CHECKING

import pytest

if TYPE_CHECKING:  # pragma: no cover - import only for typing
    from meshtastic.ble_interface import BLEInterface


def _get_ble_module():
    """
    Load the meshtastic.ble_interface module on demand.

    Returns:
        ble_module: The imported meshtastic.ble_interface module.
    """
    import meshtastic.ble_interface as ble_mod

    return ble_mod


@pytest.fixture(autouse=True)
def mock_serial(monkeypatch):
    """
    Inject a minimal fake `serial` package into sys.modules for tests.

    The injected package exposes:
    - serial.tools.list_ports.comports() -> empty list
    - SerialException and SerialTimeoutException mapped to Exception
    - entries for "serial", "serial.tools", and "serial.tools.list_ports" in sys.modules

    Parameters
    ----------
        monkeypatch: pytest monkeypatch fixture used to install modules into sys.modules.

    Returns
    -------
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
    Inject a fake `pubsub` module into sys.modules for tests.
    
    The injected module has a `pub` attribute (a SimpleNamespace) with no-op
    `subscribe` and `sendMessage` callables and `AUTO_TOPIC` set to `None`.
    
    Returns:
        module: The injected fake `pubsub` module.
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
    Install a mocked publishingThread module whose `queueWork` executes callbacks synchronously.
    
    The mock is inserted into sys.modules under "publishingThread" and "meshtastic.publishingThread" so tests receive a deterministic, immediate `queueWork` implementation.
    
    Returns:
        publishing_thread_module (module): The mocked publishingThread module inserted into sys.modules.
    """
    publishing_thread_module = types.ModuleType("publishingThread")

    def queueWork(callback):
        """
        Invoke `callback` immediately instead of scheduling it for later execution.

        Parameters
        ----------
            callback (Optional[Callable[[], Any]]): Callable to execute; if falsy (e.g., `None`), no action is taken.

        """
        if callback:
            callback()

    publishing_thread_module.queueWork = queueWork

    # Ensure fresh state
    for module_name in ("publishingThread", "meshtastic.publishingThread"):
        if module_name in sys.modules:
            del sys.modules[module_name]

    monkeypatch.setitem(sys.modules, "publishingThread", publishing_thread_module)
    monkeypatch.setitem(
        sys.modules,
        "meshtastic.publishingThread",
        publishing_thread_module,
    )
    return publishing_thread_module


@pytest.fixture(autouse=True)
def mock_tabulate(monkeypatch):
    """
    Install a minimal fake `tabulate` module into sys.modules for tests.

    The fake module exposes a `tabulate(*args, **kwargs)` callable that always returns an empty string.

    Returns:
        module: The fake `tabulate` module object inserted into `sys.modules`.
    """
    tabulate_module = types.ModuleType("tabulate")
    tabulate_module.tabulate = lambda *_args, **_kwargs: ""

    monkeypatch.setitem(sys.modules, "tabulate", tabulate_module)
    return tabulate_module


@pytest.fixture(autouse=True)
def mock_bleak(monkeypatch):
    """
    Create and install a minimal fake `bleak` module into sys.modules for use in tests.
    
    The injected module provides lightweight stand-ins for BleakClient, BleakScanner.discover, and BLEDevice so tests can exercise BLE-related code without real hardware.
    
    Returns:
        module: The fake `bleak` module object inserted into sys.modules.
    """
    bleak_module = types.ModuleType("bleak")
    bleak_module.__version__ = "1.1.1"

    class _StubBleakClient:
        def __init__(self, address=None, **_kwargs):
            """
            Create a minimal test BLE client that stores an address and exposes a lightweight services shim.

            Parameters:
                address (str | None): BLE device address associated with this client, or None.
                **_kwargs: Additional keyword arguments are accepted and ignored.

            Attributes:
                services (types.SimpleNamespace): Provides get_characteristic(specifier) which always returns None.
            """
            self.address = address
            self.services = SimpleNamespace(get_characteristic=lambda _specifier: None)

        async def connect(self, **_kwargs):
            """
            No-op connect method that accepts and ignores any keyword arguments.
            
            Parameters:
                _kwargs (dict): Keyword arguments passed to the method are accepted and ignored.
            """
            return None

        async def disconnect(self, **_kwargs):
            """
            No-op mock disconnect that performs no action.
            
            Accepts arbitrary keyword arguments which are ignored.
            """
            return None

        async def start_notify(self, **_kwargs):
            """
            Compatibility shim for a BLE client's start_notify that accepts arbitrary keyword arguments and ignores them.
            
            This no-op placeholder preserves the method signature for compatibility but performs no actions.
            
            Returns:
                None
            """
            return None

        async def read_gatt_char(self, *_args, **_kwargs):
            """
            Provide a stub GATT characteristic read that always returns an empty value.
            
            Returns:
                b'': Empty bytes.
            """
            return b""

        async def write_gatt_char(self, *_args, **_kwargs):
            """
            No-op stub for write_gatt_char that accepts any positional and keyword arguments.
            
            Ignores all arguments and performs no action; intended for use in tests where a write_gatt_char method is required.
            """
            return None

        def is_connected(self):
            """
            Report whether the dummy BLE client is connected.
            
            Returns:
                False: The dummy client never reports being connected.
            """
            return False

    async def _stub_discover(**_kwargs):
        """
        Simulate BLE device discovery.
        
        Accepts and ignores arbitrary keyword arguments to maintain API compatibility.
        
        Parameters:
            **_kwargs (dict): Ignored keyword arguments.
        
        Returns:
            list: An empty list of discovered devices.
        """
        return []

    class _StubBLEDevice:
        def __init__(self, address=None, name=None):
            """
            Create a minimal BLE device representation with optional address and name.
            
            Parameters:
                address (str | None): BLE device address, if known.
                name (str | None): Human-readable device name, if known.
            """
            self.address = address
            self.name = name

    class _StubBleakScanner:
        def __init__(self):
            """
            Create a stub BleakScanner that reports no devices.
            
            Creates a `_backend` attribute whose `get_devices()` method returns an empty list.
            """
            self._backend = SimpleNamespace(get_devices=lambda: [])

        @staticmethod
        async def discover(**_kwargs):
            """
            Provide an empty list of BLE devices to mirror BleakScanner.discover behavior in tests.

            Returns:
                list: Empty list representing no discovered BLE devices.
            """
            return await _stub_discover(**_kwargs)

    bleak_module.BleakClient = _StubBleakClient
    bleak_module.BleakScanner = _StubBleakScanner
    bleak_module.BLEDevice = _StubBLEDevice

    monkeypatch.setitem(sys.modules, "bleak", bleak_module)
    return bleak_module


@pytest.fixture(autouse=True)
def mock_bleak_exc(monkeypatch, mock_bleak):  # pylint: disable=redefined-outer-name
    """
    Create and register a minimal `bleak.exc` submodule exposing `BleakError` and `BleakDBusError`.
    
    Returns:
        bleak_exc_module (module): The created `bleak.exc` module with `BleakError` and `BleakDBusError` attributes.
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
        Create a DummyClient test double that simulates a BLE client for unit tests.
        
        Parameters:
            disconnect_exception (Optional[Exception]): Exception to raise when disconnect() is called; pass None to disable raising.
        """
        self.disconnect_calls = 0
        self.close_calls = 0
        self.address = "dummy"
        self.disconnect_exception = disconnect_exception
        self.services = SimpleNamespace(get_characteristic=lambda _specifier: None)
        # The bleak_client should be a separate object to correctly test identity checks
        self.bleak_client = SimpleNamespace(address=self.address)

    def has_characteristic(self, _specifier) -> bool:
        """
        Determine whether this mock client exposes a BLE characteristic matching the given specifier.
        
        This mock implementation never exposes characteristics and always returns `False`.
        
        Parameters:
            _specifier: Identifier of the characteristic to check (e.g., UUID string or characteristic object)
        
        Returns:
            `True` if a matching characteristic is present, `False` otherwise.
        """
        return False

    def start_notify(self, *_args, **_kwargs):
        """
        Simulate subscribing to a BLE characteristic notification in tests.
        
        This no-op stub accepts any positional and keyword arguments and performs no action.
        """
        return None

    def read_gatt_char(self, *_args, **_kwargs) -> bytes:
        """
        Return an empty bytes object for any GATT characteristic read.
        
        Returns:
            bytes: Empty bytes (`b""`).
        """
        return b""

    def is_connected(self) -> bool:
        """
        Return whether the mock BLE client is connected; this stub always reports connected.
        
        Returns:
            `true` if the mock client is connected, `false` otherwise.
        """
        return True

    def disconnect(self, *_args, **_kwargs):
        """
        Record that a disconnect was invoked and raise a preconfigured exception if present.

        Increments self.disconnect_calls. If self.disconnect_exception is set, raises that exception.

        Raises:
            Exception: The exception instance stored in `self.disconnect_exception`, if present.
        """
        self.disconnect_calls += 1
        if self.disconnect_exception:
            raise self.disconnect_exception

    def close(self):
        """
        Record that the client has been closed.

        Increments the instance's `close_calls` counter to track how many times `close()` has been invoked.
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
    Replace meshtastic.ble_interface.atexit.register and unregister with test stubs that record callbacks and invoke them after the test.

    This pytest fixture patches the BLE interface module's atexit.register/unregister so registered callables are appended to an internal list, yields control to the test, and then invokes all recorded callbacks during teardown (exceptions raised by callbacks are caught and logged at debug level). The extra fixture parameters are accepted only to enforce fixture ordering and are not otherwise used.
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
        Append a callable to the module-level `registered` list so it will be invoked later.

        Parameters
        ----------
        func : callable
            The callable to register; can also be used as a decorator.

        Returns
        -------
        callable
            The same callable that was registered.
        """
        registered.append(func)
        return func

    def fake_unregister(func):
        """
        Unregister a previously registered callback from the local `registered` list.
        
        Removes every entry from `registered` that is the same object as `func` (compares by identity).
        
        Parameters:
            func (callable): The callback to remove from the registered callbacks.
        """
        registered[:] = [f for f in registered if f is not func]

    ble_mod = _get_ble_module()
    monkeypatch.setattr(ble_mod.atexit, "register", fake_register, raising=True)
    monkeypatch.setattr(ble_mod.atexit, "unregister", fake_unregister, raising=True)
    yield
    # run any registered functions manually to avoid surprising global state
    for func in registered:
        try:
            func()
        except Exception as e:  # noqa: BLE001 - teardown should log but continue
            logging.debug(
                "atexit callback %r raised during teardown: %s: %s",
                func,
                type(e).__name__,
                e,
            )


def _build_interface(monkeypatch: Any, client: DummyClient) -> "BLEInterface":
    """
    Create a BLEInterface instance configured for tests with a stubbed `connect` that returns the supplied client and a no-op `_startConfig`.

    Parameters
    ----------
        monkeypatch: pytest monkeypatch fixture used to patch BLEInterface methods.
        client: Fake or mock BLE client instance to be returned by the patched `connect` method.

    Returns
    -------
        BLEInterface: A test-configured interface whose `connect` returns `client` and whose `_startConfig` performs no configuration.

    """
    ble_mod = _get_ble_module()
    BleInterfaceClass = ble_mod.BLEInterface
    connect_calls: list = []

    def _stub_connect(
        _self: Any, _address: Optional[str] = None, *args, **kwargs
    ) -> "DummyClient":
        """
        Record a connection attempt and attach a preconfigured test client to the interface.
        
        Parameters:
            _address (Optional[str]): Address passed to connect; recorded in the surrounding `connect_calls` list.
        
        Returns:
            DummyClient: The test client instance attached to the interface.
        
        Detailed behavior:
            Appends `_address` to `connect_calls`, sets `_self.client` to the provided test client, resets `_self._disconnect_notified` to False, and calls `_self._reconnected_event.set()` if `_reconnected_event` exists.
        """
        _ = (args, kwargs)
        connect_calls.append(_address)
        _self.client = client
        _self._disconnect_notified = False
        if hasattr(_self, "_reconnected_event"):
            _self._reconnected_event.set()
        return client

    def _stub_start_config(_self: "BLEInterface") -> None:
        """
        No-op replacement for BLEInterface._startConfig used in tests.
        
        Intentionally performs no startup configuration so tests run without real startup side effects.
        """
        return None

    monkeypatch.setattr(BleInterfaceClass, "connect", _stub_connect)
    monkeypatch.setattr(BleInterfaceClass, "_startConfig", _stub_start_config)
    iface = BleInterfaceClass(address="dummy", noProto=True)
    iface._connect_stub_calls = connect_calls
    return iface