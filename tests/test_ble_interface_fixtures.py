"""Common fixtures for BLE interface tests."""

import logging
import sys
import types
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, Optional

import pytest

if TYPE_CHECKING:  # pragma: no cover - import only for typing
    from meshtastic.interfaces.ble.interface import BLEInterface


def _get_ble_module():
    """
    Dynamically import and return the meshtastic BLE interface module.

    Returns
    -------
        The imported meshtastic.interfaces.ble.interface module.
    """
    import meshtastic.interfaces.ble.interface as ble_mod

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
    monkeypatch : Any
        pytest monkeypatch fixture used to install modules into sys.modules.

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

    Returns
    -------
        module: The injected `pubsub` module. Its `pub` attribute is a SimpleNamespace with
        `subscribe` and `sendMessage` no-op callables and `AUTO_TOPIC` set to None.
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
    Install a mocked publishingThread module into sys.modules whose `queueWork` invokes callbacks immediately.

    The mock is placed under the module names "publishingThread" and "meshtastic.publishingThread" to provide synchronous, deterministic behavior for tests.

    Returns
    -------
        publishing_thread_module (module): The mocked publishingThread module inserted into sys.modules.
    """
    publishing_thread_module = types.ModuleType("publishingThread")

    def queueWork(callback):
        """
        Execute the provided callback immediately if one is supplied.

        Parameters
        ----------
        callback : Any
            Any]] Callable to invoke; if falsy (e.g., `None`), no action is taken.
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

    Returns
    -------
        module : Any
            The fake `tabulate` module object inserted into `sys.modules`.
    """
    tabulate_module = types.ModuleType("tabulate")
    tabulate_module.tabulate = lambda *_args, **_kwargs: ""

    monkeypatch.setitem(sys.modules, "tabulate", tabulate_module)
    return tabulate_module


@pytest.fixture(autouse=True)
def mock_bleak(monkeypatch):
    """
    Install a minimal fake `bleak` module into sys.modules for tests.

    The injected module exposes a stub `BleakClient` with async connect/disconnect/read/write/start_notify and a synchronous `is_connected`, a `BleakScanner.discover` coroutine that returns an empty list, and a lightweight `BLEDevice` type with `address` and `name` attributes.

    Returns
    -------
        module : Any
            The fake `bleak` module object inserted into `sys.modules`.
    """
    bleak_module = types.ModuleType("bleak")
    bleak_module.__version__ = "2.0.0"

    class _StubBleakClient:
        def __init__(self, address=None, **_kwargs):
            """
            Create a minimal test BLE client that stores an address and provides a lightweight services shim.

            Parameters
            ----------
            address : Any
                BLE device address associated with this client, or None.
            **_kwargs : dict
                Additional keyword arguments are accepted and ignored.

            Attributes
            ----------
            services: A SimpleNamespace with get_characteristic(specifier) that always returns None.
            """
            self.address = address
            self.services = SimpleNamespace(get_characteristic=lambda _specifier: None)

        async def connect(self, **_kwargs):
            """
            No-op asynchronous connect method that accepts and ignores any keyword arguments.

            Parameters
            ----------
            _kwargs : Any
                Keyword arguments passed to the method; all entries are ignored.
            """
            return None

        async def disconnect(self, **_kwargs):
            """
            Mock disconnect method that does nothing.

            Accept arbitrary keyword arguments which are ignored.
            """
            return None

        async def start_notify(self, **_kwargs):
            """
            Compatibility shim for a BLE client's start_notify that accepts and ignores any keyword arguments.
            """
            return None

        async def read_gatt_char(self, *_args, **_kwargs):
            """
            Return an empty value for any GATT characteristic read.

            Returns
            -------
                b'': empty bytes
            """
            return b""

        async def write_gatt_char(self, *_args, **_kwargs):
            """
            Accept any positional and keyword arguments and perform no action as a test stub for `write_gatt_char`.
            """
            return None

        def is_connected(self):
            """
            Report whether the dummy BLE client is connected.

            Returns
            -------
            bool: `True` if the client is connected, `False` otherwise. This dummy implementation always returns `False`.
            """
            return False

    async def _stub_discover(**_kwargs):
        """
        Simulate BLE device discovery.

        Parameters
        ----------
        **_kwargs : dict
            Ignored keyword arguments kept for API compatibility.

        Returns
        -------
        list: Empty list of discovered devices.
        """
        return []

    class _StubBLEDevice:
        def __init__(self, address=None, name=None):
            """
            Create a minimal BLE device representation with an optional address and name.

            Parameters
            ----------
            address : Any
                | None The device's BLE address, or None if unknown.
            name : Any
                | None The device's human-readable name, or None if unknown.
            """
            self.address = address
            self.name = name

    class _StubBleakScanner:
        def __init__(self):
            """
            Create a stub Bleak scanner whose backend reports no devices.

            Sets self._backend to a simple namespace with a get_devices() callable that returns an empty list.
            """
            self._backend = SimpleNamespace(get_devices=lambda: [])

        @staticmethod
        async def discover(**_kwargs):
            """
            Simulate BLE device discovery by returning an empty list of devices.

            Returns
            -------
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
    Create and register a minimal bleak.exc submodule providing BleakError and BleakDBusError.

    Returns
    -------
        bleak_exc_module (module): The created `bleak.exc` module with `BleakError` and `BleakDBusError` attributes; the module is attached to the provided `mock_bleak` and registered in `sys.modules`.
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
        Test double for a BLE client used in unit tests.

        Parameters
        ----------
        disconnect_exception : Any
            Exception to raise when disconnect() is called; pass None to disable raising.

        Attributes
        ----------
            disconnect_calls (int): Number of times disconnect() has been invoked.
            close_calls (int): Number of times close() has been invoked.
            address (str): Client address identifier, set to "dummy".
            disconnect_exception (Optional[Exception]): Stored exception raised by disconnect(), if any.
            services (types.SimpleNamespace): Provides get_characteristic(specifier) -> None for characteristic lookups.
            bleak_client (types.SimpleNamespace): Minimal mock of a bleak client with an address attribute used for identity checks.
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
        Indicate whether the mock client exposes a BLE characteristic matching the given specifier.

        Parameters
        ----------
        _specifier : Any
            Identifier of the characteristic to check (e.g., UUID string or characteristic object).

        Returns
        -------
            `False`, this mock never exposes characteristics.
        """
        return False

    def start_notify(self, *_args, **_kwargs):
        """
        Simulate subscribing to a BLE characteristic notification for tests.

        This no-op stub accepts and ignores any positional and keyword arguments.
        """
        return None

    def read_gatt_char(self, *_args, **_kwargs) -> bytes:
        """
        Return empty bytes for any GATT characteristic read.

        Returns
        -------
            b'': empty bytes
        """
        return b""

    def is_connected(self) -> bool:
        """
        Report the connection status of the mock BLE client (always connected for this test double).

        Returns
        -------
            True if the mock client is connected, False otherwise.
        """
        return True

    def disconnect(self, *_args, **_kwargs):
        """
        Record a disconnect invocation and raise a configured exception when present.

        Increments self.disconnect_calls and, if self.disconnect_exception is set, raises that exception.

        Raises
        ------
        Exception: The exception instance stored in `self.disconnect_exception`, if present.
        """
        self.disconnect_calls += 1
        if self.disconnect_exception:
            raise self.disconnect_exception

    def close(self):
        """
        Mark the mock client as closed by incrementing its close_calls counter.

        This records that close() was invoked so tests can assert how many times the client was closed.
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
        func : Any
            callable The callable to register; can also be used as a decorator.

        Returns
        -------
        callable: The same callable that was registered.
        """
        registered.append(func)
        return func

    def fake_unregister(func):
        """
        Remove all entries identical to the given callback from the module-level `registered` list.

        This removes every element that is the same object as `func` (compares by identity), not by equality.

        Parameters
        ----------
        func : Any
            callable The callback to remove from the registration list.
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
    monkeypatch : Any
        pytest monkeypatch fixture used to patch BLEInterface methods.
    client : Any
        Fake or mock BLE client instance to be returned by the patched `connect` method.

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
        Record a connect attempt and attach a preconfigured test client to the interface.

        Parameters
        ----------
        _self : Any
            BLEInterface-like instance being patched.
        _address : Any
            Address passed to connect; recorded in `connect_calls`.
        *args : tuple
            Additional positional arguments forwarded to the patched connect.
        **kwargs : dict
            Additional keyword arguments forwarded to the patched connect.

        Returns
        -------
        DummyClient: The preconfigured test client instance attached to the interface.

        Notes
        -----
            Side effects include appending `_address` to `connect_calls`, setting `self.client` to the test client, clearing `self._disconnect_notified`, and calling `self._reconnected_event.set()` if `_reconnected_event` exists on the interface.
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
        No-op replacement for BLEInterface._startConfig used in tests to suppress startup configuration side effects.
        """
        return None

    monkeypatch.setattr(BleInterfaceClass, "connect", _stub_connect)
    monkeypatch.setattr(BleInterfaceClass, "_startConfig", _stub_start_config)
    iface = BleInterfaceClass(address="dummy", noProto=True)
    iface._connect_stub_calls = connect_calls
    return iface
