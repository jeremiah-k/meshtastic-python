"""Common fixtures for BLE interface tests."""

import logging
import sys
from types import SimpleNamespace
from typing import Optional

import pytest  # type: ignore[import-untyped]  # pylint: disable=E0401

# NOTE: ble_interface is imported lazily inside fixtures/helpers after mocks are installed.


class DummyClient:
    """Dummy client for testing BLE interface functionality."""

    def __init__(self, disconnect_exception: Optional[Exception] = None) -> None:
        """
        Initialize a dummy BLE client used as a test double.

        Parameters
        ----------
            disconnect_exception (Optional[Exception]): Exception to raise when disconnect() is called; pass `None` to disable raising.

        Attributes
        ----------
            disconnect_calls (int): Number of times disconnect() has been invoked.
            close_calls (int): Number of times close() has been invoked.
            address (str): Client address identifier, set to "dummy".
            disconnect_exception (Optional[Exception]): Stored exception raised by disconnect(), if any.
            services (types.SimpleNamespace): Provides get_characteristic(specifier) -> None for characteristic lookups.
            bleak_client (types.SimpleNamespace): Minimal mock of a bleak client with an `address` attribute used for identity checks.

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
        Report whether the client exposes a BLE characteristic matching the given specifier.

        Parameters
        ----------
            _specifier: Identifier for a characteristic (e.g., a UUID string or characteristic object).

        Returns
        -------
            `False` always.

        """
        return False

    def start_notify(self, *_args, **_kwargs):
        """
        Simulate subscribing to a BLE characteristic notification for tests.

        Accepts any positional and keyword arguments and performs no action.
        """
        return None

    def read_gatt_char(self, *_args, **_kwargs):
        """
        Provide an empty payload for a GATT characteristic read.

        Returns:
            bytes: An empty byte string `b''`.

        """
        return b""

    def write_gatt_char(self, *_args, **_kwargs):
        """
        No-op stub that simulates writing to a BLE GATT characteristic and ignores all arguments.
        """
        return None

    def is_connected(self) -> bool:
        """
        Report whether the dummy BLE client is connected.

        Returns:
            True if the client is considered connected, False otherwise.

        """
        return True

    def disconnect(self, *_args, **_kwargs):
        """
        Record that a disconnect was invoked and raise the configured exception, if any.

        Raises:
            Exception: The exception instance stored in `self.disconnect_exception`, if present.

        """
        self.disconnect_calls += 1
        if self.disconnect_exception:
            raise self.disconnect_exception

    def close(self):
        """
        Record a close operation by incrementing the dummy client's close_calls counter.

        Used by tests to verify how many times close() has been invoked.
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
    Register and later invoke callables that code under test registers with meshtastic.ble_interface.atexit.

    Patches meshtastic.ble_interface.atexit.register and .unregister to record callbacks in a local list, yields control to the test, and after the test invokes each recorded callback while swallowing and logging any exceptions at debug level. The extra fixture parameters exist only to enforce pytest fixture ordering and are not used by this fixture.
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
        Register a callable for later invocation by appending it to the local registry.

        Parameters
        ----------
            func (callable): Callable to register; may also be used as a decorator.

        Returns
        -------
            callable: The registered callable.

        """
        registered.append(func)
        return func

    def fake_unregister(func):
        """
        Unregister a previously registered callback by identity.

        Removes all occurrences of the given callable from the module-level registered list using object identity comparison.

        Parameters
        ----------
            func (callable): Callback to remove from the registry.

        """
        registered[:] = [f for f in registered if f is not func]

    # Import after mocks: ensure bleak/pubsub/serial/tabulate are stubbed first
    import importlib  # pylint: disable=C0415
    import importlib.metadata as _im  # pylint: disable=C0415

    _orig_version = _im.version

    def _version_proxy(name: str):
        """
        Return a distribution version, preferring a mocked bleak version when present.

        Parameters
        ----------
            name (str): Distribution name to resolve.

        Returns
        -------
            str: If `name` is "bleak", the mocked `bleak.__version__` when available, otherwise "0.0.0". For other names, the version from the original resolver.

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
    Create a BLEInterface configured for tests with a patched `connect` that returns the supplied `client` and a no-op `_startConfig`.

    Parameters
    ----------
        client: Fake or mock BLE client instance that the patched `connect` will return and assign to the interface.
        monkeypatch: Pytest monkeypatch fixture for patching.

    Returns
    -------
        BLEInterface: A test-configured interface whose `connect` returns `client`, whose `_startConfig` does nothing,
        and which exposes `_connect_stub_calls` (list of addresses passed to the stubbed `connect`).

    """
    # Import BLEInterface lazily after mocks are in place
    import importlib  # pylint: disable=C0415

    BLEInterface = importlib.import_module("meshtastic.ble_interface").BLEInterface

    connect_calls: list = []

    def _stub_connect(
        _self: BLEInterface, _address: Optional[str] = None
    ) -> "DummyClient":
        """
        Assign the prepared test BLE client to the interface and record the connection attempt.

        Records the attempted address in the external `connect_calls` list, assigns the preconfigured test client to the interface, marks the interface as connected, triggers the interface's connected hook, and sets `_reconnected_event` if present.

        Parameters
        ----------
            _address (Optional[str]): Ignored by this stub; accepted to match the original signature.

        Returns
        -------
            DummyClient: The test client instance assigned to the interface.

        """
        connect_calls.append(_address)
        _self.client = client
        _self._disconnect_notified = False
        # Mark as connected for proper pubsub behavior in tests
        # Set isConnected flag to simulate connected state
        _self.isConnected.set()
        # Update state manager to CONNECTED for proper state tracking
        # In tests, we skip CONNECTING and go directly to CONNECTED
        from meshtastic.ble_interface import ConnectionState  # pylint: disable=C0415

        # First transition to CONNECTING to simulate proper connection flow
        _self._state_manager.transition_to(ConnectionState.CONNECTING)
        success = _self._state_manager.transition_to(ConnectionState.CONNECTED, client)
        if not success:
            print(
                f"WARNING: State transition to CONNECTED failed, current state: {_self._state_manager.state}"
            )
        # Now fire connected hook to publish after state is CONNECTED
        _self._connected()
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
