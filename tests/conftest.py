"""
Shared pytest fixtures for BLE interface tests.
"""

import logging
import sys
import types
from types import SimpleNamespace
from typing import Optional

import pytest  # type: ignore[import-untyped]


@pytest.fixture(autouse=True)
def mock_serial(monkeypatch):
    """
    Inject a minimal fake `serial` package into sys.modules for tests.

    The injected module exposes:
    - serial.tools.list_ports.comports() -> [] (always returns an empty list)
    - SerialException and SerialTimeoutException mapped to built-in Exception
    - entries registered for "serial", "serial.tools", and "serial.tools.list_ports" in sys.modules

    Returns:
        types.ModuleType: The mocked `serial` module object.

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
        module: The injected fake `pubsub` module whose `pub` attribute is a SimpleNamespace with `subscribe`
        and `sendMessage` no-op callables and `AUTO_TOPIC` set to None.

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
    Create and register a mocked publishingThread module whose queueWork executes a provided callback immediately.

    The mock is inserted into sys.modules as "publishingThread" to provide a synchronous, deterministic queueWork implementation for tests.

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
    if "publishingThread" in sys.modules:
        del sys.modules["publishingThread"]

    monkeypatch.setitem(sys.modules, "publishingThread", publishing_thread_module)
    return publishing_thread_module


@pytest.fixture(autouse=True)
def mock_tabulate(monkeypatch):
    """
    Install a minimal fake `tabulate` module into sys.modules for tests.

    Returns:
        The fake `tabulate` module object inserted into `sys.modules`.

    """
    tabulate_module = types.ModuleType("tabulate")
    tabulate_module.tabulate = lambda *_args, **_kwargs: ""

    monkeypatch.setitem(sys.modules, "tabulate", tabulate_module)
    return tabulate_module


@pytest.fixture(autouse=True)
def mock_bleak(monkeypatch):
    """
    Inject a minimal fake 'bleak' module into sys.modules for use in tests.

    The injected module provides:
    - BleakClient: a stub client implementing the basic BleakClient API (async connect/disconnect/read/write/start_notify and sync is_connected).
    - BleakScanner.discover: an async coroutine that returns an empty list.
    - BLEDevice: a lightweight device object with `address` and `name` attributes.
    - __version__: set to "1.1.1".

    Returns:
        module: The fake `bleak` module object inserted into sys.modules.

    """
    bleak_module = types.ModuleType("bleak")
    bleak_module.__version__ = "1.1.1"

    class _StubBleakClient:
        def __init__(self, address=None, **_kwargs):
            """
            Initialize a minimal test BLE client with an optional address and a lightweight services shim.

            Parameters
            ----------
                address (str | None): BLE device address associated with this client, or None.
                **_kwargs: Additional keyword arguments are accepted and ignored.

            Attributes
            ----------
                services (types.SimpleNamespace): Exposes `get_characteristic(specifier)` which always returns `None`.

            """
            self.address = address
            self.services = SimpleNamespace(get_characteristic=lambda _specifier: None)

        async def connect(self, **_kwargs):
            """
            Accept any keyword arguments and perform no connection as a test stub.

            This no-op connect ignores all provided keyword arguments and always returns None.
            """
            return None

        async def disconnect(self, **_kwargs):
            """
            Perform a no-op disconnection for the test double.

            Any keyword arguments provided are accepted and ignored.

            Returns:
                None: No value is returned.

            """
            return None

        async def start_notify(self, *_args, **_kwargs):
            """
            Accepts arbitrary arguments and does nothing.

            Parameters
            ----------
                _args: Positional arguments provided for compatibility; they are ignored.
                _kwargs (dict): Keyword arguments provided for compatibility; they are ignored.

            """
            return None

        async def read_gatt_char(self, *_args, **_kwargs):
            """
            Simulate reading a GATT characteristic and return no data.

            Returns:
                bytes: Empty bytes (b'').

            """
            return b""

        async def write_gatt_char(self, *_args, **_kwargs):
            """
            Accepts any arguments and performs no operation, emulating BleakClient.write_gatt_char for tests.

            The stub ignores all positional and keyword arguments and produces no side effects.

            Returns:
                None

            """
            return None

        def is_connected(self):
            """
            Report whether the dummy BLE client is connected.

            Returns:
                False — the dummy client is never connected.

            """
            return False

    async def _stub_discover(**_kwargs):
        """
        Simulate BLE device discovery.

        Accepts and ignores arbitrary keyword arguments for API compatibility.

        Parameters
        ----------
            **_kwargs (dict): Ignored keyword arguments.

        Returns
        -------
            list: Empty list of discovered BLE devices.

        """
        return []

    class _StubBLEDevice:
        def __init__(self, address=None, name=None, **_kwargs):
            """
            Create a minimal BLE device representation.

            Parameters
            ----------
                address (str | None): BLE device address, if known.
                name (str | None): Human-readable device name, if known.
                **_kwargs: Additional keyword arguments are accepted and ignored.

            """
            self.address = address
            self.name = name
            # preserve commonly used metadata if provided
            self.details = _kwargs.get("details", {})

    class _StubBleakScanner:
        def __init__(self, *_args, **_kwargs):
            # accept arbitrary args/kwargs for parity with real BleakScanner
            pass

        @staticmethod
        async def discover(**_kwargs):
            return []

        async def start(self):
            pass

        async def stop(self):
            pass

        def register_detection_callback(self, *_args, **_kwargs):
            return None

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
        bleak_exc_module (module): The created `bleak.exc` module providing `BleakError` and `BleakDBusError`.

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