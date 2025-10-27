"""
Shared pytest fixtures for BLE interface tests.
"""

import sys
import types
from types import SimpleNamespace

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
    Provide a fake `pubsub` module in sys.modules for tests.

    Returns:
        The injected fake `pubsub` module. Its `pub` attribute is a SimpleNamespace with `subscribe`
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
        Run the provided callback immediately if one is supplied.

        Parameters
        ----------
                callback (Optional[Callable[[], Any]]): Callable to execute; if `None` or falsy, nothing is invoked.

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
    Install a minimal fake `tabulate` module into `sys.modules` for use in tests.

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
    Injects a minimal fake `bleak` module into sys.modules for use in tests.

    The injected module exposes a stubbed BleakClient (no-op async methods and is_connected always False), a BleakScanner with async discover returning an empty list, a lightweight BLEDevice type, and __version__ = "1.1.1".

    Returns:
        module: The fake `bleak` module object inserted into sys.modules.

    """
    bleak_module = types.ModuleType("bleak")
    bleak_module.__version__ = "1.1.1"

    class _StubBleakClient:
        def __init__(self, address=None, **_kwargs):
            """
            Create a minimal test BLE client bound to an optional address with a lightweight services shim.

            Parameters
            ----------
                address (str | None): BLE device address associated with this client, or None.
                **_kwargs: Additional keyword arguments are accepted and ignored.

            Attributes
            ----------
                services (types.SimpleNamespace): Provides `get_characteristic(specifier)` which always returns `None`.

            """
            self.address = address
            self.services = SimpleNamespace(get_characteristic=lambda _specifier: None)

        async def connect(self, **_kwargs):
            """
            No-op connect method for the stub Bleak client that ignores any keyword arguments.

            Parameters
            ----------
                _kwargs (dict): Ignored keyword arguments.

            Returns
            -------
                None

            """
            return None

        async def disconnect(self, **_kwargs):
            """
            No-op disconnect for the test double.

            Accepts and ignores any keyword arguments. Returns None.
            """
            return None

        async def start_notify(self, *_args, **_kwargs):
            """
            No-op placeholder for starting notifications on a BLE characteristic; accepts any arguments for API compatibility.

            Returns:
                None: This stub does not perform any action.

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
            No-op stub that accepts any arguments to emulate BleakClient.write_gatt_char in tests.

            This function ignores all positional and keyword arguments and performs no side effects.

            Returns:
                None

            """
            return None

        def is_connected(self):
            """
            Report whether the dummy BLE client is connected.

            Returns:
                False (this stubbed client never reports being connected).

            """
            return False

    async def _stub_discover(**_kwargs):
        """
        Simulate BLE device discovery by returning an empty list.

        Accepts arbitrary keyword arguments for API compatibility; all are ignored.

        Returns:
            list: Empty list of discovered BLE devices.

        """
        return []

    class _StubBLEDevice:
        def __init__(self, address=None, name=None, **_kwargs):
            """
            Initialize a minimal BLE device representation.

            Parameters
            ----------
                address (str | None): BLE device address, if known.
                name (str | None): Human-readable device name, if known.
                **_kwargs: Additional keyword arguments are accepted and ignored; if a `details` mapping
                    is provided it will be preserved on the instance as `self.details`.

            """
            self.address = address
            self.name = name
            # preserve commonly used metadata if provided
            self.details = _kwargs.get("details", {})

    class _StubBleakScanner:
        def __init__(self, *_args, **_kwargs):
            # accept arbitrary args/kwargs for parity with real BleakScanner
            """
            Initialize a stub BleakScanner; accepts any positional and keyword arguments for API compatibility.

            Parameters
            ----------
                _args: Positional arguments passed through for compatibility with the real BleakScanner (ignored).
                _kwargs: Keyword arguments passed through for compatibility with the real BleakScanner (ignored).

            """
            pass

        @staticmethod
        async def discover(**_kwargs):
            """
            Simulate BLE discovery that finds no devices.

            Returns:
                list: An empty list of discovered BLE devices.

            """
            return []

        async def start(self):
            """
            Start the BLE scanner.

            This stub implementation performs no action.

            Returns:
                None

            """
            pass

        async def stop(self):
            """
            Stop the scanner. This stub method performs no action.
            """
            pass

        def register_detection_callback(self, *_args, **_kwargs):
            """
            Accepts a detection callback but performs no action.

            Any positional and keyword arguments are accepted and ignored.
            """
            return None

    bleak_module.BleakClient = _StubBleakClient
    bleak_module.BleakScanner = _StubBleakScanner
    bleak_module.BLEDevice = _StubBLEDevice

    monkeypatch.setitem(sys.modules, "bleak", bleak_module)
    return bleak_module


@pytest.fixture(autouse=True)
def mock_bleak_exc(monkeypatch, mock_bleak):  # pylint: disable=redefined-outer-name
    """
    Create and register a minimal bleak.exc submodule exposing BleakError and BleakDBusError.

    The created module is attached as the `exc` attribute of the provided `mock_bleak` module and inserted into sys.modules under "bleak.exc".

    Returns:
        bleak_exc_module (module): Module object providing `BleakError` and `BleakDBusError`.

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
