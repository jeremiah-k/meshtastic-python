"""Common fixtures for BLE interface tests."""

import logging
import sys
import types
from types import SimpleNamespace
from typing import Optional

import pytest  # type: ignore[import-untyped]

# NOTE: ble_interface is imported lazily inside fixtures/helpers after mocks are installed.


class DummyClient:
    """Dummy client for testing BLE interface functionality."""

    def __init__(self, disconnect_exception: Optional[Exception] = None) -> None:
        """
        Test double for a BLE client used in unit tests.

        Parameters
        ----------
            disconnect_exception (Optional[Exception]): Exception to raise when disconnect() is called; pass None to disable raising.

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

    def has_characteristic(self, _specifier):
        """
        Indicates whether the mock client exposes a BLE characteristic matching the given specifier.

        Parameters
        ----------
            _specifier: Identifier for a characteristic (for example, a UUID string or a characteristic object).

        Returns
        -------
            `False` always.

        """
        return False

    def start_notify(self, *_args, **_kwargs):
        """
        Simulate subscribing to a BLE characteristic notification during tests.

        This no-op stub accepts any positional and keyword arguments and performs no action.
        """
        return None

    def read_gatt_char(self, *_args, **_kwargs):
        """
        Return an empty payload for a GATT characteristic read.

        Returns:
            bytes: An empty byte string (`b''`).

        """
        return b""

    def write_gatt_char(self, *_args, **_kwargs):
        """
        Simulate writing to a GATT characteristic without performing any operation.
        """
        return None

    def is_connected(self) -> bool:
        """
        Report whether the mock BLE client is connected.

        Returns:
            True — the mock client is always considered connected.

        """
        return True

    def disconnect(self, *_args, **_kwargs):
        """
        Record a disconnect invocation and raise a preconfigured exception if one was provided.

        Increments the instance's disconnect_calls counter. If self.disconnect_exception is set, that exception instance is raised.

        Raises:
            Exception: The exception instance stored in `self.disconnect_exception`, if present.

        """
        self.disconnect_calls += 1
        if self.disconnect_exception:
            raise self.disconnect_exception

    def close(self):
        """
        Record that the client has been closed.

        Increments the instance's close_calls counter to track how many times close() has been invoked.
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
    Collect and invoke callables registered with meshtastic.ble_interface.atexit for the duration of a test.

    Patches meshtastic.ble_interface.atexit.register and .unregister to record callbacks in an internal list, yields to the test, and after the test invokes all recorded callbacks. Exceptions raised by callbacks are caught and logged at debug level. The extra fixture parameters are present only to enforce fixture ordering and are not used by the fixture itself.
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
        Register a callable to be invoked later by appending it to the module-level `registered` list.

        Parameters
        ----------
            func (callable): Callable to register; may also be used as a decorator.

        Returns
        -------
            callable: The same callable that was registered.

        """
        registered.append(func)
        return func

    def fake_unregister(func):
        """
        Remove all occurrences of a previously registered callback from the module-level registry.

        Comparison is by object identity; every entry identical to `func` is removed.

        Parameters
        ----------
            func (callable): The callback to unregister.

        """
        registered[:] = [f for f in registered if f is not func]

    # Import after mocks: ensure bleak/pubsub/serial/tabulate are stubbed first
    import importlib
    import importlib.metadata as _im

    _orig_version = _im.version

    def _version_proxy(name: str):
        """
        Resolve a distribution's version, using the mocked bleak version when available.

        Parameters
        ----------
            name (str): The distribution name to resolve.

        Returns
        -------
            str: The version string for the requested distribution. If `name` is `"bleak"`, returns the mocked bleak module's `__version__` when present, otherwise `"0.0.0"`. For other names, returns the version as determined by the original version lookup.

        """
        if name == "bleak":
            # use mocked bleak's __version__ if available; else a benign default
            return getattr(sys.modules.get("bleak"), "__version__", "0.0.0")
        return _orig_version(name)

    monkeypatch.setattr(_im, "version", _version_proxy)
    ble_mod = importlib.import_module("meshtastic.ble_interface")

    monkeypatch.setattr(ble_mod.atexit, "register", fake_register, raising=True)
    monkeypatch.setattr(ble_mod.atexit, "unregister", fake_unregister, raising=True)
    yield
    # run any registered functions manually to avoid surprising global state
    for func in registered:
        try:
            func()
        except Exception as e:  # noqa: BLE001 - tests must swallow any callback failure
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
    Create a BLEInterface instance configured for tests with a stubbed `connect` that returns the supplied client and a no-op `_startConfig`.

    Parameters
    ----------
        monkeypatch: pytest monkeypatch fixture used to patch BLEInterface methods.
        client: Fake or mock BLE client instance to be returned by the patched `connect` method.

    Returns
    -------
        BLEInterface: A test-configured interface whose `connect` returns `client` and whose `_startConfig` performs no configuration.

    """
    # Import BLEInterface lazily after mocks are in place
    import importlib

    BLEInterface = importlib.import_module("meshtastic.ble_interface").BLEInterface

    connect_calls: list = []

    def _stub_connect(
        _self: BLEInterface, _address: Optional[str] = None
    ) -> "DummyClient":
        """
        Provide the preconfigured test BLE client and record the connection attempt.

        Assigns the test client to the interface, clears the interface's disconnect-notified flag, appends the attempted address to connect_calls, advances the connection state to CONNECTING then CONNECTED, and sets _reconnected_event if present.

        Parameters
        ----------
            _address (str | None): Accepted to match the original signature; ignored by this stub.

        Returns
        -------
            DummyClient: The preconfigured test client instance.

        """
        connect_calls.append(_address)
        _self.client = client
        _self._disconnect_notified = False
        # Mark as connected for proper pubsub behavior in tests
        # Set isConnected flag to simulate connected state
        _self.isConnected.set()
        # Also call _connected to trigger pubsub messages
        _self._connected()
        # Update state manager to CONNECTED for proper state tracking
        # In tests, we skip CONNECTING and go directly to CONNECTED
        from meshtastic.ble_interface import ConnectionState

        # First transition to CONNECTING to simulate proper connection flow
        _self._state_manager.transition_to(ConnectionState.CONNECTING)
        success = _self._state_manager.transition_to(ConnectionState.CONNECTED, client)
        if not success:
            print(
                f"WARNING: State transition to CONNECTED failed, current state: {_self._state_manager.state}"
            )
        if hasattr(_self, "_reconnected_event"):
            _self._reconnected_event.set()
        return client

    def _stub_start_config(_self: BLEInterface) -> None:
        """
        No-op startup configuration hook for a BLEInterface used in tests.

        Replaces the real startup configuration to prevent side effects during testing.
        """
        return None

    monkeypatch.setattr(BLEInterface, "connect", _stub_connect)
    monkeypatch.setattr(BLEInterface, "_startConfig", _stub_start_config)
    iface = BLEInterface(address="dummy", noProto=True)
    iface._connect_stub_calls = connect_calls
    return iface
