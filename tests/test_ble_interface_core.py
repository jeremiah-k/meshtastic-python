"""Tests for the BLE interface module - Core functionality."""

import asyncio
import contextlib
import logging
import re
import subprocess
import threading
import time
from collections.abc import Iterator
from queue import Queue
from types import SimpleNamespace, TracebackType
from typing import TYPE_CHECKING, Any, Callable, Literal, Protocol, cast
from unittest.mock import MagicMock

import pytest
from bleak.backends.device import BLEDevice
from bleak.exc import BleakDBusError, BleakError

# Import meshtastic modules for use in tests
import meshtastic.interfaces.ble as ble_mod
import meshtastic.interfaces.ble.discovery as discovery_mod
from meshtastic.interfaces.ble import (
    FROMNUM_UUID,
    LEGACY_LOGRADIO_UUID,
    LOGRADIO_UUID,
    SERVICE_UUID,
    BLEClient,
    BLEInterface,
)
from meshtastic.interfaces.ble.connection import ConnectionValidator
from meshtastic.interfaces.ble.constants import (
    BLECLIENT_ERROR_CANNOT_PAIR_NOT_INITIALIZED,
    BLECLIENT_ERROR_CANNOT_UNPAIR_NOT_INITIALIZED,
    CONNECTION_ERROR_LOST_OWNERSHIP,
    ERROR_CONNECTION_SUPPRESSED,
    ERROR_INTERFACE_CLOSING,
    ERROR_MANAGEMENT_ADDRESS_EMPTY,
    ERROR_MANAGEMENT_ADDRESS_REQUIRED,
    ERROR_MANAGEMENT_AWAIT_TIMEOUT_INVALID,
    ERROR_MANAGEMENT_CONNECTING,
    ERROR_MANAGEMENT_TARGET_CHANGED,
    ERROR_TRUST_ADDRESS_NOT_RESOLVED,
    ERROR_TRUST_BLUETOOTHCTL_MISSING,
    ERROR_TRUST_COMMAND_FAILED,
    ERROR_TRUST_COMMAND_TIMEOUT,
    ERROR_TRUST_INVALID_TIMEOUT,
)
from meshtastic.interfaces.ble.discovery import (
    DiscoveryClientError,
    DiscoveryManager,
    _close_discovery_client_best_effort,
    _filter_devices_for_target_identifier,
    _looks_like_ble_address,
    _parse_scan_response,
)
from meshtastic.interfaces.ble.reconnection import ReconnectScheduler, ReconnectWorker
from meshtastic.interfaces.ble.state import BLEStateManager, ConnectionState

# Import common fixtures
from tests.test_ble_interface_fixtures import DummyClient, _build_interface

pytestmark = pytest.mark.unit

_MAX_SPURIOUS_CONNECT_WAIT_CALLS_BEFORE_FAIL = 10
_MAX_SPURIOUS_CLOSE_WAIT_CALLS_BEFORE_FAIL = 50
START_FAILED_MSG = "start failed"


def _pin_trust_environment(
    monkeypatch: pytest.MonkeyPatch,
    *,
    run: Callable[..., object] | None = None,
) -> None:
    """
    Configure host-specific dependencies so tests of trust() run in a controlled Linux-like environment.

    Parameters
    ----------
        monkeypatch (pytest.MonkeyPatch): Fixture used to apply attribute patches.
        run (Callable[..., object] | None): Optional replacement for subprocess.run used by trust(); if None, a sentinel callable is installed that raises AssertionError if invoked.
    """
    monkeypatch.setattr("meshtastic.interfaces.ble.interface.sys.platform", "linux")
    monkeypatch.setattr(
        "meshtastic.interfaces.ble.interface.shutil.which",
        lambda _name: "/usr/bin/bluetoothctl",
    )
    if run is None:

        def _unexpected_run(*_args: object, **_kwargs: object) -> None:
            raise AssertionError("subprocess.run should not be reached")

        run = _unexpected_run
    monkeypatch.setattr("meshtastic.interfaces.ble.interface.subprocess.run", run)


def _capture_management_wait_event(
    monkeypatch: pytest.MonkeyPatch,
    iface: BLEInterface,
) -> threading.Event:
    """Return an event that fires when close() blocks on in-flight management work."""
    wait_entered = threading.Event()
    original_wait = iface._management_idle_condition.wait

    def _wait(timeout: float | None = None) -> bool:
        wait_entered.set()
        return original_wait(timeout=timeout)

    monkeypatch.setattr(iface._management_idle_condition, "wait", _wait)
    return wait_entered


if TYPE_CHECKING:

    class _PubProtocol(Protocol):
        """Protocol for pubsub test doubles.

        Methods
        -------
        sendMessage(topic: str, **kwargs: Any)
        """

        def sendMessage(self, topic: str, **kwargs: Any) -> None:
            """Publish a message to the specified pubsub topic.

            The provided keyword arguments are assembled into the message payload and published under the given topic name.

            Parameters
            ----------
            topic : str
                Topic name to publish the message under.
            **kwargs : Any
                Arbitrary key/value pairs included as the message payload.
            """
            ...

    pub: _PubProtocol
else:  # pragma: no cover - import only at runtime
    from pubsub import pub


def _create_ble_device(address: str, name: str) -> BLEDevice:
    """Construct a BLEDevice for testing.

    Parameters
    ----------
    address : str
    name : str

    Returns
    -------
    BLEDevice
        A BLEDevice instance for use in tests.
    """
    return BLEDevice(address=address, name=name, details={})


def _build_minimal_connect_test_interface() -> BLEInterface:
    """Create a minimally initialized BLEInterface for connect() unit tests."""
    iface = object.__new__(BLEInterface)
    iface._state_manager = BLEStateManager()
    iface._state_lock = threading.RLock()
    iface._connect_lock = threading.RLock()
    iface._management_lock = threading.RLock()
    iface._management_idle_condition = threading.Condition(iface._management_lock)
    iface._management_inflight = 0
    iface._disconnect_lock = threading.Lock()
    iface._closed = False
    iface.address = None
    iface.client = None
    iface._disconnect_notified = False
    iface._client_publish_pending = False
    iface._last_connection_request = None
    iface.pair_on_connect = False
    iface._connection_alias_key = None
    iface._ever_connected = False
    iface._read_retry_count = 0
    cast(Any, iface)._client_manager = SimpleNamespace(
        _safe_close_client=lambda _client: None
    )
    return iface


class _FakeDiscoveryClient:
    """Context-manager BLE client stub used by discovery tests."""

    def __init__(
        self,
        discover_result: dict[str, Any],
        *,
        async_await_impl: Callable[..., Any] | None = None,
    ) -> None:
        """Initialize the fake discovery client with a preset discovery result.

        Parameters
        ----------
        discover_result : dict[str, Any]
            The value to return from discovery() calls; represents the simulated scan results.
        async_await_impl : Callable[..., Any] | None
            Optional function used to run/await coroutines passed to async_await(coro, timeout). If omitted, the default awaiting behavior is used.
        """
        self._discover_result = discover_result
        self._async_await_impl = async_await_impl

    def __enter__(self) -> "_FakeDiscoveryClient":
        """Enter the context for the fake discovery client and return the client instance.

        Returns
        -------
        '_FakeDiscoveryClient'
            The fake discovery client instance to be used inside the context manager.
        """
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> Literal[False]:
        """Exit the context and indicate that any exception should propagate.

        Parameters
        ----------
        exc_type : type[BaseException] | None
            Exception type if an exception was raised inside the context, otherwise None.
        exc : BaseException | None
            Exception instance if raised, otherwise None.
        tb : TracebackType | None
            Traceback object if an exception was raised, otherwise None.

        Returns
        -------
        bool
            `False` to indicate that exceptions should not be suppressed and must be re-raised.
        """
        _ = (exc_type, exc, tb)
        return False

    def _discover(self, **_kwargs: Any) -> dict[str, Any]:
        """Provide the preconfigured discovery result for use in tests.

        Parameters
        ----------
        **_kwargs : Any

        Returns
        -------
        dict[str, Any]
            The stored discovery result dictionary that this fake discovery client will return.
        """
        return self._discover_result

    def discover(self, **kwargs: Any) -> dict[str, Any]:
        """Alias for _discover.

        Parameters
        ----------
        **kwargs : Any

        Returns
        -------
        dict[str, Any]
        """
        return self._discover(**kwargs)

    def _async_await(self, coro: Any, timeout: float | None = None) -> Any:
        """Run the given coroutine to completion using the configured await implementation or the default runner.

        Parameters
        ----------
        coro : Any
            The coroutine or awaitable to execute.
        timeout : float | None
            Optional timeout in seconds for the await implementation to honor; may be ignored by the configured implementation. (Default value = None)

        Returns
        -------
        Any
            The value returned by the awaited coroutine.
        """
        if self._async_await_impl is not None:
            return self._async_await_impl(coro, timeout)
        return asyncio.run(coro)

    def async_await(self, coro: Any, timeout: float | None = None) -> Any:
        """Alias for _async_await.

        Parameters
        ----------
        coro : Any
        timeout : float | None

        Returns
        -------
        Any
        """
        return self._async_await(coro, timeout)


def _attach_close_monitor(
    monkeypatch: pytest.MonkeyPatch, iface: BLEInterface
) -> threading.Event:
    """Wrap iface.close so calling close sets a threading.Event and then invokes the original close.

    Parameters
    ----------
    monkeypatch : pytest.MonkeyPatch
        pytest-style monkeypatch fixture used to replace attributes on the interface.
    iface : BLEInterface
        BLEInterface whose close method will be wrapped.

    Returns
    -------
    threading.Event
        event that will be set when the patched close is invoked.
    """
    original_close = iface.close
    close_called = threading.Event()

    # Bind outer values into defaults so monkeypatched method keeps stable
    # references even if local names are reassigned later in the test.
    def _mock_close(
        original_close: Callable[[], Any] = original_close,
        close_called: threading.Event = close_called,
    ) -> Any:
        """Mark the provided close event and invoke the original close callable.

        Parameters
        ----------
        original_close : Callable[[], Any]
            The original close function to invoke. (Default value = original_close)
        close_called : threading.Event
            Event to set when close is invoked. (Default value = close_called)

        Returns
        -------
        Any
            The value returned by `original_close`.
        """
        close_called.set()
        return original_close()

    monkeypatch.setattr(iface, "close", _mock_close)
    return close_called


class _ReconnectTestNotificationManager:
    """Shared notification-manager test double for reconnect worker tests."""

    def __init__(self, *, fail_on_resubscribe: bool = False) -> None:
        """Initialize the test notification manager used by reconnect tests.

        Tracks how many times cleanup is requested and records resubscription attempts.
        When `fail_on_resubscribe` is True, the manager is configured to simulate a failing
        resubscribe operation.

        Parameters
        ----------
        fail_on_resubscribe : bool
            If True, resubscription attempts will be treated as failures. (Default value = False)
        """
        self.cleaned = 0
        self.resubscribed: list[tuple[Any, float]] = []
        self._fail_on_resubscribe = fail_on_resubscribe

    def _cleanup_all(self) -> None:
        """Record notification cleanup calls."""
        self.cleaned += 1

    def _resubscribe_all(self, client: Any, timeout: float) -> None:
        """Record a resubscription request for testing, or raise if resubscriptions are configured to fail.

        Parameters
        ----------
        client : Any
            The client object for which resubscription was requested.
        timeout : float
            The timeout (in seconds) to use for the resubscription attempt.

        Raises
        ------
        AssertionError
            If the test instance is configured to fail on resubscribe.
        """
        if self._fail_on_resubscribe:
            raise AssertionError("Should not resubscribe without a client")
        self.resubscribed.append((client, timeout))


class _ReconnectTestScheduler:
    """Shared reconnect-scheduler test double for reconnect worker tests."""

    def __init__(self) -> None:
        """Initialize the test scheduler and mark it as not cleared.

        Sets the `cleared` attribute to `False`. The `cleared` flag indicates whether clear_thread_reference() has been invoked.
        """
        self.cleared = False

    def _clear_thread_reference(self) -> None:
        """Record that reconnect thread reference cleanup was requested."""
        self.cleared = True


def test_find_device_returns_single_scan_result() -> None:
    """FindDevice should return the lone scanned device."""
    # BLEDevice and BLEInterface already imported at top as ble_mod.BLEDevice, ble_mod.BLEInterface

    # Intentional constructor bypass: inject a controlled _discovery_manager
    # without running BLEInterface.__init__ side effects.
    iface = object.__new__(ble_mod.BLEInterface)
    scanned_device = _create_ble_device(address="11:22:33:44:55:66", name="Test Device")
    iface._discovery_manager = SimpleNamespace(  # type: ignore[assignment]
        _discover_devices=lambda _address: [scanned_device]
    )

    result = ble_mod.BLEInterface.findDevice(iface, None)

    assert result is scanned_device


def test_find_device_multiple_scan_results_without_address_raises() -> None:
    """Discovery-mode findDevice should reject ambiguous multi-device scans."""
    iface = object.__new__(ble_mod.BLEInterface)
    devices = [
        _create_ble_device(address="11:22:33:44:55:66", name="Meshtastic-A"),
        _create_ble_device(address="22:33:44:55:66:77", name="Meshtastic-B"),
    ]
    iface._discovery_manager = SimpleNamespace(  # type: ignore[assignment]
        _discover_devices=lambda _address: devices
    )

    with pytest.raises(BLEInterface.BLEError) as excinfo:
        ble_mod.BLEInterface.findDevice(iface, None)

    assert "Multiple Meshtastic BLE peripherals found." in str(excinfo.value)


def test_ble_package_all_uses_stable_surface() -> None:
    """`meshtastic.interfaces.ble.__all__` should expose the stable facade only."""
    assert "BLEInterface" in ble_mod.__all__
    assert "BLEClient" in ble_mod.__all__
    assert "ConnectionValidator" not in ble_mod.__all__
    assert "ThreadCoordinator" not in ble_mod.__all__


def test_ble_package_and_legacy_facade_exports_match() -> None:
    """Legacy BLE facade should include canonical exports plus retained Bleak compat names."""
    import meshtastic.ble_interface as legacy_ble_mod

    canonical_exports = set(ble_mod.__all__)
    legacy_exports = set(legacy_ble_mod.__all__)
    compat_bleak_exports = {
        "BleakClient",
        "BleakScanner",
        "BLEDevice",
        "BleakError",
        "BleakDBusError",
    }

    assert canonical_exports.issubset(legacy_exports)
    assert compat_bleak_exports.issubset(legacy_exports)
    assert canonical_exports.isdisjoint(compat_bleak_exports)


def test_state_manager_closing_only_for_disconnect() -> None:
    """is_closing should be true only while disconnecting."""
    state_manager = BLEStateManager()
    assert state_manager._is_closing is False
    # DISCONNECTED -> DISCONNECTING is not allowed (semantically incorrect:
    # you can't "begin disconnecting" from an already-disconnected state).
    # The proper path is through a connected/active state first.
    assert state_manager._transition_to(ConnectionState.CONNECTING) is True
    assert state_manager._is_closing is False
    assert state_manager._transition_to(ConnectionState.DISCONNECTING) is True
    assert state_manager._is_closing is True
    assert state_manager._transition_to(ConnectionState.DISCONNECTED) is True
    assert state_manager._is_closing is False
    # ERROR state should also not be "closing"
    assert state_manager._transition_to(ConnectionState.ERROR) is True
    assert state_manager._is_closing is False


def test_state_manager_allows_error_to_disconnecting_shutdown() -> None:
    """State manager should support ERROR -> DISCONNECTING for deterministic close paths."""
    state_manager = BLEStateManager()

    assert state_manager._transition_to(ConnectionState.CONNECTING) is True
    assert state_manager._transition_to(ConnectionState.ERROR) is True
    assert state_manager._transition_to(ConnectionState.DISCONNECTING) is True
    assert state_manager._is_closing is True
    assert state_manager._transition_to(ConnectionState.DISCONNECTED) is True
    assert state_manager._is_closing is False


def test_ble_interface_defaults_auto_reconnect_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """BLEInterface should default auto_reconnect to False.

    Parameters
    ----------
    monkeypatch : pytest.MonkeyPatch
    """
    iface = _build_interface(monkeypatch, DummyClient(), start_receive_thread=False)
    assert iface.auto_reconnect is False
    assert iface.pair_on_connect is False
    iface.close()


def test_ble_interface_init_rejects_non_bool_pair_on_connect() -> None:
    """Constructor should reject non-bool pair_on_connect values."""
    with pytest.raises(BLEInterface.BLEError, match="pair_on_connect must be a bool"):
        BLEInterface(
            address=None,
            noProto=True,
            pair_on_connect=cast(Any, "false"),
        )


def test_ble_interface_repr_includes_non_default_flags(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """repr() should include non-default flags and debug output."""
    iface = _build_interface(monkeypatch, DummyClient(), start_receive_thread=False)

    def _debug_sink(_line: str) -> None:
        return None

    iface.debugOut = _debug_sink
    iface.noProto = True
    iface.noNodes = True
    iface.auto_reconnect = True
    iface.pair_on_connect = True

    rendered = repr(iface)

    assert "address='dummy'" in rendered
    assert "debugOut=" in rendered
    assert "noProto=True" in rendered
    assert "noNodes=True" in rendered
    assert "auto_reconnect=True" in rendered
    assert "pair_on_connect=True" in rendered
    iface.close()


def test_build_interface_connect_stub_records_kwargs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The shared test connect stub should retain keyword arguments for assertions."""
    iface = _build_interface(monkeypatch, DummyClient(), start_receive_thread=False)

    iface.connect("AA:BB:CC:DD:EE:FF", pair=True, connect_timeout=4.5)

    assert cast(Any, iface)._connect_stub_calls[-1] == "AA:BB:CC:DD:EE:FF"
    assert cast(Any, iface)._connect_stub_kwargs[-1] == {
        "pair": True,
        "connect_timeout": 4.5,
    }
    iface.close()


def test_ble_interface_extract_client_address_prefers_bleak_and_falls_back() -> None:
    """_extract_client_address should prefer bleak_client.address and then client.address."""
    assert (
        BLEInterface._extract_client_address(
            cast(
                BLEClient,
                SimpleNamespace(
                    bleak_client=SimpleNamespace(address="AA:BB:CC:DD:EE:FF"),
                    address="11:22:33:44:55:66",
                ),
            )
        )
        == "AA:BB:CC:DD:EE:FF"
    )
    assert (
        BLEInterface._extract_client_address(
            cast(
                BLEClient,
                SimpleNamespace(
                    bleak_client=SimpleNamespace(address=None),
                    address="11:22:33:44:55:66",
                ),
            )
        )
        == "11:22:33:44:55:66"
    )
    assert BLEInterface._extract_client_address(None) is None


def test_ble_interface_pair_prefers_active_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """pair() should delegate to the active matching client when connected."""
    client = DummyClient()
    iface = _build_interface(monkeypatch, client, start_receive_thread=False)
    monkeypatch.setattr(
        iface,
        "findDevice",
        lambda _address: pytest.fail(
            "Unexpected findDevice call during active-client pair reuse test"
        ),
    )
    monkeypatch.setattr(
        "meshtastic.interfaces.ble.interface.BLEClient",
        lambda *_args, **_kwargs: pytest.fail(
            "Unexpected temporary BLEClient created during active-client pair reuse test"
        ),
    )

    iface.pair(confirm=True, await_timeout=12.5)
    assert client.pair_calls == 1
    assert client.pair_kwargs == [{"confirm": True}]
    assert client.pair_await_timeouts == [12.5]
    iface.close()


def test_ble_interface_pair_prefers_active_client_without_address(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """pair() should reuse an active client even when it cannot expose an address."""
    client = DummyClient()
    client.address = cast(Any, None)
    client.bleak_client = SimpleNamespace(address=None)
    iface = _build_interface(monkeypatch, client, start_receive_thread=False)
    with iface._state_lock:
        iface.address = "mesh-node"
    monkeypatch.setattr(
        iface,
        "findDevice",
        lambda _address: pytest.fail(
            "Unexpected findDevice call during active-client address-less pair reuse test"
        ),
    )
    monkeypatch.setattr(
        "meshtastic.interfaces.ble.interface.BLEClient",
        lambda *_args, **_kwargs: pytest.fail(
            "Unexpected temporary BLEClient created during active-client address-less pair reuse test"
        ),
    )

    iface.pair(confirm=True, await_timeout=9.5)
    assert client.pair_calls == 1
    assert client.pair_kwargs == [{"confirm": True}]
    assert client.pair_await_timeouts == [9.5]
    iface.close()


def test_ble_interface_unpair_prefers_active_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """unpair() should delegate and run disconnect cleanup when the backend drops."""
    client = DummyClient()
    iface = _build_interface(monkeypatch, client, start_receive_thread=False)
    monkeypatch.setattr(
        iface,
        "findDevice",
        lambda _address: pytest.fail(
            "Unexpected findDevice call during active-client unpair reuse test"
        ),
    )
    monkeypatch.setattr(
        "meshtastic.interfaces.ble.interface.BLEClient",
        lambda *_args, **_kwargs: pytest.fail(
            "Unexpected temporary BLEClient created during active-client unpair reuse test"
        ),
    )

    def _on_unpair() -> None:
        iface._handle_disconnect("test-unpair", client=cast(BLEClient, client))

    client.on_unpair = _on_unpair

    iface.unpair(await_timeout=8.0)
    assert client.unpair_calls == 1
    assert client.unpair_await_timeouts == [8.0]
    assert iface.client is None
    assert iface._state_manager._is_connected is False
    iface.close()


def test_ble_interface_pair_uses_existing_client_when_request_matches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """pair() should reuse a matching existing client before creating a temporary one."""
    iface = _build_interface(monkeypatch, DummyClient(), start_receive_thread=False)
    with iface._state_lock:
        iface.client = None
        iface._state_manager._reset_to_disconnected()

    existing_client = DummyClient()
    existing_client.address = "AA:BB:CC:DD:EE:FF"
    existing_client.bleak_client = SimpleNamespace(address="AA:BB:CC:DD:EE:FF")

    monkeypatch.setattr(
        iface,
        "_get_existing_client_if_valid",
        lambda _request: cast(BLEClient, existing_client),
    )
    monkeypatch.setattr(
        iface,
        "findDevice",
        lambda _address: pytest.fail(
            "Unexpected findDevice call during existing-client pair reuse test"
        ),
    )
    monkeypatch.setattr(
        "meshtastic.interfaces.ble.interface.BLEClient",
        lambda *_args, **_kwargs: pytest.fail(
            "Unexpected temporary BLEClient created during existing-client pair reuse test"
        ),
    )

    iface.pair("mesh-node", confirm=True, await_timeout=7.0)

    assert existing_client.pair_calls == 1
    assert existing_client.pair_kwargs == [{"confirm": True}]
    assert existing_client.pair_await_timeouts == [7.0]
    iface.close()


@pytest.mark.parametrize(
    "factory_mode",
    ["with_optional_kwargs", "without_optional_kwargs"],
)
def test_ble_interface_pair_uses_temporary_client_when_disconnected(
    monkeypatch: pytest.MonkeyPatch, factory_mode: str
) -> None:
    """pair() should use temporary BLEClient factories with or without kwargs.

    Parameters
    ----------
    monkeypatch : pytest.MonkeyPatch
        Fixture used to patch BLE client construction and cleanup hooks.
    factory_mode : str
        Factory signature variant under test:
        ``"with_optional_kwargs"`` or ``"without_optional_kwargs"``.

    Returns
    -------
    None
    """
    iface = _build_interface(monkeypatch, DummyClient(), start_receive_thread=False)
    with iface._state_lock:
        iface.client = None
        iface._state_manager._reset_to_disconnected()
    monkeypatch.setattr(
        iface,
        "findDevice",
        lambda _address: _create_ble_device("AA:BB:CC:DD:EE:FF", "Meshtastic"),
    )

    pair_kwargs: list[dict[str, object]] = []
    pair_await_timeouts: list[float | None] = []

    def _pair(*, await_timeout: float | None = None, **kwargs: object) -> None:
        pair_await_timeouts.append(await_timeout)
        pair_kwargs.append(dict(kwargs))

    temp_client = SimpleNamespace(
        pair=_pair,
        bleak_client=SimpleNamespace(address="AA:BB:CC:DD:EE:FF"),
    )
    cleanup_calls: list[Any] = []

    if factory_mode == "with_optional_kwargs":
        def _temp_client_factory(_address: str, **_kwargs: object) -> SimpleNamespace:
            return temp_client
    elif factory_mode == "without_optional_kwargs":
        def _temp_client_factory(_address: str) -> SimpleNamespace:
            return temp_client
    else:
        pytest.fail(f"Unexpected factory_mode: {factory_mode}")

    monkeypatch.setattr(
        "meshtastic.interfaces.ble.interface.BLEClient",
        _temp_client_factory,
    )
    monkeypatch.setattr(
        iface._client_manager,
        "_safe_close_client",
        lambda client: cleanup_calls.append(client),
    )

    iface.pair("mesh-node", confirm=True, await_timeout=7.0)
    assert pair_kwargs == [{"confirm": True}]
    assert pair_await_timeouts == [7.0]
    assert cleanup_calls == [temp_client]
    iface.close()


def test_ble_interface_close_waits_for_temporary_pair_operation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """close() should wait for temporary-client pair() work to finish."""
    iface = _build_interface(monkeypatch, DummyClient(), start_receive_thread=False)
    with iface._state_lock:
        iface.client = None
        iface.address = None
        iface._state_manager._reset_to_disconnected()
    monkeypatch.setattr(
        iface,
        "findDevice",
        lambda _address: _create_ble_device("AA:BB:CC:DD:EE:FF", "Meshtastic"),
    )

    pair_started = threading.Event()
    allow_pair_return = threading.Event()
    pair_errors: list[Exception] = []
    close_errors: list[Exception] = []
    cleanup_calls: list[Any] = []

    def _blocking_pair(*, await_timeout: float | None = None, **kwargs: object) -> None:
        _ = (await_timeout, kwargs)
        pair_started.set()
        assert allow_pair_return.wait(timeout=1.0)

    temp_client = SimpleNamespace(
        pair=_blocking_pair,
        bleak_client=SimpleNamespace(address="AA:BB:CC:DD:EE:FF"),
    )

    monkeypatch.setattr(
        "meshtastic.interfaces.ble.interface.BLEClient",
        lambda _address, **_kwargs: temp_client,
    )
    monkeypatch.setattr(
        iface._client_manager,
        "_safe_close_client",
        lambda client: cleanup_calls.append(client),
    )
    management_wait_entered = _capture_management_wait_event(monkeypatch, iface)

    def _run_pair() -> None:
        try:
            iface.pair("mesh-node", confirm=True, await_timeout=7.0)
        except (
            Exception
        ) as exc:  # pragma: no cover - failure captured below  # noqa: BLE001 - test captures thread errors
            pair_errors.append(exc)

    close_done = threading.Event()

    def _run_close() -> None:
        try:
            iface.close()
        except (
            Exception
        ) as exc:  # pragma: no cover - failure captured below  # noqa: BLE001 - test captures thread errors
            close_errors.append(exc)
        finally:
            close_done.set()

    pair_thread = threading.Thread(target=_run_pair, daemon=True)
    pair_thread.start()
    assert pair_started.wait(timeout=1.0)

    close_thread = threading.Thread(target=_run_close, daemon=True)
    close_thread.start()
    assert management_wait_entered.wait(timeout=1.0)
    with iface._state_lock:
        assert iface._closed is True
    assert close_done.is_set() is False

    allow_pair_return.set()
    pair_thread.join(timeout=2.0)
    close_thread.join(timeout=2.0)

    assert not pair_thread.is_alive()
    assert not close_thread.is_alive()
    assert pair_errors == []
    assert close_errors == []
    assert cleanup_calls == [temp_client]


def test_ble_interface_unpair_uses_temporary_client_when_disconnected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """unpair() should create and clean up a temporary BLEClient when disconnected."""
    iface = _build_interface(monkeypatch, DummyClient(), start_receive_thread=False)
    with iface._state_lock:
        iface.client = None
        iface._state_manager._reset_to_disconnected()
    monkeypatch.setattr(
        iface,
        "findDevice",
        lambda _address: _create_ble_device("AA:BB:CC:DD:EE:FF", "Meshtastic"),
    )

    unpair_await_timeouts: list[float | None] = []

    def _unpair(*, await_timeout: float | None = None) -> None:
        unpair_await_timeouts.append(await_timeout)

    temp_client = SimpleNamespace(
        unpair=_unpair,
        bleak_client=SimpleNamespace(address="AA:BB:CC:DD:EE:FF"),
    )
    cleanup_calls: list[Any] = []

    def _temp_client_factory(_address: str, **_kwargs: object) -> SimpleNamespace:
        return temp_client

    monkeypatch.setattr(
        "meshtastic.interfaces.ble.interface.BLEClient",
        _temp_client_factory,
    )
    monkeypatch.setattr(
        iface._client_manager,
        "_safe_close_client",
        lambda client: cleanup_calls.append(client),
    )

    iface.unpair("mesh-node", await_timeout=7.0)

    assert unpair_await_timeouts == [7.0]
    assert cleanup_calls == [temp_client]
    iface.close()


@pytest.mark.parametrize("method_name", ["pair", "unpair"])
def test_ble_interface_management_rejects_temp_client_when_target_owned_elsewhere(
    monkeypatch: pytest.MonkeyPatch,
    method_name: str,
) -> None:
    """Disconnected management ops should not open a temp client for another interface's target."""
    iface = _build_interface(monkeypatch, DummyClient(), start_receive_thread=False)
    with iface._state_lock:
        iface.client = None
        iface._state_manager._reset_to_disconnected()
    monkeypatch.setattr(
        iface,
        "findDevice",
        lambda _address: _create_ble_device("AA:BB:CC:DD:EE:FF", "Meshtastic"),
    )
    lock_states: list[tuple[bool, bool]] = []

    def _connected_elsewhere_probe(
        key: str | None, owner: object | None = None
    ) -> bool:
        probe_done = threading.Event()
        probe_result: list[tuple[bool, bool]] = []

        def _probe_lock_ownership() -> None:
            connect_lock_was_free = iface._connect_lock.acquire(blocking=False)
            if connect_lock_was_free:
                iface._connect_lock.release()
            management_lock_was_free = iface._management_lock.acquire(blocking=False)
            if management_lock_was_free:
                iface._management_lock.release()
            probe_result.append((connect_lock_was_free, management_lock_was_free))
            probe_done.set()

        probe_thread = threading.Thread(target=_probe_lock_ownership, daemon=True)
        probe_thread.start()
        assert probe_done.wait(timeout=1.0)
        probe_thread.join(timeout=1.0)
        assert probe_result
        lock_states.append(probe_result[0])
        return key == "aabbccddeeff" and owner is iface

    monkeypatch.setattr(
        "meshtastic.interfaces.ble.interface._is_currently_connected_elsewhere",
        _connected_elsewhere_probe,
    )
    monkeypatch.setattr(
        "meshtastic.interfaces.ble.interface.BLEClient",
        lambda *_args, **_kwargs: pytest.fail(
            "Temporary BLEClient should not be created when target is owned elsewhere"
        ),
    )

    if method_name == "pair":
        with pytest.raises(BLEInterface.BLEError, match=ERROR_CONNECTION_SUPPRESSED):
            iface.pair("mesh-node", confirm=True, await_timeout=7.0)
    else:
        with pytest.raises(BLEInterface.BLEError, match=ERROR_CONNECTION_SUPPRESSED):
            iface.unpair("mesh-node", await_timeout=7.0)

    assert lock_states
    assert all(state == (True, True) for state in lock_states)
    iface.close()


@pytest.mark.parametrize("method_name", ["pair", "unpair"])
def test_ble_interface_management_revalidates_implicit_target_after_gate_handoff(
    monkeypatch: pytest.MonkeyPatch,
    method_name: str,
) -> None:
    """Implicit management ops should abort if the interface target changes at the gate."""
    current_address = "AA:BB:CC:DD:EE:FF"
    replacement_address = "11:22:33:44:55:66"
    client = DummyClient()
    client.address = current_address
    client.bleak_client = SimpleNamespace(address=current_address)
    iface = _build_interface(monkeypatch, client, start_receive_thread=False)

    replacement_client = DummyClient()
    replacement_client.address = replacement_address
    replacement_client.bleak_client = SimpleNamespace(address=replacement_address)
    command_calls: list[str] = []

    def _record_pair(*, await_timeout: float = 0.0, **kwargs: object) -> None:
        _ = (await_timeout, kwargs)
        command_calls.append("pair")

    def _record_unpair(*, await_timeout: float = 0.0, **kwargs: object) -> None:
        _ = (await_timeout, kwargs)
        command_calls.append("unpair")

    monkeypatch.setattr(client, "pair", _record_pair)
    monkeypatch.setattr(client, "unpair", _record_unpair)
    monkeypatch.setattr(replacement_client, "pair", _record_pair)
    monkeypatch.setattr(replacement_client, "unpair", _record_unpair)

    @contextlib.contextmanager
    def _swap_target_gate(_target_address: str) -> Iterator[None]:
        with iface._state_lock:
            cast(Any, iface).client = replacement_client
            iface.address = replacement_address
            iface._state_manager._reset_to_disconnected()
            assert iface._state_manager._transition_to(ConnectionState.CONNECTING)
            assert iface._state_manager._transition_to(ConnectionState.CONNECTED)
        yield

    monkeypatch.setattr(iface, "_management_target_gate", _swap_target_gate)

    if method_name == "pair":
        with pytest.raises(
            BLEInterface.BLEError, match=ERROR_MANAGEMENT_TARGET_CHANGED
        ):
            iface.pair(confirm=True, await_timeout=7.0)
    else:
        with pytest.raises(
            BLEInterface.BLEError, match=ERROR_MANAGEMENT_TARGET_CHANGED
        ):
            iface.unpair(await_timeout=7.0)

    assert command_calls == []
    iface.close()


@pytest.mark.parametrize("method_name", ["pair", "unpair"])
def test_ble_interface_management_aborts_when_implicit_target_disappears_at_gate(
    monkeypatch: pytest.MonkeyPatch,
    method_name: str,
) -> None:
    """Implicit management ops should abort if the bound target disappears at the gate."""
    current_address = "AA:BB:CC:DD:EE:FF"
    client = DummyClient()
    client.address = current_address
    client.bleak_client = SimpleNamespace(address=current_address)
    iface = _build_interface(monkeypatch, client, start_receive_thread=False)
    command_calls: list[str] = []

    monkeypatch.setattr(client, "pair", lambda **_kwargs: command_calls.append("pair"))
    monkeypatch.setattr(
        client, "unpair", lambda **_kwargs: command_calls.append("unpair")
    )

    @contextlib.contextmanager
    def _clear_target_gate(_target_address: str) -> Iterator[None]:
        with iface._state_lock:
            cast(Any, iface).client = None
            iface.address = None
            iface._state_manager._reset_to_disconnected()
        yield

    monkeypatch.setattr(iface, "_management_target_gate", _clear_target_gate)

    if method_name == "pair":
        with pytest.raises(
            BLEInterface.BLEError, match=ERROR_MANAGEMENT_TARGET_CHANGED
        ):
            iface.pair(confirm=True, await_timeout=7.0)
    else:
        with pytest.raises(
            BLEInterface.BLEError, match=ERROR_MANAGEMENT_TARGET_CHANGED
        ):
            iface.unpair(await_timeout=7.0)

    assert command_calls == []
    iface.close()


def test_get_current_implicit_management_address_locked_returns_concrete_address(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Implicit management address helper should return concrete BLE address bindings."""
    iface = _build_interface(monkeypatch, DummyClient(), start_receive_thread=False)
    with iface._state_lock:
        iface.client = None
        iface.address = "AA:BB:CC:DD:EE:FF"
        assert iface._get_current_implicit_management_address_locked() == iface.address
    iface.close()


def test_revalidate_implicit_management_target_rejects_binding_mismatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Implicit target revalidation should fail when the binding changes while waiting."""
    iface = _build_interface(monkeypatch, DummyClient(), start_receive_thread=False)
    with iface._state_lock:
        iface.client = None
        iface.address = "mesh-node"

    with pytest.raises(BLEInterface.BLEError, match=ERROR_MANAGEMENT_TARGET_CHANGED):
        iface._revalidate_implicit_management_target(
            "AA:BB:CC:DD:EE:FF",
            expected_binding="different-node",
        )

    iface.close()


def test_execute_management_command_detects_disappearing_existing_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Management command path should abort if an addressless existing client disappears."""
    iface = _build_interface(monkeypatch, DummyClient(), start_receive_thread=False)
    addressless_client = cast(
        BLEClient,
        SimpleNamespace(
            isConnected=lambda: True,
            bleak_client=None,
            address=None,
        ),
    )
    call_count = 0

    def _get_management_client(_address: str | None) -> BLEClient | None:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return addressless_client
        return None

    monkeypatch.setattr(
        iface,
        "_get_management_client_if_available",
        _get_management_client,
    )

    with pytest.raises(BLEInterface.BLEError, match=ERROR_MANAGEMENT_TARGET_CHANGED):
        iface._execute_management_command(None, lambda _client: None)

    iface.close()


def test_execute_management_command_requires_resolved_target_address(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Management command path should fail when no target address can be resolved."""
    iface = _build_interface(monkeypatch, DummyClient(), start_receive_thread=False)
    monkeypatch.setattr(
        iface, "_get_management_client_if_available", lambda _address: None
    )
    monkeypatch.setattr(
        iface, "_resolve_target_address_for_management", lambda _address: None
    )

    with pytest.raises(BLEInterface.BLEError, match=ERROR_MANAGEMENT_ADDRESS_REQUIRED):
        iface._execute_management_command("mesh-node", lambda _client: None)

    iface.close()


@pytest.mark.parametrize("method_name", ["pair", "unpair"])
def test_ble_interface_management_rejects_blank_explicit_target(
    monkeypatch: pytest.MonkeyPatch,
    method_name: str,
) -> None:
    """pair()/unpair() should reject blank explicit targets before any resolution."""
    iface = _build_interface(monkeypatch, DummyClient(), start_receive_thread=False)

    with pytest.raises(BLEInterface.BLEError, match=ERROR_MANAGEMENT_ADDRESS_EMPTY):
        getattr(iface, method_name)("   ")

    iface.close()


@pytest.mark.parametrize("method_name", ["pair", "unpair"])
@pytest.mark.parametrize(
    "invalid_timeout",
    [None, 0.0, -1.0, float("nan"), float("inf"), True],
)
def test_ble_interface_management_rejects_unbounded_or_invalid_await_timeout(
    monkeypatch: pytest.MonkeyPatch,
    method_name: str,
    invalid_timeout: object,
) -> None:
    """pair()/unpair() should require a finite positive await_timeout."""
    client = DummyClient()
    iface = _build_interface(monkeypatch, client, start_receive_thread=False)

    with pytest.raises(
        BLEInterface.BLEError,
        match=re.escape(ERROR_MANAGEMENT_AWAIT_TIMEOUT_INVALID),
    ):
        getattr(iface, method_name)(await_timeout=invalid_timeout)

    assert client.pair_calls == 0
    assert client.unpair_calls == 0
    iface.close()


@pytest.mark.parametrize(
    ("method_name", "expected_error"),
    [
        ("pair", BLECLIENT_ERROR_CANNOT_PAIR_NOT_INITIALIZED),
        ("unpair", BLECLIENT_ERROR_CANNOT_UNPAIR_NOT_INITIALIZED),
    ],
)
def test_dummy_client_management_rejects_cleared_backend(
    method_name: str,
    expected_error: str,
) -> None:
    """DummyClient should mirror BLEClient management failures after backend teardown."""
    client = DummyClient()
    client.bleak_client = cast(Any, None)

    if method_name == "pair":
        with pytest.raises(BLEClient.BLEError, match=re.escape(expected_error)):
            client.pair(confirm=True)
    else:
        with pytest.raises(BLEClient.BLEError, match=re.escape(expected_error)):
            client.unpair()


def test_ble_interface_trust_rejects_blank_explicit_target_before_environment_checks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """trust() should reject blank targets before platform or tool validation."""
    iface = _build_interface(monkeypatch, DummyClient(), start_receive_thread=False)
    monkeypatch.setattr("meshtastic.interfaces.ble.interface.sys.platform", "darwin")
    monkeypatch.setattr(
        "meshtastic.interfaces.ble.interface.shutil.which",
        lambda _name: None,
    )
    monkeypatch.setattr(
        "meshtastic.interfaces.ble.interface.subprocess.run",
        lambda *_args, **_kwargs: pytest.fail("subprocess.run should not be reached"),
    )

    with pytest.raises(BLEInterface.BLEError, match=ERROR_MANAGEMENT_ADDRESS_EMPTY):
        iface.trust("   ")

    iface.close()


def test_ble_interface_trust_revalidates_implicit_target_after_gate_handoff(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """trust() should abort if the implicit management target changes at the gate."""
    current_address = "AA:BB:CC:DD:EE:FF"
    replacement_address = "11:22:33:44:55:66"
    client = DummyClient()
    client.address = current_address
    client.bleak_client = SimpleNamespace(address=current_address)
    iface = _build_interface(monkeypatch, client, start_receive_thread=False)

    replacement_client = DummyClient()
    replacement_client.address = replacement_address
    replacement_client.bleak_client = SimpleNamespace(address=replacement_address)

    @contextlib.contextmanager
    def _swap_target_gate(_target_address: str) -> Iterator[None]:
        with iface._state_lock:
            cast(Any, iface).client = replacement_client
            iface.address = replacement_address
            iface._state_manager._reset_to_disconnected()
            assert iface._state_manager._transition_to(ConnectionState.CONNECTING)
            assert iface._state_manager._transition_to(ConnectionState.CONNECTED)
        yield

    def _unexpected_run(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("subprocess.run should not be reached")

    _pin_trust_environment(monkeypatch, run=_unexpected_run)
    monkeypatch.setattr(iface, "_management_target_gate", _swap_target_gate)

    with pytest.raises(BLEInterface.BLEError, match=ERROR_MANAGEMENT_TARGET_CHANGED):
        iface.trust(timeout=7.0)

    iface.close()


@pytest.mark.parametrize("method_name", ["pair", "unpair", "trust"])
def test_ble_interface_management_allows_bound_name_when_target_stays_resolved(
    monkeypatch: pytest.MonkeyPatch,
    method_name: str,
) -> None:
    """Disconnected name-bound management ops should revalidate by resolving the same target."""
    target_name = "mesh-node"
    target_address = "AA:BB:CC:DD:EE:20"
    iface = _build_interface(monkeypatch, DummyClient(), start_receive_thread=False)
    with iface._state_lock:
        iface.client = None
        iface.address = target_name
        iface._state_manager._reset_to_disconnected()

    monkeypatch.setattr(
        iface,
        "findDevice",
        lambda identifier: _create_ble_device(target_address, str(identifier)),
    )

    if method_name == "trust":
        run_calls: list[tuple[tuple[object, ...], dict[str, object]]] = []

        def _run(*args: object, **kwargs: object) -> SimpleNamespace:
            run_calls.append((args, dict(kwargs)))
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        _pin_trust_environment(monkeypatch, run=_run)
        iface.trust(timeout=7.0)
        assert run_calls
    else:
        command_calls: list[str] = []
        temp_client = SimpleNamespace(
            pair=lambda **_kwargs: command_calls.append("pair"),
            unpair=lambda **_kwargs: command_calls.append("unpair"),
            bleak_client=SimpleNamespace(address=target_address),
        )
        cleanup_calls: list[object] = []

        monkeypatch.setattr(
            "meshtastic.interfaces.ble.interface.BLEClient",
            lambda _address, **_kwargs: temp_client,
        )
        monkeypatch.setattr(
            iface._client_manager,
            "_safe_close_client",
            lambda client: cleanup_calls.append(client),
        )

        if method_name == "pair":
            iface.pair(confirm=True, await_timeout=7.0)
        else:
            iface.unpair(await_timeout=7.0)
        assert command_calls == [method_name]
        assert cleanup_calls == [temp_client]

    iface.close()


@pytest.mark.parametrize("method_name", ["pair", "unpair", "trust"])
def test_ble_interface_management_requires_target_when_disconnected(
    monkeypatch: pytest.MonkeyPatch,
    method_name: str,
) -> None:
    """Management operations should not discover an arbitrary device when disconnected."""
    iface = _build_interface(monkeypatch, DummyClient(), start_receive_thread=False)
    with iface._state_lock:
        iface.client = None
        iface.address = None
        iface._state_manager._reset_to_disconnected()

    find_device_called = False

    def _unexpected_find_device(_address: str | None) -> BLEDevice:
        nonlocal find_device_called
        find_device_called = True
        return _create_ble_device("AA:BB:CC:DD:EE:FF", "Meshtastic")

    monkeypatch.setattr(iface, "findDevice", _unexpected_find_device)
    if method_name == "trust":
        _pin_trust_environment(monkeypatch)

    with pytest.raises(BLEInterface.BLEError, match="explicit address"):
        getattr(iface, method_name)()

    assert find_device_called is False
    iface.close()


@pytest.mark.parametrize("method_name", ["pair", "unpair", "trust"])
def test_ble_interface_management_rejects_connecting_state(
    monkeypatch: pytest.MonkeyPatch,
    method_name: str,
) -> None:
    """Management operations should refuse to run while a connect is in progress."""
    iface = _build_interface(monkeypatch, DummyClient(), start_receive_thread=False)
    with iface._state_lock:
        iface.client = None
        iface._state_manager._reset_to_disconnected()
        assert iface._state_manager._transition_to(ConnectionState.CONNECTING) is True

    if method_name == "trust":
        _pin_trust_environment(monkeypatch)

    with pytest.raises(BLEInterface.BLEError, match=ERROR_MANAGEMENT_CONNECTING):
        getattr(iface, method_name)("AA:BB:CC:DD:EE:FF")

    iface.close()


def test_ble_interface_resolve_management_address_prefers_connected_client_address(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Management target resolution should reuse the connected client's address."""
    client = DummyClient()
    client.address = "11:22:33:44:55:66"
    client.bleak_client = SimpleNamespace(address="AA:BB:CC:DD:EE:FF")
    iface = _build_interface(monkeypatch, client, start_receive_thread=False)

    assert iface._resolve_target_address_for_management(None) == "AA:BB:CC:DD:EE:FF"
    iface.close()


def test_ble_interface_resolve_management_address_rejects_blank_bound_target(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bound but blank management targets should fail fast."""
    iface = _build_interface(monkeypatch, DummyClient(), start_receive_thread=False)
    with iface._state_lock:
        iface.client = None
        iface.address = "   "
        iface._state_manager._reset_to_disconnected()

    with pytest.raises(BLEInterface.BLEError, match=ERROR_MANAGEMENT_ADDRESS_EMPTY):
        iface._resolve_target_address_for_management(None)

    iface.close()


def test_ble_interface_resolve_management_address_uses_existing_client_address(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Management target resolution should reuse a matching existing client address."""
    iface = _build_interface(monkeypatch, DummyClient(), start_receive_thread=False)
    with iface._state_lock:
        iface.client = None
        iface._state_manager._reset_to_disconnected()

    existing_client = DummyClient()
    existing_client.address = "11:22:33:44:55:66"
    existing_client.bleak_client = SimpleNamespace(address=None)
    monkeypatch.setattr(
        iface,
        "_get_existing_client_if_valid",
        lambda _request: cast(BLEClient, existing_client),
    )

    assert (
        iface._resolve_target_address_for_management("mesh-node") == "11:22:33:44:55:66"
    )
    iface.close()


def test_ble_interface_resolve_management_address_accepts_explicit_ble_address(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Explicit BLE addresses should bypass discovery resolution."""
    iface = _build_interface(monkeypatch, DummyClient(), start_receive_thread=False)
    with iface._state_lock:
        iface.client = None
        iface._state_manager._reset_to_disconnected()

    discovery_called = False

    def _unexpected_find_device(_address: str | None) -> BLEDevice:
        nonlocal discovery_called
        discovery_called = True
        return _create_ble_device("11:22:33:44:55:66", "Unexpected")

    monkeypatch.setattr(iface, "findDevice", _unexpected_find_device)

    assert (
        iface._resolve_target_address_for_management("AA-BB-CC-DD-EE-FF")
        == "AA-BB-CC-DD-EE-FF"
    )
    assert discovery_called is False
    iface.close()


def test_ble_interface_format_bluetoothctl_address_rejects_unresolved_input(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bluetoothctl formatting should fail for unresolved non-address identifiers."""
    iface = _build_interface(monkeypatch, DummyClient(), start_receive_thread=False)

    with pytest.raises(
        BLEInterface.BLEError,
        match=re.escape(ERROR_TRUST_ADDRESS_NOT_RESOLVED.format(address="mesh-node")),
    ):
        iface._format_bluetoothctl_address("mesh-node")

    iface.close()


def test_ble_interface_trust_includes_stdout_and_stderr_in_failure_details(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """trust() should include both stderr and stdout when bluetoothctl fails."""
    iface = _build_interface(monkeypatch, DummyClient(), start_receive_thread=False)
    with iface._state_lock:
        iface.client = None
        iface._state_manager._reset_to_disconnected()
    monkeypatch.setattr(
        iface,
        "findDevice",
        lambda _address: _create_ble_device("AA:BB:CC:DD:EE:FF", "Meshtastic"),
    )
    monkeypatch.setattr("meshtastic.interfaces.ble.interface.sys.platform", "linux")
    monkeypatch.setattr(
        "meshtastic.interfaces.ble.interface.shutil.which",
        lambda _name: "/usr/bin/bluetoothctl",
    )
    monkeypatch.setattr(
        "meshtastic.interfaces.ble.interface.subprocess.run",
        lambda *_args, **_kwargs: SimpleNamespace(
            returncode=1,
            stdout="generic output",
            stderr="specific failure",
        ),
    )

    with pytest.raises(BLEInterface.BLEError) as exc_info:
        iface.trust("mesh-node")

    detail = str(exc_info.value)
    assert "stderr: specific failure" in detail
    assert "stdout: generic output" in detail

    iface.close()


def test_ble_interface_trust_truncates_long_subprocess_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """trust() should truncate oversized subprocess snippets to a bounded length."""
    iface = _build_interface(monkeypatch, DummyClient(), start_receive_thread=False)
    with iface._state_lock:
        iface.client = None
        iface._state_manager._reset_to_disconnected()
    monkeypatch.setattr(
        iface,
        "findDevice",
        lambda _address: _create_ble_device("AA:BB:CC:DD:EE:FF", "Meshtastic"),
    )
    monkeypatch.setattr("meshtastic.interfaces.ble.interface.sys.platform", "linux")
    monkeypatch.setattr(
        "meshtastic.interfaces.ble.interface.shutil.which",
        lambda _name: "/usr/bin/bluetoothctl",
    )
    long_output = "long-output-segment " * 200
    monkeypatch.setattr(
        "meshtastic.interfaces.ble.interface.subprocess.run",
        lambda *_args, **_kwargs: SimpleNamespace(
            returncode=1,
            stdout="",
            stderr=long_output,
        ),
    )

    with pytest.raises(BLEInterface.BLEError) as exc_info:
        iface.trust("mesh-node")

    detail = str(exc_info.value)
    assert "stderr:" in detail
    assert "..." in detail

    iface.close()


def test_ble_interface_trust_runs_bluetoothctl(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """trust() should invoke bluetoothctl trust with a canonicalized address."""
    iface = _build_interface(monkeypatch, DummyClient(), start_receive_thread=False)
    with iface._state_lock:
        iface.client = None
        iface._state_manager._reset_to_disconnected()
    monkeypatch.setattr(
        iface,
        "findDevice",
        lambda _address: _create_ble_device("aa bb cc dd ee ff", "Meshtastic"),
    )
    monkeypatch.setattr("meshtastic.interfaces.ble.interface.sys.platform", "linux")
    monkeypatch.setattr(
        "meshtastic.interfaces.ble.interface.shutil.which",
        lambda _name: "/usr/bin/bluetoothctl",
    )

    run_calls: list[tuple[list[str], float]] = []

    def _fake_run(
        args: list[str],
        *,
        capture_output: bool,
        text: bool,
        check: bool,
        timeout: float,
    ) -> SimpleNamespace:
        _ = (capture_output, text, check)
        run_calls.append((args, timeout))
        return SimpleNamespace(returncode=0, stdout="succeeded", stderr="")

    monkeypatch.setattr(
        "meshtastic.interfaces.ble.interface.subprocess.run",
        _fake_run,
    )

    iface.trust("mesh-node", timeout=7.0)

    assert run_calls == [(["/usr/bin/bluetoothctl", "trust", "AA:BB:CC:DD:EE:FF"], 7.0)]
    iface.close()


def test_ble_interface_trust_rejects_non_linux(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """trust() should reject non-Linux hosts with a clear BLEError."""
    iface = _build_interface(monkeypatch, DummyClient(), start_receive_thread=False)
    monkeypatch.setattr("meshtastic.interfaces.ble.interface.sys.platform", "darwin")
    monkeypatch.setattr(
        "meshtastic.interfaces.ble.interface.shutil.which",
        lambda _name: pytest.fail("shutil.which should not be reached"),
    )
    monkeypatch.setattr(
        "meshtastic.interfaces.ble.interface.subprocess.run",
        lambda *_args, **_kwargs: pytest.fail("subprocess.run should not be reached"),
    )
    with pytest.raises(BLEInterface.BLEError, match="only supported on Linux"):
        iface.trust("AA:BB:CC:DD:EE:FF")
    iface.close()


@pytest.mark.parametrize(
    "invalid_timeout",
    [0, -1.0, float("nan"), float("inf"), float("-inf"), True, "7.0"],
)
def test_ble_interface_trust_rejects_invalid_timeout(
    monkeypatch: pytest.MonkeyPatch,
    invalid_timeout: object,
) -> None:
    """trust() should require a finite positive numeric timeout."""
    iface = _build_interface(monkeypatch, DummyClient(), start_receive_thread=False)
    _pin_trust_environment(monkeypatch)

    with pytest.raises(BLEInterface.BLEError, match=ERROR_TRUST_INVALID_TIMEOUT):
        iface.trust("AA:BB:CC:DD:EE:FF", timeout=cast(Any, invalid_timeout))

    iface.close()


def test_ble_interface_trust_requires_bluetoothctl_binary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """trust() should fail before spawning when bluetoothctl is unavailable."""
    iface = _build_interface(monkeypatch, DummyClient(), start_receive_thread=False)
    monkeypatch.setattr("meshtastic.interfaces.ble.interface.sys.platform", "linux")
    monkeypatch.setattr(
        "meshtastic.interfaces.ble.interface.shutil.which",
        lambda _name: None,
    )
    monkeypatch.setattr(
        "meshtastic.interfaces.ble.interface.subprocess.run",
        lambda *_args, **_kwargs: pytest.fail("subprocess.run should not be reached"),
    )

    with pytest.raises(BLEInterface.BLEError, match=ERROR_TRUST_BLUETOOTHCTL_MISSING):
        iface.trust("AA:BB:CC:DD:EE:FF")

    iface.close()


def test_ble_interface_trust_translates_subprocess_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """trust() should translate bluetoothctl timeouts into BLEError."""
    iface = _build_interface(monkeypatch, DummyClient(), start_receive_thread=False)
    monkeypatch.setattr("meshtastic.interfaces.ble.interface.sys.platform", "linux")
    monkeypatch.setattr(
        "meshtastic.interfaces.ble.interface.shutil.which",
        lambda _name: "/usr/bin/bluetoothctl",
    )

    def _raise_timeout(*_args: object, **_kwargs: object) -> SimpleNamespace:
        raise subprocess.TimeoutExpired(
            cmd=["/usr/bin/bluetoothctl", "trust", "AA:BB:CC:DD:EE:FF"],
            timeout=2.5,
        )

    monkeypatch.setattr(
        "meshtastic.interfaces.ble.interface.subprocess.run",
        _raise_timeout,
    )

    with pytest.raises(
        BLEInterface.BLEError,
        match=re.escape(
            ERROR_TRUST_COMMAND_TIMEOUT.format(timeout=2.5, address="AA:BB:CC:DD:EE:FF")
        ),
    ):
        iface.trust("AA:BB:CC:DD:EE:FF", timeout=2.5)

    iface.close()


def test_ble_interface_trust_translates_spawn_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """trust() should translate subprocess spawn failures into BLEError."""
    iface = _build_interface(monkeypatch, DummyClient(), start_receive_thread=False)
    monkeypatch.setattr("meshtastic.interfaces.ble.interface.sys.platform", "linux")
    monkeypatch.setattr(
        "meshtastic.interfaces.ble.interface.shutil.which",
        lambda _name: "/usr/bin/bluetoothctl",
    )

    def _raise_os_error(*_args: object, **_kwargs: object) -> SimpleNamespace:
        raise OSError("permission denied")

    monkeypatch.setattr(
        "meshtastic.interfaces.ble.interface.subprocess.run",
        _raise_os_error,
    )

    with pytest.raises(
        BLEInterface.BLEError,
        match=re.escape(
            ERROR_TRUST_COMMAND_FAILED.format(
                address="AA:BB:CC:DD:EE:FF",
                detail="/usr/bin/bluetoothctl: permission denied",
            )
        ),
    ):
        iface.trust("AA:BB:CC:DD:EE:FF", timeout=2.5)

    iface.close()


def test_ble_interface_trust_rejects_closing_interface(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """trust() should fail before resolution or subprocess work once shutdown starts."""
    iface = _build_interface(monkeypatch, DummyClient(), start_receive_thread=False)
    with iface._state_lock:
        iface.client = None
        iface._closed = True

    find_device_called = False
    subprocess_called = False

    def _unexpected_find_device(_address: str | None) -> BLEDevice:
        nonlocal find_device_called
        find_device_called = True
        return _create_ble_device("AA:BB:CC:DD:EE:FF", "Meshtastic")

    def _unexpected_run(*_args: object, **_kwargs: object) -> SimpleNamespace:
        nonlocal subprocess_called
        subprocess_called = True
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(iface, "findDevice", _unexpected_find_device)
    monkeypatch.setattr("meshtastic.interfaces.ble.interface.sys.platform", "linux")
    monkeypatch.setattr(
        "meshtastic.interfaces.ble.interface.shutil.which",
        lambda _name: "/usr/bin/bluetoothctl",
    )
    monkeypatch.setattr(
        "meshtastic.interfaces.ble.interface.subprocess.run",
        _unexpected_run,
    )

    try:
        with pytest.raises(BLEInterface.BLEError, match="closing"):
            iface.trust("mesh-node")

        assert find_device_called is False
        assert subprocess_called is False
    finally:
        with iface._state_lock:
            iface._closed = False
        iface.close()


def test_ble_interface_trust_does_not_hold_interface_locks_during_subprocess(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """trust() should let close() mark shutdown before bluetoothctl returns."""
    iface = _build_interface(monkeypatch, DummyClient(), start_receive_thread=False)
    trust_target = "AA:BB:CC:DD:EE:FF"
    with iface._state_lock:
        assert iface.client is not None
        active_client = cast(DummyClient, iface.client)
        active_client.address = trust_target
        active_client.bleak_client = SimpleNamespace(address=trust_target)
        iface.address = trust_target
    run_started = threading.Event()
    allow_run_return = threading.Event()
    close_done = threading.Event()
    close_started = threading.Event()
    trust_errors: list[Exception] = []
    close_errors: list[Exception] = []

    def _blocking_run(*_args: object, **_kwargs: object) -> SimpleNamespace:
        run_started.set()
        assert allow_run_return.wait(timeout=1.0)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    _pin_trust_environment(monkeypatch, run=_blocking_run)
    management_wait_entered = _capture_management_wait_event(monkeypatch, iface)

    def _run_trust() -> None:
        try:
            iface.trust(trust_target, timeout=7.0)
        except (
            Exception
        ) as exc:  # pragma: no cover - failure captured below  # noqa: BLE001 - test captures thread errors
            trust_errors.append(exc)

    def _close_iface() -> None:
        try:
            close_started.set()
            iface.close()
        except (
            Exception
        ) as exc:  # pragma: no cover - failure captured below  # noqa: BLE001 - test captures thread errors
            close_errors.append(exc)
        finally:
            close_done.set()

    trust_thread = threading.Thread(target=_run_trust, daemon=True)
    trust_thread.start()
    assert run_started.wait(timeout=1.0)

    close_thread = threading.Thread(target=_close_iface, daemon=True)
    close_thread.start()
    assert close_started.wait(timeout=1.0)
    assert management_wait_entered.wait(timeout=1.0)
    with iface._state_lock:
        assert iface._closed is True
    assert close_done.is_set() is False

    allow_run_return.set()
    trust_thread.join(timeout=2.0)
    close_thread.join(timeout=2.0)

    assert not trust_thread.is_alive()
    assert not close_thread.is_alive()
    assert close_done.is_set() is True
    assert trust_errors == []
    assert close_errors == []
    with iface._state_lock:
        assert iface._closed is True


def test_ble_interface_close_waits_for_explicit_trust_without_active_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """close() should wait for explicit trust() even when no client is active."""
    iface = _build_interface(monkeypatch, DummyClient(), start_receive_thread=False)
    with iface._state_lock:
        iface.client = None
        iface.address = None
        iface._state_manager._reset_to_disconnected()

    run_started = threading.Event()
    allow_run_return = threading.Event()
    trust_errors: list[Exception] = []
    close_errors: list[Exception] = []
    close_done = threading.Event()

    def _blocking_run(*_args: object, **_kwargs: object) -> SimpleNamespace:
        run_started.set()
        assert allow_run_return.wait(timeout=1.0)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    _pin_trust_environment(monkeypatch, run=_blocking_run)
    management_wait_entered = _capture_management_wait_event(monkeypatch, iface)

    def _run_trust() -> None:
        try:
            iface.trust("AA:BB:CC:DD:EE:FF", timeout=7.0)
        except (
            Exception
        ) as exc:  # pragma: no cover - failure captured below  # noqa: BLE001 - test captures thread errors
            trust_errors.append(exc)

    def _run_close() -> None:
        try:
            iface.close()
        except (
            Exception
        ) as exc:  # pragma: no cover - failure captured below  # noqa: BLE001 - test captures thread errors
            close_errors.append(exc)
        finally:
            close_done.set()

    trust_thread = threading.Thread(target=_run_trust, daemon=True)
    trust_thread.start()
    assert run_started.wait(timeout=1.0)

    close_thread = threading.Thread(target=_run_close, daemon=True)
    close_thread.start()
    assert management_wait_entered.wait(timeout=1.0)
    with iface._state_lock:
        assert iface._closed is True
    assert close_done.is_set() is False

    allow_run_return.set()
    trust_thread.join(timeout=2.0)
    close_thread.join(timeout=2.0)

    assert not trust_thread.is_alive()
    assert not close_thread.is_alive()
    assert trust_errors == []
    assert close_errors == []
    assert close_done.is_set() is True


def test_ble_interface_close_skips_management_gate_after_wait_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """close() should not block on the per-target gate after management wait timeout."""
    client = DummyClient()
    client.address = "AA:BB:CC:DD:EE:21"
    client.bleak_client = SimpleNamespace(address=client.address)
    iface = _build_interface(monkeypatch, client, start_receive_thread=False)
    gate_calls: list[str] = []
    unsubscribe_calls: list[object] = []
    disconnect_calls: list[object] = []

    with iface._management_lock:
        iface._management_inflight = 1

    monkeypatch.setattr(
        iface._management_idle_condition,
        "wait",
        lambda timeout=None: False,
        raising=True,
    )

    def _unexpected_management_gate(
        _address: str,
    ) -> contextlib.AbstractContextManager[None]:
        gate_calls.append(_address)
        return contextlib.nullcontext()

    monkeypatch.setattr(
        iface,
        "_management_target_gate",
        _unexpected_management_gate,
        raising=True,
    )
    monkeypatch.setattr(
        "meshtastic.interfaces.ble.interface.MeshInterface.close",
        lambda _self: None,
    )
    monkeypatch.setattr(
        iface._notification_manager,
        "_unsubscribe_all",
        lambda active_client, timeout=None: unsubscribe_calls.append(active_client),
        raising=True,
    )
    monkeypatch.setattr(
        iface,
        "_disconnect_and_close_client",
        lambda active_client: disconnect_calls.append(active_client),
        raising=True,
    )
    monkeypatch.setattr(
        iface._notification_manager,
        "_cleanup_all",
        lambda: None,
        raising=True,
    )

    iface.close()

    assert gate_calls == []
    assert unsubscribe_calls == [client]
    assert disconnect_calls == [client]


def test_ble_interface_close_bounds_wait_on_spurious_management_wakeups(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """close() should enforce shutdown timeout despite spurious management wakeups."""
    client = DummyClient()
    client.address = "AA:BB:CC:DD:EE:31"
    client.bleak_client = SimpleNamespace(address=client.address)
    iface = _build_interface(monkeypatch, client, start_receive_thread=False)
    wait_calls: list[float | None] = []
    gate_calls: list[str] = []
    unsubscribe_calls: list[object] = []
    disconnect_calls: list[object] = []
    close_errors: list[Exception] = []
    close_done = threading.Event()

    with iface._management_lock:
        iface._management_inflight = 1
    with iface._state_lock:
        iface._disconnect_notified = True

    monkeypatch.setattr(
        "meshtastic.interfaces.ble.interface._MANAGEMENT_SHUTDOWN_WAIT_TIMEOUT_SECONDS",
        0.05,
    )
    monkeypatch.setattr(
        "meshtastic.interfaces.ble.interface._MANAGEMENT_CONNECT_WAIT_POLL_SECONDS",
        0.005,
    )

    def _spurious_wait(timeout: float | None = None) -> bool:
        """
        Simulate a spurious wait for tests and track each requested timeout.

        Parameters
        ----------
            timeout (float | None): The requested wait duration in seconds; if greater than zero the function sleeps for that duration.

        Returns
        -------
            bool: `True` to indicate the wait condition remains satisfied.

        Raises
        ------
            AssertionError: If the number of invocations exceeds the allowed spurious-wait budget, signals that shutdown waited past its timeout.
        """
        wait_calls.append(timeout)
        if timeout is not None and timeout > 0:
            time.sleep(timeout)
        if len(wait_calls) > _MAX_SPURIOUS_CLOSE_WAIT_CALLS_BEFORE_FAIL:
            close_done.set()
            raise AssertionError(
                "close() kept waiting past the shutdown timeout budget"
            )
        return True

    monkeypatch.setattr(
        iface._management_idle_condition,
        "wait",
        _spurious_wait,
        raising=True,
    )

    def _management_gate(
        address: str,
    ) -> contextlib.AbstractContextManager[None]:
        """
        Context manager used in tests to record a requested management address and provide a no-op gate.

        Parameters
        ----------
            address (str): The management address being requested; the address is recorded for test inspection.

        Returns
        -------
            contextmanager: A context manager that performs no gating and yields `None`.
        """
        gate_calls.append(address)
        return contextlib.nullcontext()

    monkeypatch.setattr(
        iface, "_management_target_gate", _management_gate, raising=True
    )
    monkeypatch.setattr(
        "meshtastic.interfaces.ble.interface.MeshInterface.close",
        lambda _self: None,
    )
    monkeypatch.setattr(
        iface._notification_manager,
        "_unsubscribe_all",
        lambda active_client, timeout=None: unsubscribe_calls.append(active_client),
        raising=True,
    )
    monkeypatch.setattr(
        iface,
        "_disconnect_and_close_client",
        lambda active_client: disconnect_calls.append(active_client),
        raising=True,
    )
    monkeypatch.setattr(
        iface._notification_manager,
        "_cleanup_all",
        lambda: None,
        raising=True,
    )

    def _run_close() -> None:
        """
        Attempt to close the interface and signal completion.

        Calls iface.close(). If an exception is raised, appends it to the shared close_errors list. Always sets the close_done event when finished.
        """
        try:
            iface.close()
        except Exception as exc:  # pragma: no cover - captured for assertion
            close_errors.append(exc)
        finally:
            close_done.set()

    with caplog.at_level(logging.WARNING):
        close_thread = threading.Thread(target=_run_close, daemon=True)
        close_thread.start()
        close_thread.join(timeout=1.0)

    assert not close_thread.is_alive()
    assert close_done.is_set() is True
    assert close_errors == []
    assert wait_calls
    assert gate_calls == []
    assert unsubscribe_calls == [client]
    assert disconnect_calls == [client]
    assert any("Timed out waiting" in record.message for record in caplog.records)


def test_ble_interface_implicit_trust_releases_connect_lock_before_subprocess(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify implicit trust() releases connect lock before blocking subprocess.

    Parameters
    ----------
    monkeypatch : pytest.MonkeyPatch
        Fixture used to patch trust subprocess behavior.

    Returns
    -------
    None
    """
    iface = _build_interface(monkeypatch, DummyClient(), start_receive_thread=False)
    trust_target = "AA:BB:CC:DD:EE:FF"
    with iface._state_lock:
        assert iface.client is not None
        active_client = cast(DummyClient, iface.client)
        active_client.address = trust_target
        active_client.bleak_client = SimpleNamespace(address=trust_target)
        iface.address = trust_target

    run_started = threading.Event()
    allow_run_return = threading.Event()
    trust_errors: list[Exception] = []

    def _blocking_run(*_args: object, **_kwargs: object) -> SimpleNamespace:
        run_started.set()
        assert allow_run_return.wait(timeout=1.0)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    _pin_trust_environment(monkeypatch, run=_blocking_run)

    def _run_trust() -> None:
        try:
            iface.trust(timeout=7.0)
        except (
            Exception
        ) as exc:  # pragma: no cover - failure captured below  # noqa: BLE001 - test captures thread errors
            trust_errors.append(exc)

    trust_thread = threading.Thread(target=_run_trust, daemon=True)
    trust_thread.start()
    try:
        assert run_started.wait(timeout=1.0)

        assert iface._connect_lock.acquire(blocking=False) is True
        iface._connect_lock.release()
    finally:
        allow_run_return.set()
        if trust_thread.is_alive():
            trust_thread.join(timeout=2.0)

    assert not trust_thread.is_alive()
    assert trust_errors == []
    assert iface._connect_lock.acquire(blocking=False) is True
    iface._connect_lock.release()

    iface.close()


def test_ble_interface_close_serializes_with_management_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """close() should not mark the interface closed while a management op holds the lock."""
    iface = _build_interface(monkeypatch, DummyClient(), start_receive_thread=False)
    close_done = threading.Event()
    close_started = threading.Event()
    close_errors: list[Exception] = []

    def _close_iface() -> None:
        try:
            close_started.set()
            iface.close()
        except (
            Exception
        ) as exc:  # pragma: no cover - failure captured below  # noqa: BLE001 - test captures thread errors
            close_errors.append(exc)
        finally:
            close_done.set()

    with iface._management_lock:
        close_thread = threading.Thread(target=_close_iface, daemon=True)
        close_thread.start()
        assert close_started.wait(timeout=1.0)
        with iface._state_lock:
            assert iface._closed is False
        assert close_done.is_set() is False

    close_thread.join(timeout=2.0)
    assert close_errors == []
    assert close_done.is_set() is True


def test_ble_interface_close_does_not_wait_for_connect_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """close() should still start shutdown while the connect lock is held."""
    iface = _build_interface(monkeypatch, DummyClient(), start_receive_thread=False)
    close_done = threading.Event()
    close_errors: list[Exception] = []

    def _close_iface() -> None:
        try:
            iface.close()
        except (
            Exception
        ) as exc:  # pragma: no cover - failure captured below  # noqa: BLE001 - test captures thread errors
            close_errors.append(exc)
        finally:
            close_done.set()

    with iface._connect_lock:
        close_thread = threading.Thread(target=_close_iface, daemon=True)
        close_thread.start()
        assert close_done.wait(timeout=1.0)
        with iface._state_lock:
            assert iface._closed is True

    close_thread.join(timeout=2.0)
    assert close_errors == []
    assert close_done.is_set() is True


def test_ble_interface_pair_waits_for_connect_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """pair() should serialize behind the interface connect lock."""
    iface = _build_interface(monkeypatch, DummyClient(), start_receive_thread=False)
    real_connect_lock = iface._connect_lock
    with iface._state_lock:
        iface.client = None
        iface._state_manager._reset_to_disconnected()
    monkeypatch.setattr(
        iface,
        "findDevice",
        lambda _address: _create_ble_device("AA:BB:CC:DD:EE:FF", "Meshtastic"),
    )

    pair_kwargs: list[dict[str, object]] = []
    pair_await_timeouts: list[float | None] = []
    close_calls: list[object] = []
    pair_finished = threading.Event()
    pair_thread_started = threading.Event()
    temp_client_created = threading.Event()
    allow_temp_client_creation = threading.Event()
    connect_lock_attempted = threading.Event()

    class _ObservedConnectLock:
        def __enter__(self) -> "_ObservedConnectLock":
            connect_lock_attempted.set()
            real_connect_lock.acquire()
            return self

        def __exit__(
            self,
            exc_type: type[BaseException] | None,
            exc: BaseException | None,
            tb: TracebackType | None,
        ) -> Literal[False]:
            _ = (exc_type, exc, tb)
            real_connect_lock.release()
            return False

    def _pair(*, await_timeout: float | None = None, **kwargs: object) -> None:
        pair_kwargs.append(dict(kwargs))
        pair_await_timeouts.append(await_timeout)
        pair_finished.set()

    temp_client = SimpleNamespace(
        pair=_pair,
        bleak_client=SimpleNamespace(address="AA:BB:CC:DD:EE:FF"),
    )

    def _temp_client_factory(_address: str, **_kwargs: object) -> SimpleNamespace:
        assert allow_temp_client_creation.wait(timeout=1.0)
        temp_client_created.set()
        return temp_client

    def _run_pair() -> None:
        pair_thread_started.set()
        iface.pair("mesh-node", confirm=True, await_timeout=7.0)

    monkeypatch.setattr(
        "meshtastic.interfaces.ble.interface.BLEClient",
        _temp_client_factory,
    )
    monkeypatch.setattr(
        iface._client_manager,
        "_safe_close_client",
        lambda client: close_calls.append(client),
    )
    monkeypatch.setattr(iface, "_connect_lock", _ObservedConnectLock())

    with real_connect_lock:
        pair_thread = threading.Thread(target=_run_pair, daemon=True)
        pair_thread.start()
        assert pair_thread_started.wait(timeout=1.0)
        assert connect_lock_attempted.wait(timeout=1.0)
        assert pair_kwargs == []
        assert pair_finished.is_set() is False
        allow_temp_client_creation.set()

    pair_thread.join(timeout=2.0)
    assert not pair_thread.is_alive()
    assert temp_client_created.is_set() is True
    assert pair_kwargs == [{"confirm": True}]
    assert pair_await_timeouts == [7.0]
    assert close_calls == [temp_client]
    assert pair_finished.is_set() is True
    iface.close()


def test_ble_interface_pair_waits_for_address_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """pair() should serialize temporary management work with the address gate."""
    iface = _build_interface(monkeypatch, DummyClient(), start_receive_thread=False)
    with iface._state_lock:
        iface.client = None
        iface._state_manager._reset_to_disconnected()
    monkeypatch.setattr(
        iface,
        "findDevice",
        lambda _address: _create_ble_device("AA:BB:CC:DD:EE:FF", "Meshtastic"),
    )

    pair_kwargs: list[dict[str, object]] = []
    pair_await_timeouts: list[float | None] = []
    close_calls: list[object] = []
    pair_finished = threading.Event()
    pair_thread_started = threading.Event()
    temp_client_created = threading.Event()
    allow_temp_client_creation = threading.Event()
    addr_gate_attempted = threading.Event()

    class _ObservedAddressLock:
        def __init__(self) -> None:
            self._lock = threading.RLock()

        def __enter__(self) -> "_ObservedAddressLock":
            addr_gate_attempted.set()
            self._lock.acquire()
            return self

        def __exit__(
            self,
            exc_type: type[BaseException] | None,
            exc: BaseException | None,
            tb: TracebackType | None,
        ) -> Literal[False]:
            _ = (exc_type, exc, tb)
            self._lock.release()
            return False

    observed_address_lock = _ObservedAddressLock()

    @contextlib.contextmanager
    def _observed_addr_lock_context(
        _addr: str | None,
    ) -> Iterator[_ObservedAddressLock]:
        with observed_address_lock:
            yield observed_address_lock

    def _pair(*, await_timeout: float | None = None, **kwargs: object) -> None:
        pair_kwargs.append(dict(kwargs))
        pair_await_timeouts.append(await_timeout)
        pair_finished.set()

    temp_client = SimpleNamespace(
        pair=_pair,
        bleak_client=SimpleNamespace(address="AA:BB:CC:DD:EE:FF"),
    )

    def _temp_client_factory(_address: str, **_kwargs: object) -> SimpleNamespace:
        assert allow_temp_client_creation.wait(timeout=1.0)
        temp_client_created.set()
        return temp_client

    def _run_pair() -> None:
        pair_thread_started.set()
        iface.pair("mesh-node", confirm=True, await_timeout=7.0)

    monkeypatch.setattr(
        "meshtastic.interfaces.ble.interface.BLEClient",
        _temp_client_factory,
    )
    monkeypatch.setattr(
        iface._client_manager,
        "_safe_close_client",
        lambda client: close_calls.append(client),
    )
    monkeypatch.setattr(
        "meshtastic.interfaces.ble.interface._addr_lock_context",
        _observed_addr_lock_context,
    )

    with observed_address_lock:
        pair_thread = threading.Thread(target=_run_pair, daemon=True)
        pair_thread.start()
        assert pair_thread_started.wait(timeout=1.0)
        assert addr_gate_attempted.wait(timeout=1.0)
        assert pair_kwargs == []
        assert pair_finished.is_set() is False
        allow_temp_client_creation.set()

    pair_thread.join(timeout=2.0)
    assert not pair_thread.is_alive()
    assert temp_client_created.is_set() is True
    assert pair_kwargs == [{"confirm": True}]
    assert pair_await_timeouts == [7.0]
    assert close_calls == [temp_client]
    assert pair_finished.is_set() is True
    iface.close()


def test_ble_interface_close_logs_when_shutdown_already_in_progress(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """close() should log when cleanup continues from an already-closing state."""
    iface = _build_interface(monkeypatch, DummyClient(), start_receive_thread=False)
    with iface._state_lock:
        iface._state_manager._reset_to_disconnected()
        assert iface._state_manager._transition_to(ConnectionState.CONNECTING) is True
        assert iface._state_manager._transition_to(ConnectionState.CONNECTED) is True
        assert (
            iface._state_manager._transition_to(ConnectionState.DISCONNECTING) is True
        )

    with caplog.at_level(logging.DEBUG):
        iface.close()

    assert "another shutdown is in progress" in caplog.text


def test_ble_interface_connect_uses_pair_override_for_orchestrator(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """connect() should forward pair and timeout overrides to connection orchestration."""
    iface = _build_minimal_connect_test_interface()

    monkeypatch.setattr(iface, "_validate_connection_preconditions", lambda: None)
    monkeypatch.setattr(iface, "_get_existing_client_if_valid", lambda _req: None)
    monkeypatch.setattr(iface, "_raise_if_duplicate_connect", lambda _key: None)
    monkeypatch.setattr(iface, "_finalize_connection_gates", lambda *_args: None)
    connected_callbacks: list[bool] = []
    monkeypatch.setattr(iface, "_connected", lambda: connected_callbacks.append(True))

    captured_pair_flags: list[bool] = []
    captured_timeouts: list[float | None] = []

    def _establish_stub(
        address: str | None,
        normalized_request: str | None,
        address_key: str | None,
        *,
        pair_on_connect: bool = False,
        connect_timeout: float | None = None,
    ) -> tuple[DummyClient, str | None, str | None]:
        _ = (address, normalized_request, address_key)
        client = DummyClient()
        captured_pair_flags.append(pair_on_connect)
        captured_timeouts.append(connect_timeout)
        with iface._state_lock:
            cast(Any, iface).client = client
            iface.address = client.address
            iface._state_manager._reset_to_disconnected()
            assert iface._state_manager._transition_to(ConnectionState.CONNECTING)
            assert iface._state_manager._transition_to(ConnectionState.CONNECTED)
        return client, None, None

    monkeypatch.setattr(iface, "_establish_and_update_client", _establish_stub)

    iface.connect(pair=True, connect_timeout=4.5)
    iface.connect(pair=False)
    iface.pair_on_connect = True
    iface.connect()

    assert captured_pair_flags == [True, False, True]
    assert captured_timeouts == [4.5, None, None]
    assert connected_callbacks == [True, True, True]


def test_connect_wraps_invalid_connect_timeout_as_ble_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """connect() should wrap invalid timeout overrides as BLEError."""
    iface = _build_minimal_connect_test_interface()
    cast(Any, iface)._connection_orchestrator = SimpleNamespace(
        _establish_connection=lambda *_args, **_kwargs: pytest.fail(
            "_establish_connection should not be called for invalid connect_timeout"
        )
    )

    monkeypatch.setattr(iface, "_validate_connection_preconditions", lambda: None)
    monkeypatch.setattr(iface, "_get_existing_client_if_valid", lambda _req: None)
    monkeypatch.setattr(iface, "_raise_if_duplicate_connect", lambda _key: None)

    with pytest.raises(
        BLEInterface.BLEError,
        match=re.escape(
            "invalid connect_timeout: connect_timeout must be a finite positive number of seconds."
        ),
    ):
        iface.connect("AA:BB:CC:DD:EE:10", connect_timeout=cast(Any, 0.0))


def test_validate_connect_timeout_override_rejects_non_numeric_values() -> None:
    """_validate_connect_timeout_override should wrap non-numeric overrides as BLEError."""
    iface = _build_minimal_connect_test_interface()
    with pytest.raises(BLEInterface.BLEError, match="invalid connect_timeout"):
        iface._validate_connect_timeout_override(
            cast(object, "invalid-timeout"),
            pair_on_connect=False,
        )


def test_finish_management_operation_clamps_underflow(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """_finish_management_operation() should clamp negative accounting to zero."""
    iface = _build_minimal_connect_test_interface()
    with iface._management_lock:
        iface._management_inflight = -1
    notify_calls: list[bool] = []
    monkeypatch.setattr(
        iface._management_idle_condition,
        "notify_all",
        lambda: notify_calls.append(True),
        raising=True,
    )

    with caplog.at_level(logging.WARNING):
        iface._finish_management_operation()

    with iface._management_lock:
        assert iface._management_inflight == 0
    assert notify_calls == [True]
    assert any("underflow" in record.message.lower() for record in caplog.records)


def test_finish_management_operation_notifies_when_count_reaches_zero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """_finish_management_operation() should notify waiters on zero transition."""
    iface = _build_minimal_connect_test_interface()
    with iface._management_lock:
        iface._management_inflight = 1
    notify_calls: list[bool] = []
    monkeypatch.setattr(
        iface._management_idle_condition,
        "notify_all",
        lambda: notify_calls.append(True),
        raising=True,
    )

    iface._finish_management_operation()

    with iface._management_lock:
        assert iface._management_inflight == 0
    assert notify_calls == [True]


def test_finish_management_operation_does_not_notify_above_zero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """_finish_management_operation() should not notify while inflight remains positive."""
    iface = _build_minimal_connect_test_interface()
    with iface._management_lock:
        iface._management_inflight = 2
    notify_calls: list[bool] = []
    monkeypatch.setattr(
        iface._management_idle_condition,
        "notify_all",
        lambda: notify_calls.append(True),
        raising=True,
    )

    iface._finish_management_operation()

    with iface._management_lock:
        assert iface._management_inflight == 1
    assert notify_calls == []


@pytest.mark.unit
def test_connect_rejects_non_bool_pair_override() -> None:
    """connect() should fail fast when `pair` is not explicitly bool/None."""
    iface = _build_minimal_connect_test_interface()
    with pytest.raises(BLEInterface.BLEError, match="pair must be a bool"):
        iface.connect("AA:BB:CC:DD:EE:10", pair=cast(Any, "false"))


@pytest.mark.unit
def test_connect_waits_for_inflight_management_before_establishing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """connect() should wait until in-flight management operations finish."""
    iface = _build_minimal_connect_test_interface()
    iface._management_lock = threading.RLock()
    iface._management_idle_condition = threading.Condition(iface._management_lock)
    iface._management_inflight = 1

    wait_calls: list[bool] = []
    establish_calls: list[bool] = []

    def _wait_for_management(timeout: float | None = None) -> bool:
        _ = timeout
        wait_calls.append(True)
        iface._management_inflight = 0
        return True

    monkeypatch.setattr(
        iface._management_idle_condition,
        "wait",
        _wait_for_management,
        raising=True,
    )
    monkeypatch.setattr(iface, "_validate_connection_preconditions", lambda: None)
    monkeypatch.setattr(iface, "_get_existing_client_if_valid", lambda _req: None)
    monkeypatch.setattr(iface, "_raise_if_duplicate_connect", lambda _key: None)
    monkeypatch.setattr(iface, "_finalize_connection_gates", lambda *_args: None)
    monkeypatch.setattr(
        iface,
        "_verify_and_publish_connected",
        lambda *_args, **_kwargs: None,
    )

    def _establish_stub(
        _address: str | None,
        _normalized_request: str | None,
        _address_key: str | None,
        *,
        pair_on_connect: bool = False,
        connect_timeout: float | None = None,
    ) -> tuple[DummyClient, str | None, str | None]:
        _ = (pair_on_connect, connect_timeout)
        establish_calls.append(True)
        client = DummyClient()
        with iface._state_lock:
            cast(Any, iface).client = client
            iface.address = client.address
            iface._state_manager._reset_to_disconnected()
            assert iface._state_manager._transition_to(ConnectionState.CONNECTING)
            assert iface._state_manager._transition_to(ConnectionState.CONNECTED)
        return client, None, None

    monkeypatch.setattr(iface, "_establish_and_update_client", _establish_stub)

    iface.connect("AA:BB:CC:DD:EE:10")

    assert wait_calls == [True]
    assert establish_calls == [True]


def test_connect_returns_preexisting_client_before_resolving_target(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """connect() should return immediately when a preexisting client already satisfies the request."""
    iface = _build_minimal_connect_test_interface()
    existing_client = cast(BLEClient, DummyClient())
    monkeypatch.setattr(iface, "_validate_connection_preconditions", lambda: None)
    monkeypatch.setattr(
        iface, "_get_existing_client_if_valid", lambda _request: existing_client
    )
    monkeypatch.setattr(
        iface,
        "_resolve_target_address_for_connect",
        lambda _identifier: pytest.fail("resolution should not run"),
    )

    assert iface.connect("AA:BB:CC:DD:EE:10") is existing_client


def test_connect_times_out_waiting_for_management_operations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """connect() should abort when inflight management never drains."""
    iface = _build_minimal_connect_test_interface()
    iface._management_lock = threading.RLock()
    iface._management_idle_condition = threading.Condition(iface._management_lock)
    iface._management_inflight = 1

    monotonic_values = iter([0.0, 999.0])
    monkeypatch.setattr(
        "meshtastic.interfaces.ble.interface.time.monotonic",
        lambda: next(monotonic_values),
    )
    monkeypatch.setattr(
        iface._management_idle_condition,
        "wait",
        lambda timeout=None: False,
        raising=True,
    )
    monkeypatch.setattr(iface, "_validate_connection_preconditions", lambda: None)
    monkeypatch.setattr(iface, "_get_existing_client_if_valid", lambda _request: None)

    with pytest.raises(BLEInterface.BLEError, match=ERROR_MANAGEMENT_CONNECTING):
        iface.connect("AA:BB:CC:DD:EE:10")


def test_connect_times_out_on_spurious_management_wakeups(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """connect() should enforce timeout even if management wait wakes spuriously."""
    iface = _build_minimal_connect_test_interface()
    iface._management_lock = threading.RLock()
    iface._management_idle_condition = threading.Condition(iface._management_lock)
    iface._management_inflight = 1
    wait_calls: list[float | None] = []
    fake_time = 0.0

    monkeypatch.setattr(
        "meshtastic.interfaces.ble.interface._MANAGEMENT_CONNECT_WAIT_TIMEOUT_SECONDS",
        0.03,
    )
    monkeypatch.setattr(
        "meshtastic.interfaces.ble.interface._MANAGEMENT_CONNECT_WAIT_POLL_SECONDS",
        0.005,
    )

    def _monotonic() -> float:
        """
        Advance and return a test monotonic timestamp by 0.01 seconds.

        Returns
        -------
            Current monotonic time in seconds; the returned value increases by 0.01 on each call.
        """
        nonlocal fake_time
        fake_time += 0.01
        return fake_time

    monkeypatch.setattr(
        "meshtastic.interfaces.ble.interface.time.monotonic", _monotonic
    )

    def _spurious_wait(timeout: float | None = None) -> bool:
        """
        Record a spurious wait invocation and signal a wakeup.

        Parameters
        ----------
            timeout (float | None): The timeout value passed to the wait; may be None.

        Returns
        -------
            bool: `True` to indicate a spurious wakeup.

        Raises
        ------
            AssertionError: If the number of recorded wait calls exceeds the configured budget.
        """
        wait_calls.append(timeout)
        if len(wait_calls) > _MAX_SPURIOUS_CONNECT_WAIT_CALLS_BEFORE_FAIL:
            raise AssertionError("connect() kept waiting past the timeout budget")
        return True

    monkeypatch.setattr(
        iface._management_idle_condition,
        "wait",
        _spurious_wait,
        raising=True,
    )
    monkeypatch.setattr(iface, "_validate_connection_preconditions", lambda: None)
    monkeypatch.setattr(iface, "_get_existing_client_if_valid", lambda _request: None)

    with pytest.raises(BLEInterface.BLEError, match=ERROR_MANAGEMENT_CONNECTING):
        iface.connect("AA:BB:CC:DD:EE:10")

    assert wait_calls


def test_connect_management_wait_timeout_resets_between_wait_cycles(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """connect() should restart timeout accounting after management drains."""
    iface = _build_minimal_connect_test_interface()
    iface._management_lock = threading.RLock()
    iface._management_idle_condition = threading.Condition(iface._management_lock)
    iface._management_inflight = 1
    wait_calls: list[float | None] = []
    duplicate_checks = 0
    monotonic_values = iter([0.0, 0.1, 100.0, 100.1])
    last_time = 100.1

    monkeypatch.setattr(
        "meshtastic.interfaces.ble.interface._MANAGEMENT_CONNECT_WAIT_TIMEOUT_SECONDS",
        1.0,
    )
    monkeypatch.setattr(
        "meshtastic.interfaces.ble.interface._MANAGEMENT_CONNECT_WAIT_POLL_SECONDS",
        0.1,
    )

    def _monotonic() -> float:
        """
        Provide a monotonic timestamp for tests, advancing through a preset sequence and falling back to incremental steps when the sequence is exhausted.

        Returns
        -------
            float: The current monotonic time value and updates the captured `last_time` variable.
        """
        nonlocal last_time
        try:
            last_time = next(monotonic_values)
        except StopIteration:
            last_time += 0.1
        return last_time

    monkeypatch.setattr(
        "meshtastic.interfaces.ble.interface.time.monotonic", _monotonic
    )

    def _wait_for_management(timeout: float | None = None) -> bool:
        """
        Simulate waiting for management operations to drain and record the requested timeout.

        Parameters
        ----------
            timeout (float | None): Maximum seconds to wait, or `None` to indicate an indefinite wait.

        Behavior:
            Appends the provided `timeout` value to the `wait_calls` list and sets
            `iface._management_inflight` to 0 to indicate no inflight management operations.

        Returns
        -------
            bool: `True` to indicate the wait condition was signaled.
        """
        wait_calls.append(timeout)
        iface._management_inflight = 0
        return True

    def _raise_if_duplicate(_key: str | None) -> None:
        """
        Increment the duplicate-check counter and, on the second invocation, mark the interface as having one inflight management operation.

        Parameters
        ----------
            _key (str | None): Ignored; present to match the duplicate-check callback signature.
        """
        nonlocal duplicate_checks
        duplicate_checks += 1
        if duplicate_checks == 2:
            iface._management_inflight = 1

    monkeypatch.setattr(
        iface._management_idle_condition,
        "wait",
        _wait_for_management,
        raising=True,
    )
    monkeypatch.setattr(iface, "_validate_connection_preconditions", lambda: None)
    monkeypatch.setattr(iface, "_get_existing_client_if_valid", lambda _request: None)
    monkeypatch.setattr(
        iface,
        "_resolve_target_address_for_connect",
        lambda identifier: cast(str, identifier),
    )
    monkeypatch.setattr(iface, "_raise_if_duplicate_connect", _raise_if_duplicate)
    monkeypatch.setattr(iface, "_finalize_connection_gates", lambda *_args: None)
    monkeypatch.setattr(
        iface,
        "_verify_and_publish_connected",
        lambda *_args, **_kwargs: None,
    )

    def _establish_stub(
        _address: str | None,
        _normalized_request: str | None,
        _address_key: str | None,
        *,
        pair_on_connect: bool = False,
        connect_timeout: float | None = None,
    ) -> tuple[DummyClient, str | None, str | None]:
        """
        Create and attach a DummyClient to the test BLE interface and mark it as connected.

        Parameters
        ----------
            _address (str | None): Ignored; present to match the real establish signature.
            _normalized_request (str | None): Ignored; present to match the real establish signature.
            _address_key (str | None): Ignored; present to match the real establish signature.
            pair_on_connect (bool): Accepted but ignored by this test stub.
            connect_timeout (float | None): Accepted but ignored by this test stub.

        Returns
        -------
            tuple[DummyClient, None, None]: The created DummyClient instance and two None placeholders (resolved address and resolved identifier).
        """
        _ = (pair_on_connect, connect_timeout)
        client = DummyClient()
        with iface._state_lock:
            cast(Any, iface).client = client
            iface.address = client.address
            iface._state_manager._reset_to_disconnected()
            assert iface._state_manager._transition_to(ConnectionState.CONNECTING)
            assert iface._state_manager._transition_to(ConnectionState.CONNECTED)
        return client, None, None

    monkeypatch.setattr(iface, "_establish_and_update_client", _establish_stub)

    assert isinstance(iface.connect("AA:BB:CC:DD:EE:10"), DummyClient)
    assert len(wait_calls) == 2


def test_connect_retries_when_management_becomes_inflight_inside_connect_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """connect() should continue outer loop when management starts after address-gate wait."""
    iface = _build_minimal_connect_test_interface()
    iface._management_lock = threading.RLock()
    iface._management_idle_condition = threading.Condition(iface._management_lock)
    iface._management_inflight = 0
    established: list[bool] = []
    duplicate_checks = 0

    def _wait_for_management(timeout: float | None = None) -> bool:
        _ = timeout
        iface._management_inflight = 0
        return True

    def _raise_if_duplicate(_key: str | None) -> None:
        nonlocal duplicate_checks
        duplicate_checks += 1
        if duplicate_checks == 2:
            iface._management_inflight = 1

    monkeypatch.setattr(
        iface._management_idle_condition,
        "wait",
        _wait_for_management,
        raising=True,
    )
    monkeypatch.setattr(iface, "_validate_connection_preconditions", lambda: None)
    monkeypatch.setattr(iface, "_get_existing_client_if_valid", lambda _request: None)
    monkeypatch.setattr(
        iface,
        "_resolve_target_address_for_connect",
        lambda identifier: cast(str, identifier),
    )
    monkeypatch.setattr(iface, "_raise_if_duplicate_connect", _raise_if_duplicate)
    monkeypatch.setattr(iface, "_finalize_connection_gates", lambda *_args: None)
    monkeypatch.setattr(
        iface,
        "_verify_and_publish_connected",
        lambda *_args, **_kwargs: None,
    )

    def _establish_stub(
        _address: str | None,
        _normalized_request: str | None,
        _address_key: str | None,
        *,
        pair_on_connect: bool = False,
        connect_timeout: float | None = None,
    ) -> tuple[DummyClient, str | None, str | None]:
        _ = (pair_on_connect, connect_timeout)
        established.append(True)
        client = DummyClient()
        with iface._state_lock:
            cast(Any, iface).client = client
            iface.address = client.address
            iface._state_manager._reset_to_disconnected()
            assert iface._state_manager._transition_to(ConnectionState.CONNECTING)
            assert iface._state_manager._transition_to(ConnectionState.CONNECTED)
        return client, None, None

    monkeypatch.setattr(iface, "_establish_and_update_client", _establish_stub)

    assert isinstance(iface.connect("AA:BB:CC:DD:EE:10"), DummyClient)
    assert established == [True]
    assert duplicate_checks >= 4


def test_connect_returns_existing_client_after_lock_recheck(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """connect() should return an existing client found during in-lock recheck."""
    iface = _build_minimal_connect_test_interface()
    existing_client = cast(BLEClient, DummyClient())
    lookup_count = 0

    def _lookup_existing(_request: str | None) -> BLEClient | None:
        nonlocal lookup_count
        lookup_count += 1
        if lookup_count == 1:
            return None
        return existing_client

    monkeypatch.setattr(iface, "_validate_connection_preconditions", lambda: None)
    monkeypatch.setattr(iface, "_get_existing_client_if_valid", _lookup_existing)
    monkeypatch.setattr(
        iface,
        "_resolve_target_address_for_connect",
        lambda identifier: cast(str, identifier),
    )
    monkeypatch.setattr(iface, "_raise_if_duplicate_connect", lambda _key: None)

    assert iface.connect("AA:BB:CC:DD:EE:10") is existing_client


def test_connect_raises_when_establish_returns_no_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """connect() should fail fast when establishment returns no client object."""
    iface = _build_minimal_connect_test_interface()
    monkeypatch.setattr(iface, "_validate_connection_preconditions", lambda: None)
    monkeypatch.setattr(iface, "_get_existing_client_if_valid", lambda _request: None)
    monkeypatch.setattr(
        iface,
        "_resolve_target_address_for_connect",
        lambda identifier: cast(str, identifier),
    )
    monkeypatch.setattr(iface, "_raise_if_duplicate_connect", lambda _key: None)
    monkeypatch.setattr(
        iface,
        "_establish_and_update_client",
        lambda *_args, **_kwargs: (cast(DummyClient, None), None, None),
    )

    with pytest.raises(BLEInterface.BLEError, match="no BLE client established"):
        iface.connect("AA:BB:CC:DD:EE:10")


def test_connect_does_not_relabel_unrelated_establish_connection_value_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """connect() should preserve unrelated ValueError failures from orchestration."""
    iface = _build_minimal_connect_test_interface()
    cast(Any, iface)._connection_orchestrator = SimpleNamespace(
        _establish_connection=lambda *_args, **_kwargs: (_ for _ in ()).throw(
            ValueError("boom")
        )
    )

    monkeypatch.setattr(iface, "_validate_connection_preconditions", lambda: None)
    monkeypatch.setattr(iface, "_get_existing_client_if_valid", lambda _req: None)
    monkeypatch.setattr(iface, "_raise_if_duplicate_connect", lambda _key: None)

    with pytest.raises(ValueError, match="boom"):
        iface.connect("AA:BB:CC:DD:EE:10", connect_timeout=4.5)


def test_ble_interface_establish_and_update_client_discards_late_connection_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Late connect results should be closed instead of being published during shutdown."""
    iface = _build_interface(monkeypatch, DummyClient(), start_receive_thread=False)
    connected_client = DummyClient()
    connected_client.address = "AA:BB:CC:DD:EE:FF"
    connected_client.bleak_client = SimpleNamespace(address="AA:BB:CC:DD:EE:FF")
    cleanup_calls: list[object] = []

    monkeypatch.setattr(
        iface._connection_orchestrator,
        "_establish_connection",
        lambda *_args, **_kwargs: cast(BLEClient, connected_client),
    )
    monkeypatch.setattr(
        iface._client_manager,
        "_safe_close_client",
        lambda client: cleanup_calls.append(client),
    )

    with iface._connect_lock:
        with iface._state_lock:
            iface._closed = True
        with pytest.raises(BLEInterface.BLEError, match="closing"):
            iface._establish_and_update_client(
                "AA:BB:CC:DD:EE:FF",
                "aabbccddeeff",
                "aabbccddeeff",
                pair_on_connect=False,
            )

    assert cleanup_calls == [connected_client]
    with iface._state_lock:
        assert cast(object, iface.client) is not connected_client
    iface.close()


def test_establish_and_update_client_sets_last_request_from_device_and_updates_previous(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Established clients should refresh last request from device key and close replaced clients."""
    iface = _build_interface(monkeypatch, DummyClient(), start_receive_thread=False)
    previous_client = DummyClient()
    previous_client.address = "11:22:33:44:55:66"
    previous_client.bleak_client = SimpleNamespace(address=previous_client.address)
    connected_client = DummyClient()
    connected_client.address = "AA:BB:CC:DD:EE:FF"
    connected_client.bleak_client = SimpleNamespace(address=connected_client.address)
    updated_refs: list[tuple[BLEClient, BLEClient | None]] = []

    monkeypatch.setattr(
        iface._connection_orchestrator,
        "_establish_connection",
        lambda *_args, **_kwargs: cast(BLEClient, connected_client),
    )
    monkeypatch.setattr(
        iface._client_manager,
        "_update_client_reference",
        lambda new_client, old_client: updated_refs.append((new_client, old_client)),
    )

    with iface._state_lock:
        cast(Any, iface).client = previous_client
        iface._state_manager._reset_to_disconnected()
        assert iface._state_manager._transition_to(ConnectionState.CONNECTING) is True
        assert iface._state_manager._transition_to(ConnectionState.CONNECTED) is True

    with iface._connect_lock:
        result_client, _, _ = iface._establish_and_update_client(
            "AA:BB:CC:DD:EE:FF",
            None,
            "aabbccddeeff",
            pair_on_connect=False,
        )

    assert result_client is connected_client
    assert updated_refs == [(cast(BLEClient, connected_client), previous_client)]
    assert iface._last_connection_request == iface._sanitize_address(
        connected_client.address
    )
    iface.close()


def test_handle_disconnect_ignores_stale_callbacks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Stale disconnect callbacks must not clear the current active client."""
    stale_client = DummyClient()
    iface = _build_interface(monkeypatch, stale_client)

    active_client = DummyClient()
    active_client.address = "active"
    active_client.bleak_client = SimpleNamespace(address="active")
    reconnect_calls: list[bool] = []
    disconnected_calls: list[bool] = []

    monkeypatch.setattr(
        iface,
        "_schedule_auto_reconnect",
        lambda: reconnect_calls.append(True),
        raising=True,
    )
    monkeypatch.setattr(
        iface, "_disconnected", lambda: disconnected_calls.append(True), raising=True
    )

    with iface._state_lock:
        cast(Any, iface).client = active_client
        iface._disconnect_notified = False
        iface._state_manager._reset_to_disconnected()
        assert iface._state_manager._transition_to(ConnectionState.CONNECTING) is True
        assert iface._state_manager._transition_to(ConnectionState.CONNECTED) is True

    # Stale callback by BLEClient instance should be ignored.
    assert iface._handle_disconnect("stale-client", client=stale_client) is True  # type: ignore[arg-type]
    # Stale callback by bleak client identity should also be ignored.
    assert (
        iface._handle_disconnect("stale-bleak", bleak_client=stale_client.bleak_client)  # type: ignore[arg-type]
        is True
    )

    assert cast(object, iface.client) is active_client
    assert iface._disconnect_notified is False
    assert reconnect_calls == []
    assert disconnected_calls == []

    iface.close()


def test_discard_invalidated_connected_client_marks_stale_callbacks_notified(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Discarded clients should not trigger a second disconnect via their stale callback."""
    iface = _build_interface(monkeypatch, DummyClient(), start_receive_thread=False)
    discarded_client = DummyClient()
    discarded_client.address = "AA:BB:CC:DD:EE:FF"
    discarded_client.bleak_client = SimpleNamespace(address=discarded_client.address)
    disconnected_calls: list[bool] = []
    reconnect_calls: list[bool] = []
    callback_results: list[bool] = []

    monkeypatch.setattr(
        iface, "_disconnected", lambda: disconnected_calls.append(True), raising=True
    )
    monkeypatch.setattr(
        iface,
        "_schedule_auto_reconnect",
        lambda: reconnect_calls.append(True),
        raising=True,
    )

    def _safe_close_client(client: BLEClient) -> None:
        callback_results.append(
            iface._handle_disconnect("discarded-client", client=client)
        )

    monkeypatch.setattr(
        iface._client_manager,
        "_safe_close_client",
        _safe_close_client,
        raising=True,
    )

    with iface._state_lock:
        cast(Any, iface).client = discarded_client
        iface.address = discarded_client.address
        iface._disconnect_notified = False
        iface._state_manager._reset_to_disconnected()
        assert iface._state_manager._transition_to(ConnectionState.CONNECTING) is True
        assert iface._state_manager._transition_to(ConnectionState.CONNECTED) is True

    iface._discard_invalidated_connected_client(cast(BLEClient, discarded_client))

    assert callback_results == [True]
    assert disconnected_calls == []
    assert reconnect_calls == []
    with iface._state_lock:
        assert iface.client is None
        assert iface.address is None
        assert iface._disconnect_notified is True
        assert iface._state_manager._current_state == ConnectionState.DISCONNECTED

    iface.close()


def test_discard_invalidated_connected_client_clears_pending_when_already_detached(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pending publish flag should clear even if the provisional client already detached."""
    iface = _build_interface(monkeypatch, DummyClient(), start_receive_thread=False)
    discarded_client = DummyClient()
    discarded_client.address = "AA:BB:CC:DD:EE:44"
    discarded_client.bleak_client = SimpleNamespace(address=discarded_client.address)
    closed_clients: list[BLEClient] = []

    monkeypatch.setattr(
        iface._client_manager,
        "_safe_close_client",
        lambda client: closed_clients.append(client),
        raising=True,
    )

    with iface._state_lock:
        cast(Any, iface).client = None
        iface._client_publish_pending = True
        iface._disconnect_notified = False
        iface._state_manager._reset_to_disconnected()
        assert iface._state_manager._transition_to(ConnectionState.CONNECTING) is True

    iface._discard_invalidated_connected_client(cast(BLEClient, discarded_client))

    assert closed_clients == [cast(BLEClient, discarded_client)]
    with iface._state_lock:
        assert iface.client is None
        assert iface._client_publish_pending is False
        assert iface._disconnect_notified is False

    iface.close()


@pytest.mark.parametrize("is_closing", [True, False])
def test_finalize_connection_gates_cleans_up_when_client_loses_ownership_mid_finalize(
    monkeypatch: pytest.MonkeyPatch,
    is_closing: bool,
) -> None:
    """Gate finalization should clean up provisional claims when ownership disappears mid-finalize."""
    from meshtastic.interfaces.ble.lifecycle_service import BLELifecycleService

    iface = _build_interface(monkeypatch, DummyClient(), start_receive_thread=False)
    connected_client = DummyClient()
    cleanup_calls: list[tuple[str | None, str | None]] = []

    monkeypatch.setattr(
        BLELifecycleService,
        "_get_connected_client_status",
        lambda _iface, _client: (True, False),
        raising=True,
    )
    monkeypatch.setattr(
        BLELifecycleService,
        "_get_connected_client_status_locked",
        lambda _iface, _client: (False, is_closing),
        raising=True,
    )
    monkeypatch.setattr(iface, "_mark_address_keys_connected", lambda *_keys: None)
    monkeypatch.setattr(
        iface,
        "_mark_address_keys_disconnected",
        lambda *keys: cleanup_calls.append(cast(tuple[str | None, str | None], keys)),
    )

    iface._finalize_connection_gates(
        cast(BLEClient, connected_client), "device-key", "alias-key"
    )

    assert cleanup_calls == [("device-key", "alias-key")]
    assert iface._connection_alias_key is None
    iface.close()


@pytest.mark.parametrize("is_closing", [True, False])
def test_finalize_connection_gates_logs_when_result_is_already_stale(
    monkeypatch: pytest.MonkeyPatch,
    is_closing: bool,
) -> None:
    """Gate finalization should no-op when initial ownership check already reports stale result."""
    from meshtastic.interfaces.ble.lifecycle_service import BLELifecycleService

    iface = _build_interface(monkeypatch, DummyClient(), start_receive_thread=False)
    connected_client = DummyClient()
    mark_connected_calls: list[tuple[str | None, str | None]] = []
    mark_disconnected_calls: list[tuple[str | None, str | None]] = []

    monkeypatch.setattr(
        BLELifecycleService,
        "_get_connected_client_status",
        lambda _iface, _client: (False, is_closing),
        raising=True,
    )
    monkeypatch.setattr(
        iface,
        "_mark_address_keys_connected",
        lambda *keys: mark_connected_calls.append(
            cast(tuple[str | None, str | None], keys)
        ),
        raising=True,
    )
    monkeypatch.setattr(
        iface,
        "_mark_address_keys_disconnected",
        lambda *keys: mark_disconnected_calls.append(
            cast(tuple[str | None, str | None], keys)
        ),
        raising=True,
    )

    iface._finalize_connection_gates(
        cast(BLEClient, connected_client), "device-key", "alias-key"
    )

    assert mark_connected_calls == []
    assert mark_disconnected_calls == []
    iface.close()


def test_is_owned_connected_client_reads_status_tuple(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Owned-client helper should return the first element of status tuple."""
    from meshtastic.interfaces.ble.lifecycle_service import BLELifecycleService

    iface = _build_interface(monkeypatch, DummyClient(), start_receive_thread=False)
    client = cast(BLEClient, DummyClient())
    monkeypatch.setattr(
        BLELifecycleService,
        "_get_connected_client_status",
        lambda _iface, _client: (True, False),
        raising=True,
    )
    assert iface._is_owned_connected_client(client) is True
    iface.close()


def test_emit_verified_connection_side_effects_sets_reconnected_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verified publish should signal reconnected_event for reconnect publishes."""
    iface = _build_interface(monkeypatch, DummyClient(), start_receive_thread=False)
    connected_client = DummyClient()
    connected_client.address = "AA:BB:CC:DD:EE:FF"
    connected_client.bleak_client = SimpleNamespace(address=connected_client.address)
    set_events: list[str] = []
    monkeypatch.setattr(
        iface.thread_coordinator,
        "_set_event",
        lambda event_name: set_events.append(event_name),
        raising=True,
    )

    iface._prior_publish_was_reconnect = True
    iface._emit_verified_connection_side_effects(cast(BLEClient, connected_client))

    assert set_events == ["reconnected_event"]
    assert iface._prior_publish_was_reconnect is False
    iface.close()


def test_concurrent_connect_and_disconnect_do_not_deadlock(
    monkeypatch: pytest.MonkeyPatch, clear_registry: Any
) -> None:
    """Concurrent connect/disconnect should complete without deadlocking under address-lock contention.

    This test forces connect() to hold the per-address lock while _handle_disconnect()
    runs, then releases connect to ensure both operations complete.

    Raises
    ------
    AssertionError
    """
    _ = clear_registry
    import meshtastic.interfaces.ble.interface as ble_iface_mod

    target_address = "AA:BB:CC:DD:EE:01"
    initial_client = DummyClient()
    initial_client.address = target_address
    initial_client.bleak_client = SimpleNamespace(address=target_address)

    connected_client = DummyClient()
    connected_client.address = target_address
    connected_client.bleak_client = SimpleNamespace(address=target_address)

    real_connect = BLEInterface.connect

    def _init_connect_stub(
        iface: BLEInterface,
        _address: str | None = None,
        *,
        connect_timeout: float | None = None,
    ) -> DummyClient:
        """Prepare the given BLEInterface for tests by installing and returning a pre-existing DummyClient and marking the interface as connected.

        Parameters
        ----------
        iface : BLEInterface
            The interface whose client and connection state will be configured.
        _address : str | None
            Ignored; present for compatibility with call sites that pass an address.

        Returns
        -------
        DummyClient
            The dummy client instance that was attached to the interface.
        """
        _ = (_address, connect_timeout)
        with iface._state_lock:
            iface.client = initial_client  # type: ignore[assignment]
            iface._disconnect_notified = False
            iface._state_manager._reset_to_disconnected()
            assert iface._state_manager._transition_to(ConnectionState.CONNECTING)
            assert iface._state_manager._transition_to(ConnectionState.CONNECTED)
        return initial_client

    monkeypatch.setattr(BLEInterface, "connect", _init_connect_stub, raising=True)
    monkeypatch.setattr(
        BLEInterface,
        "_start_receive_thread",
        lambda _self, *, name: None,
        raising=True,
    )
    monkeypatch.setattr(BLEInterface, "_start_config", lambda _self: None, raising=True)

    iface = BLEInterface(address=target_address, noProto=True, auto_reconnect=False)
    monkeypatch.setattr(BLEInterface, "connect", real_connect, raising=True)

    with iface._state_lock:
        iface.client = None
        iface._disconnect_notified = False
        iface._connection_alias_key = None
        iface._state_manager._reset_to_disconnected()

    connect_waiting = threading.Event()
    allow_connect = threading.Event()
    establish_called = threading.Event()
    thread_errors: "Queue[tuple[str, Exception]]" = Queue()

    def _gate_check_stub(_addr_key: str | None, owner: Any | None = None) -> bool:
        """Block test caller until the test releases a connection gate and record that the gate was reached.

        Parameters
        ----------
        _addr_key : str | None
            Address key that must be provided (asserted non-None); used to identify the gated connection.
        owner : Any | None
            Ignored; present to match the gate-check signature. (Default value = None)

        Returns
        -------
        bool
            `False` always.

        Raises
        ------
        AssertionError
            If `_addr_key` is None or if waiting for the test to release the gate times out (12 seconds).
        """
        _ = owner
        assert _addr_key is not None
        connect_waiting.set()
        if not allow_connect.wait(timeout=12.0):
            raise AssertionError("Timed out waiting to release connect gate check")
        return False

    def _establish_connection_stub(*_args: Any, **_kwargs: Any) -> DummyClient:
        """Simulate a successful connection for tests by transitioning the interface state to CONNECTING then CONNECTED.

        Also sets the `establish_called` event to signal completion.

        Parameters
        ----------
        *_args : Any
        **_kwargs : Any

        Returns
        -------
        connected_client : DummyClient
            A DummyClient instance representing the established connection.
        """
        with iface._state_lock:
            assert iface._state_manager._transition_to(ConnectionState.CONNECTING)
            assert iface._state_manager._transition_to(ConnectionState.CONNECTED)
        establish_called.set()
        return connected_client

    monkeypatch.setattr(
        ble_iface_mod,
        "_is_currently_connected_elsewhere",
        _gate_check_stub,
        raising=True,
    )
    monkeypatch.setattr(
        iface._connection_orchestrator,
        "_establish_connection",
        _establish_connection_stub,
        raising=True,
    )
    monkeypatch.setattr(iface, "_register_notifications", lambda _client: None)
    monkeypatch.setattr(iface, "_connected", lambda: None)
    monkeypatch.setattr(iface, "_disconnected", lambda: None)

    def _connect_worker() -> None:
        """Invoke the interface's connect routine for the configured target address and capture any exception raised.

        If an exception occurs, record a tuple ("connect", exc) into the `thread_errors` queue for later inspection by tests.
        """
        try:
            iface.connect(target_address)
        except Exception as exc:  # noqa: BLE001 - test captures thread errors
            thread_errors.put(("connect", exc))

    def _disconnect_worker() -> None:
        """Invoke the interface's disconnect handler in a thread and capture any exception for test inspection.

        Calls iface._handle_disconnect("concurrency-test"). If an exception is raised, places a ("disconnect", exception) tuple into the thread_errors queue so test code can observe thread failures.
        """
        try:
            iface._handle_disconnect("concurrency-test")
        except Exception as exc:  # noqa: BLE001 - test captures thread errors
            thread_errors.put(("disconnect", exc))

    connect_thread = threading.Thread(target=_connect_worker, daemon=True)
    disconnect_thread = threading.Thread(target=_disconnect_worker, daemon=True)
    try:
        connect_thread.start()
        assert connect_waiting.wait(timeout=12.0), "connect() did not reach gate check"

        disconnect_thread.start()
        allow_connect.set()

        connect_thread.join(timeout=12.0)
        disconnect_thread.join(timeout=12.0)

        assert (
            establish_called.is_set()
        ), "connect() did not run connection establishment"
        assert not connect_thread.is_alive(), "connect() thread appears deadlocked"
        assert not disconnect_thread.is_alive(), "disconnect thread appears deadlocked"

        if not thread_errors.empty():
            where, exc = thread_errors.get_nowait()
            pytest.fail(f"{where} thread raised {type(exc).__name__}: {exc}")
    finally:
        allow_connect.set()
        if connect_thread.is_alive():
            connect_thread.join(timeout=1.0)
        if disconnect_thread.is_alive():
            disconnect_thread.join(timeout=1.0)
        iface.close()


def test_ble_interface_init_forwards_constructor_timeout_to_initial_connect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """__init__() should pass its timeout through to the eager initial connect()."""
    captured_timeouts: list[float | None] = []
    initial_client = DummyClient()
    initial_client.address = "AA:BB:CC:DD:EE:09"
    initial_client.bleak_client = SimpleNamespace(address=initial_client.address)

    def _init_connect_stub(
        iface: BLEInterface,
        _address: str | None = None,
        *,
        connect_timeout: float | None = None,
    ) -> DummyClient:
        _ = _address
        captured_timeouts.append(connect_timeout)
        with iface._state_lock:
            iface.client = initial_client  # type: ignore[assignment]
            iface._disconnect_notified = False
            iface._state_manager._reset_to_disconnected()
            assert iface._state_manager._transition_to(ConnectionState.CONNECTING)
            assert iface._state_manager._transition_to(ConnectionState.CONNECTED)
        return initial_client

    monkeypatch.setattr(BLEInterface, "connect", _init_connect_stub, raising=True)
    monkeypatch.setattr(
        BLEInterface,
        "_start_receive_thread",
        lambda _self, *, name: None,
        raising=True,
    )
    monkeypatch.setattr(BLEInterface, "_start_config", lambda _self: None, raising=True)

    iface = BLEInterface(
        address=initial_client.address,
        noProto=True,
        auto_reconnect=False,
        timeout=17.5,
    )

    assert captured_timeouts == [17.5]
    iface.close()


def test_connect_finalizes_gates_after_address_lock_scope(
    monkeypatch: pytest.MonkeyPatch,
    clear_registry: None,
) -> None:
    """connect() should finalize address gates only after per-address lock scope exits."""
    _ = clear_registry
    import meshtastic.interfaces.ble.interface as ble_iface_mod

    target_address = "AA:BB:CC:DD:EE:02"
    real_connect = BLEInterface.connect

    def _init_connect_stub(
        iface: BLEInterface,
        _address: str | None = None,
        *,
        connect_timeout: float | None = None,
    ) -> DummyClient:
        _ = (_address, connect_timeout)
        initial_client = DummyClient()
        initial_client.address = target_address
        initial_client.bleak_client = SimpleNamespace(address=target_address)
        with iface._state_lock:
            iface.client = initial_client  # type: ignore[assignment]
            iface._disconnect_notified = False
            iface._state_manager._reset_to_disconnected()
            assert iface._state_manager._transition_to(ConnectionState.CONNECTING)
            assert iface._state_manager._transition_to(ConnectionState.CONNECTED)
        return initial_client

    monkeypatch.setattr(BLEInterface, "connect", _init_connect_stub, raising=True)
    monkeypatch.setattr(
        BLEInterface,
        "_start_receive_thread",
        lambda _self, *, name: None,
        raising=True,
    )
    monkeypatch.setattr(BLEInterface, "_start_config", lambda _self: None, raising=True)

    iface = BLEInterface(address=target_address, noProto=True, auto_reconnect=False)
    monkeypatch.setattr(BLEInterface, "connect", real_connect, raising=True)

    with iface._state_lock:
        iface.client = None
        iface._disconnect_notified = False
        iface._connection_alias_key = None
        iface._state_manager._reset_to_disconnected()

    address_lock_held = False

    class _FakeAddressLock:
        def __enter__(self) -> "_FakeAddressLock":
            nonlocal address_lock_held
            address_lock_held = True
            return self

        def __exit__(
            self,
            exc_type: type[BaseException] | None,
            exc: BaseException | None,
            tb: TracebackType | None,
        ) -> Literal[False]:
            _ = (exc_type, exc, tb)
            nonlocal address_lock_held
            address_lock_held = False
            return False

    @contextlib.contextmanager
    def _fake_addr_lock_context(_addr: str | None) -> Iterator[_FakeAddressLock]:
        with _FakeAddressLock() as lock:
            yield lock

    connected_client = DummyClient()
    connected_client.address = target_address
    connected_client.bleak_client = SimpleNamespace(address=target_address)

    finalized_lock_states: list[bool] = []

    def _finalize_stub(
        _client: BLEClient, _device_key: str | None, _alias_key: str | None
    ) -> None:
        finalized_lock_states.append(address_lock_held)

    monkeypatch.setattr(
        ble_iface_mod, "_addr_lock_context", _fake_addr_lock_context, raising=True
    )
    monkeypatch.setattr(
        iface, "_raise_if_duplicate_connect", lambda _connection_key: None, raising=True
    )
    monkeypatch.setattr(
        iface, "_get_existing_client_if_valid", lambda _request: None, raising=True
    )

    def _establish_stub(
        _address: str | None,
        _normalized_request: str | None,
        _address_key: str | None,
        *,
        pair_on_connect: bool = False,
        connect_timeout: float | None = None,
    ) -> tuple[DummyClient, str | None, str | None]:
        _ = (pair_on_connect, connect_timeout)
        with iface._state_lock:
            cast(Any, iface).client = connected_client
            iface.address = target_address
            iface._state_manager._reset_to_disconnected()
            assert iface._state_manager._transition_to(ConnectionState.CONNECTING)
            assert iface._state_manager._transition_to(ConnectionState.CONNECTED)
        return connected_client, "device-key", None

    monkeypatch.setattr(
        iface,
        "_establish_and_update_client",
        _establish_stub,
        raising=True,
    )
    monkeypatch.setattr(
        iface, "_finalize_connection_gates", _finalize_stub, raising=True
    )

    result = iface.connect(target_address)

    assert cast(object, result) is connected_client
    assert finalized_lock_states == [False]
    iface.close()


def test_connect_marks_provisional_claims_before_gate_release(
    monkeypatch: pytest.MonkeyPatch,
    clear_registry: None,
) -> None:
    """connect() should publish provisional ownership before releasing the address gate."""
    _ = clear_registry
    from meshtastic.interfaces.ble.gating import _is_currently_connected_elsewhere

    target_identifier = "mesh-node"
    device_key = "aabbccddee30"
    iface = _build_minimal_connect_test_interface()
    connected_client = DummyClient()
    connected_client.address = "AA:BB:CC:DD:EE:30"
    connected_client.bleak_client = SimpleNamespace(address=connected_client.address)
    observed_claims: list[bool] = []

    monkeypatch.setattr(iface, "_validate_connection_preconditions", lambda: None)
    monkeypatch.setattr(
        iface, "_raise_if_duplicate_connect", lambda _connection_key: None, raising=True
    )
    monkeypatch.setattr(
        iface, "_get_existing_client_if_valid", lambda _request: None, raising=True
    )
    monkeypatch.setattr(iface, "_connected", lambda: None, raising=True)

    def _establish_stub(
        _address: str | None,
        _normalized_request: str | None,
        _address_key: str | None,
        *,
        pair_on_connect: bool = False,
        connect_timeout: float | None = None,
    ) -> tuple[DummyClient, str | None, str | None]:
        _ = (pair_on_connect, connect_timeout)
        with iface._state_lock:
            cast(Any, iface).client = connected_client
            iface.address = connected_client.address
            iface._state_manager._reset_to_disconnected()
            assert iface._state_manager._transition_to(ConnectionState.CONNECTING)
            assert iface._state_manager._transition_to(ConnectionState.CONNECTED)
        return connected_client, device_key, target_identifier

    def _finalize_stub(
        _self: BLEInterface,
        _client: BLEClient,
        _device_key: str | None,
        _alias_key: str | None,
    ) -> None:
        observed_claims.append(
            _is_currently_connected_elsewhere(device_key, owner=object())
        )

    monkeypatch.setattr(
        iface,
        "_establish_and_update_client",
        _establish_stub,
        raising=True,
    )
    monkeypatch.setattr(BLEInterface, "_finalize_connection_gates", _finalize_stub)

    iface.connect(target_identifier)

    assert observed_claims == [True]
    assert _is_currently_connected_elsewhere(device_key, owner=object()) is False


def test_connect_name_target_reserves_requested_and_resolved_keys(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Name-based connect should reserve both alias and resolved concrete keys."""
    from meshtastic.interfaces.ble.gating import _addr_key

    iface = _build_minimal_connect_test_interface()
    target_identifier = "mesh-node"
    resolved_address = "AA:BB:CC:DD:EE:31"
    connected_client = DummyClient()
    connected_client.address = resolved_address
    connected_client.bleak_client = SimpleNamespace(address=resolved_address)
    cast(Any, iface)._discovery_manager = object()
    duplicate_checks: list[str] = []
    addr_lock_keys: list[str | None] = []
    established_args: list[tuple[str | None, str | None, str | None]] = []

    monkeypatch.setattr(iface, "_validate_connection_preconditions", lambda: None)
    monkeypatch.setattr(
        iface, "_get_existing_client_if_valid", lambda _request: None, raising=True
    )
    monkeypatch.setattr(
        iface,
        "findDevice",
        lambda _identifier: BLEDevice(
            address=resolved_address, name="Mesh", details={}
        ),
        raising=True,
    )
    monkeypatch.setattr(
        iface, "_raise_if_duplicate_connect", duplicate_checks.append, raising=True
    )
    monkeypatch.setattr(iface, "_finalize_connection_gates", lambda *_args: None)
    monkeypatch.setattr(iface, "_connected", lambda: None, raising=True)
    monkeypatch.setattr(
        iface,
        "_emit_verified_connection_side_effects",
        lambda _client: None,
        raising=True,
    )

    @contextlib.contextmanager
    def _record_addr_lock_context(_addr: str | None) -> Iterator[threading.RLock]:
        addr_lock_keys.append(_addr)
        lock = threading.RLock()
        yield lock

    monkeypatch.setattr(
        "meshtastic.interfaces.ble.interface._addr_lock_context",
        _record_addr_lock_context,
    )

    def _establish_stub(
        address: str | None,
        normalized_request: str | None,
        address_key: str | None,
        *,
        pair_on_connect: bool = False,
        connect_timeout: float | None = None,
    ) -> tuple[DummyClient, str | None, str | None]:
        _ = (pair_on_connect, connect_timeout)
        established_args.append((address, normalized_request, address_key))
        with iface._state_lock:
            cast(Any, iface).client = connected_client
            iface.address = resolved_address
            iface._state_manager._reset_to_disconnected()
            assert iface._state_manager._transition_to(ConnectionState.CONNECTING)
            assert iface._state_manager._transition_to(ConnectionState.CONNECTED)
        return (
            connected_client,
            _addr_key(resolved_address),
            _addr_key(target_identifier),
        )

    monkeypatch.setattr(
        iface,
        "_establish_and_update_client",
        _establish_stub,
        raising=True,
    )

    try:
        result = iface.connect(target_identifier)

        assert cast(object, result) is connected_client
        requested_key = _addr_key(target_identifier)
        resolved_key = _addr_key(resolved_address)
        assert requested_key is not None and resolved_key is not None
        assert duplicate_checks.count(requested_key) >= 2
        assert duplicate_checks.count(resolved_key) >= 2
        assert requested_key in addr_lock_keys
        assert resolved_key in addr_lock_keys
        assert established_args == [
            (
                resolved_address,
                iface._sanitize_address(target_identifier),
                requested_key,
            )
        ]
    finally:
        if hasattr(iface, "_shutdown_event"):
            iface.close()


def test_connect_raises_when_client_becomes_stale_after_gate_finalization(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """connect() should not return a client that lost ownership after finalization."""
    target_address = "AA:BB:CC:DD:EE:03"
    replacement_address = "AA:BB:CC:DD:EE:04"
    iface = _build_minimal_connect_test_interface()
    connected_callbacks: list[bool] = []
    connected_client = DummyClient()
    connected_client.address = target_address
    connected_client.bleak_client = SimpleNamespace(address=target_address)
    finalized_clients: list[BLEClient] = []
    closed_clients: list[BLEClient] = []
    released_claims: list[tuple[str | None, ...]] = []

    monkeypatch.setattr(iface, "_connected", lambda: connected_callbacks.append(True))
    monkeypatch.setattr(iface, "_validate_connection_preconditions", lambda: None)
    monkeypatch.setattr(
        iface, "_raise_if_duplicate_connect", lambda _connection_key: None, raising=True
    )
    monkeypatch.setattr(
        iface, "_get_existing_client_if_valid", lambda _request: None, raising=True
    )
    monkeypatch.setattr(
        iface._client_manager,
        "_safe_close_client",
        lambda client: closed_clients.append(client),
        raising=True,
    )
    monkeypatch.setattr(
        iface,
        "_mark_address_keys_disconnected",
        lambda *keys: released_claims.append(keys),
        raising=True,
    )

    def _establish_stub(
        _address: str | None,
        _normalized_request: str | None,
        _address_key: str | None,
        *,
        pair_on_connect: bool = False,
        connect_timeout: float | None = None,
    ) -> tuple[DummyClient, str | None, str | None]:
        _ = (pair_on_connect, connect_timeout)
        with iface._state_lock:
            cast(Any, iface).client = connected_client
            iface.address = target_address
            iface._state_manager._reset_to_disconnected()
            assert iface._state_manager._transition_to(ConnectionState.CONNECTING)
            assert iface._state_manager._transition_to(ConnectionState.CONNECTED)
        return connected_client, "device-key", None

    def _finalize_stub(
        _self: BLEInterface,
        _client: BLEClient,
        _device_key: str | None,
        _alias_key: str | None,
    ) -> None:
        finalized_clients.append(_client)
        replacement_client = DummyClient()
        replacement_client.address = replacement_address
        replacement_client.bleak_client = SimpleNamespace(address=replacement_address)
        with iface._state_lock:
            cast(Any, iface).client = replacement_client
            iface.address = replacement_address
            iface._state_manager._reset_to_disconnected()
            assert iface._state_manager._transition_to(ConnectionState.CONNECTING)
            assert iface._state_manager._transition_to(ConnectionState.CONNECTED)

    monkeypatch.setattr(
        iface,
        "_establish_and_update_client",
        _establish_stub,
        raising=True,
    )
    monkeypatch.setattr(BLEInterface, "_finalize_connection_gates", _finalize_stub)

    with pytest.raises(BLEInterface.BLEError, match=CONNECTION_ERROR_LOST_OWNERSHIP):
        iface.connect(target_address)

    assert finalized_clients == [cast(BLEClient, connected_client)]
    assert closed_clients == [cast(BLEClient, connected_client)]
    assert released_claims == [("device-key",)]
    assert connected_callbacks == []
    assert cast(object, iface.client) is not connected_client
    assert iface.address != target_address


def test_connect_preserves_reclaimed_keys_for_newer_client_after_gate_finalization(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """connect() should not release keys a newer client on this interface already reclaimed."""
    target_address = "AA:BB:CC:DD:EE:03"
    device_key = "aabbccddee03"
    alias_key = "mesh-node"
    iface = _build_minimal_connect_test_interface()
    connected_client = DummyClient()
    connected_client.address = target_address
    connected_client.bleak_client = SimpleNamespace(address=target_address)
    finalized_clients: list[BLEClient] = []
    closed_clients: list[BLEClient] = []
    released_claims: list[tuple[str | None, ...]] = []

    monkeypatch.setattr(iface, "_validate_connection_preconditions", lambda: None)
    monkeypatch.setattr(
        iface, "_raise_if_duplicate_connect", lambda _connection_key: None, raising=True
    )
    monkeypatch.setattr(
        iface, "_get_existing_client_if_valid", lambda _request: None, raising=True
    )
    monkeypatch.setattr(
        iface._client_manager,
        "_safe_close_client",
        lambda client: closed_clients.append(client),
        raising=True,
    )
    monkeypatch.setattr(
        iface,
        "_mark_address_keys_disconnected",
        lambda *keys: released_claims.append(keys),
        raising=True,
    )

    def _establish_stub(
        _address: str | None,
        _normalized_request: str | None,
        _address_key: str | None,
        *,
        pair_on_connect: bool = False,
        connect_timeout: float | None = None,
    ) -> tuple[DummyClient, str | None, str | None]:
        _ = (pair_on_connect, connect_timeout)
        with iface._state_lock:
            cast(Any, iface).client = connected_client
            iface.address = target_address
            iface._connection_alias_key = alias_key
            iface._state_manager._reset_to_disconnected()
            assert iface._state_manager._transition_to(ConnectionState.CONNECTING)
            assert iface._state_manager._transition_to(ConnectionState.CONNECTED)
        return connected_client, device_key, alias_key

    def _finalize_stub(
        _self: BLEInterface,
        _client: BLEClient,
        _device_key: str | None,
        _alias_key: str | None,
    ) -> None:
        finalized_clients.append(_client)
        replacement_client = DummyClient()
        replacement_client.address = target_address
        replacement_client.bleak_client = SimpleNamespace(address=target_address)
        with iface._state_lock:
            cast(Any, iface).client = replacement_client
            iface.address = target_address
            iface._connection_alias_key = alias_key
            iface._state_manager._reset_to_disconnected()
            assert iface._state_manager._transition_to(ConnectionState.CONNECTING)
            assert iface._state_manager._transition_to(ConnectionState.CONNECTED)

    monkeypatch.setattr(
        iface,
        "_establish_and_update_client",
        _establish_stub,
        raising=True,
    )
    monkeypatch.setattr(BLEInterface, "_finalize_connection_gates", _finalize_stub)

    with pytest.raises(BLEInterface.BLEError, match=CONNECTION_ERROR_LOST_OWNERSHIP):
        iface.connect(target_address)

    assert finalized_clients == [cast(BLEClient, connected_client)]
    assert closed_clients == [cast(BLEClient, connected_client)]
    assert released_claims == []
    assert cast(Any, iface).client is not connected_client


def test_connect_raises_when_registry_ownership_is_lost_after_gate_finalization(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """connect() should fail if process-wide address ownership moves elsewhere."""
    target_address = "AA:BB:CC:DD:EE:0A"
    iface = _build_minimal_connect_test_interface()
    connected_callbacks: list[bool] = []
    connected_client = DummyClient()
    connected_client.address = target_address
    connected_client.bleak_client = SimpleNamespace(address=target_address)
    finalized_clients: list[BLEClient] = []
    closed_clients: list[BLEClient] = []
    released_claims: list[tuple[str | None, ...]] = []

    monkeypatch.setattr(iface, "_connected", lambda: connected_callbacks.append(True))
    monkeypatch.setattr(iface, "_validate_connection_preconditions", lambda: None)
    monkeypatch.setattr(
        iface, "_raise_if_duplicate_connect", lambda _connection_key: None, raising=True
    )
    monkeypatch.setattr(
        iface, "_get_existing_client_if_valid", lambda _request: None, raising=True
    )
    monkeypatch.setattr(
        iface._client_manager,
        "_safe_close_client",
        lambda client: closed_clients.append(client),
        raising=True,
    )
    monkeypatch.setattr(
        iface,
        "_mark_address_keys_disconnected",
        lambda *keys: released_claims.append(keys),
        raising=True,
    )
    monkeypatch.setattr(
        "meshtastic.interfaces.ble.interface._is_currently_connected_elsewhere",
        lambda key, owner=None: key == "device-key" and owner is iface,
    )

    def _establish_stub(
        _address: str | None,
        _normalized_request: str | None,
        _address_key: str | None,
        *,
        pair_on_connect: bool = False,
        connect_timeout: float | None = None,
    ) -> tuple[DummyClient, str | None, str | None]:
        _ = (pair_on_connect, connect_timeout)
        with iface._state_lock:
            cast(Any, iface).client = connected_client
            iface.address = target_address
            iface._state_manager._reset_to_disconnected()
            assert iface._state_manager._transition_to(ConnectionState.CONNECTING)
            assert iface._state_manager._transition_to(ConnectionState.CONNECTED)
        return connected_client, "device-key", None

    def _finalize_stub(
        _self: BLEInterface,
        _client: BLEClient,
        _device_key: str | None,
        _alias_key: str | None,
    ) -> None:
        finalized_clients.append(_client)

    monkeypatch.setattr(
        iface,
        "_establish_and_update_client",
        _establish_stub,
        raising=True,
    )
    monkeypatch.setattr(BLEInterface, "_finalize_connection_gates", _finalize_stub)

    with pytest.raises(BLEInterface.BLEError, match=CONNECTION_ERROR_LOST_OWNERSHIP):
        iface.connect(target_address)

    assert finalized_clients == [cast(BLEClient, connected_client)]
    assert closed_clients == [cast(BLEClient, connected_client)]
    assert released_claims == [("device-key",)]
    assert connected_callbacks == []
    assert iface.client is None
    assert iface.address == target_address
    assert iface._last_connection_request == iface._sanitize_address(target_address)


def test_connect_restores_requested_identifier_after_name_target_loses_ownership(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A discarded name-based connect should restore the caller's requested identifier."""
    target_identifier = "mesh-node"
    target_address = "AA:BB:CC:DD:EE:11"
    iface = _build_minimal_connect_test_interface()
    connected_client = DummyClient()
    connected_client.address = target_address
    connected_client.bleak_client = SimpleNamespace(address=target_address)
    closed_clients: list[BLEClient] = []
    released_claims: list[tuple[str | None, ...]] = []

    monkeypatch.setattr(iface, "_validate_connection_preconditions", lambda: None)
    monkeypatch.setattr(
        iface, "_raise_if_duplicate_connect", lambda _connection_key: None, raising=True
    )
    monkeypatch.setattr(
        iface, "_get_existing_client_if_valid", lambda _request: None, raising=True
    )
    monkeypatch.setattr(
        iface._client_manager,
        "_safe_close_client",
        lambda client: closed_clients.append(client),
        raising=True,
    )
    monkeypatch.setattr(
        iface,
        "_mark_address_keys_disconnected",
        lambda *keys: released_claims.append(keys),
        raising=True,
    )
    monkeypatch.setattr(
        "meshtastic.interfaces.ble.interface._is_currently_connected_elsewhere",
        lambda key, owner=None: key == "device-key" and owner is iface,
    )

    def _establish_stub(
        _address: str | None,
        _normalized_request: str | None,
        _address_key: str | None,
        *,
        pair_on_connect: bool = False,
        connect_timeout: float | None = None,
    ) -> tuple[DummyClient, str | None, str | None]:
        _ = (pair_on_connect, connect_timeout)
        with iface._state_lock:
            cast(Any, iface).client = connected_client
            iface.address = target_address
            iface._state_manager._reset_to_disconnected()
            assert iface._state_manager._transition_to(ConnectionState.CONNECTING)
            assert iface._state_manager._transition_to(ConnectionState.CONNECTED)
        return connected_client, "device-key", None

    monkeypatch.setattr(
        iface,
        "_establish_and_update_client",
        _establish_stub,
        raising=True,
    )
    monkeypatch.setattr(BLEInterface, "_finalize_connection_gates", lambda *_args: None)

    with pytest.raises(BLEInterface.BLEError, match=CONNECTION_ERROR_LOST_OWNERSHIP):
        iface.connect(target_identifier)

    assert closed_clients == [cast(BLEClient, connected_client)]
    assert released_claims == [("device-key",)]
    assert iface.address == target_identifier
    assert iface._last_connection_request == iface._sanitize_address(target_identifier)
    with iface._state_lock:
        assert iface._get_current_implicit_management_address_locked() is None


def test_connect_rechecks_ownership_before_publishing_connected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """connect() should discard a client that becomes stale after the first check."""
    from meshtastic.interfaces.ble.lifecycle_service import BLELifecycleService

    target_address = "AA:BB:CC:DD:EE:12"
    iface = _build_minimal_connect_test_interface()
    connected_client = DummyClient()
    connected_client.address = target_address
    connected_client.bleak_client = SimpleNamespace(address=target_address)
    connected_callbacks: list[bool] = []
    closed_clients: list[BLEClient] = []
    released_claims: list[tuple[str | None, ...]] = []
    status_checks = iter([(True, False), (False, False)])

    monkeypatch.setattr(iface, "_connected", lambda: connected_callbacks.append(True))
    monkeypatch.setattr(iface, "_validate_connection_preconditions", lambda: None)
    monkeypatch.setattr(
        iface, "_raise_if_duplicate_connect", lambda _connection_key: None, raising=True
    )
    monkeypatch.setattr(
        iface, "_get_existing_client_if_valid", lambda _request: None, raising=True
    )
    monkeypatch.setattr(
        iface._client_manager,
        "_safe_close_client",
        lambda client: closed_clients.append(client),
        raising=True,
    )
    monkeypatch.setattr(
        iface,
        "_mark_address_keys_disconnected",
        lambda *keys: released_claims.append(keys),
        raising=True,
    )
    monkeypatch.setattr(
        BLELifecycleService,
        "_get_connected_client_status_locked",
        lambda _iface, _client: next(status_checks),
        raising=True,
    )
    monkeypatch.setattr(
        iface, "_has_lost_gate_ownership", lambda *_keys: True, raising=True
    )

    def _establish_stub(
        _address: str | None,
        _normalized_request: str | None,
        _address_key: str | None,
        *,
        pair_on_connect: bool = False,
        connect_timeout: float | None = None,
    ) -> tuple[DummyClient, str | None, str | None]:
        _ = (pair_on_connect, connect_timeout)
        with iface._state_lock:
            cast(Any, iface).client = connected_client
            iface.address = target_address
            iface._state_manager._reset_to_disconnected()
            assert iface._state_manager._transition_to(ConnectionState.CONNECTING)
            assert iface._state_manager._transition_to(ConnectionState.CONNECTED)
        return connected_client, "device-key", None

    monkeypatch.setattr(
        iface,
        "_establish_and_update_client",
        _establish_stub,
        raising=True,
    )
    monkeypatch.setattr(BLEInterface, "_finalize_connection_gates", lambda *_args: None)

    with pytest.raises(BLEInterface.BLEError, match=CONNECTION_ERROR_LOST_OWNERSHIP):
        iface.connect(target_address)

    assert connected_callbacks == []
    assert released_claims == [("device-key",)]
    assert closed_clients == [cast(BLEClient, connected_client)]
    assert iface.client is None
    assert iface.address == target_address


def test_connect_raises_when_shutdown_wins_after_gate_finalization(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """connect() should surface shutdown when close() wins after gate finalization."""
    target_address = "AA:BB:CC:DD:EE:05"
    iface = _build_minimal_connect_test_interface()
    connected_callbacks: list[bool] = []
    connected_client = DummyClient()
    connected_client.address = target_address
    connected_client.bleak_client = SimpleNamespace(address=target_address)
    finalized_clients: list[BLEClient] = []
    closed_clients: list[BLEClient] = []
    released_claims: list[tuple[str | None, ...]] = []

    monkeypatch.setattr(iface, "_connected", lambda: connected_callbacks.append(True))
    monkeypatch.setattr(iface, "_validate_connection_preconditions", lambda: None)
    monkeypatch.setattr(
        iface, "_raise_if_duplicate_connect", lambda _connection_key: None, raising=True
    )
    monkeypatch.setattr(
        iface, "_get_existing_client_if_valid", lambda _request: None, raising=True
    )
    monkeypatch.setattr(
        iface._client_manager,
        "_safe_close_client",
        lambda client: closed_clients.append(client),
        raising=True,
    )
    monkeypatch.setattr(
        iface,
        "_mark_address_keys_disconnected",
        lambda *keys: released_claims.append(keys),
        raising=True,
    )

    def _establish_stub(
        _address: str | None,
        _normalized_request: str | None,
        _address_key: str | None,
        *,
        pair_on_connect: bool = False,
        connect_timeout: float | None = None,
    ) -> tuple[DummyClient, str | None, str | None]:
        _ = (pair_on_connect, connect_timeout)
        with iface._state_lock:
            cast(Any, iface).client = connected_client
            iface.address = target_address
            iface._state_manager._reset_to_disconnected()
            assert iface._state_manager._transition_to(ConnectionState.CONNECTING)
            assert iface._state_manager._transition_to(ConnectionState.CONNECTED)
        return connected_client, "device-key", None

    def _finalize_stub(
        _self: BLEInterface,
        _client: BLEClient,
        _device_key: str | None,
        _alias_key: str | None,
    ) -> None:
        finalized_clients.append(_client)
        with iface._state_lock:
            iface._closed = True

    monkeypatch.setattr(
        iface,
        "_establish_and_update_client",
        _establish_stub,
        raising=True,
    )
    monkeypatch.setattr(BLEInterface, "_finalize_connection_gates", _finalize_stub)

    with pytest.raises(BLEInterface.BLEError, match=ERROR_INTERFACE_CLOSING):
        iface.connect(target_address)

    assert finalized_clients == [cast(BLEClient, connected_client)]
    assert closed_clients == [cast(BLEClient, connected_client)]
    assert released_claims == [("device-key",)]
    assert connected_callbacks == []
    assert cast(object, iface.client) is not connected_client
    assert iface.address == target_address


def test_transient_read_retry_uses_zero_based_delay(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Transient read retries should pass a zero-based attempt index to policy delay."""
    iface = _build_interface(monkeypatch, DummyClient())
    delay_attempts: list[int] = []

    class StubTransientPolicy:
        """Retry policy stub that records delay attempt indexes."""

        def _should_retry(self, attempt: int) -> bool:
            """Decide whether to perform another retry based on the zero-based attempt index.

            Parameters
            ----------
            attempt : int
                Zero-based retry attempt index (0 for the first attempt).

            Returns
            -------
            bool
                `True` if `attempt` is less than 1, `False` otherwise.
            """
            return attempt < 1

        def _get_delay(self, attempt: int) -> float:
            """Record the retry attempt index and return a zero-second retry delay.

            Appends the zero-based `attempt` index to the surrounding test's `delay_attempts` list.

            Parameters
            ----------
            attempt : int
                Zero-based retry attempt index to record.

            Returns
            -------
            float
                Delay in seconds (always 0.0).
            """
            delay_attempts.append(attempt)
            return 0.0

    iface._transient_read_policy = StubTransientPolicy()  # type: ignore[assignment]
    monkeypatch.setattr(
        "meshtastic.interfaces.ble.interface._sleep", lambda _delay: None
    )

    iface._read_retry_count = 0
    iface._handle_transient_read_error(BleakError("transient"))

    assert iface._read_retry_count == 1
    assert delay_attempts == [0]

    iface.close()


def test_receive_recovery_backoff_reaches_configured_cap_for_non_power_of_two(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify recovery backoff reaches configured max for non-power-of-two caps.

    Parameters
    ----------
    monkeypatch : pytest.MonkeyPatch
        Fixture used to patch recovery timing constants.

    Returns
    -------
    None
    """
    import meshtastic.interfaces.ble.receive_service as receive_service_mod

    iface = SimpleNamespace()
    iface._is_connection_closing = False
    iface._state_lock = threading.RLock()
    iface.client = None
    iface._handle_disconnect = lambda *_args, **_kwargs: True
    iface._set_receive_wanted = lambda *_args, **_kwargs: None
    iface._receive_recovery_attempts = 4
    iface._last_recovery_time = 100.0
    iface._read_retry_count = 7
    iface._should_run_receive_loop = lambda: True
    iface._start_receive_thread = MagicMock()
    iface._shutdown_event = MagicMock()
    iface._shutdown_event.wait.return_value = True

    monkeypatch.setattr(
        receive_service_mod,
        "RECEIVE_RECOVERY_RAPID_FAILURE_THRESHOLD",
        0,
        raising=True,
    )
    monkeypatch.setattr(
        receive_service_mod,
        "RECEIVE_RECOVERY_MAX_BACKOFF_SEC",
        30.0,
        raising=True,
    )
    monkeypatch.setattr(receive_service_mod.time, "monotonic", lambda: 100.0)

    receive_service_mod.BLEReceiveRecoveryService._recover_receive_thread(
        iface, "receive_thread_fatal"
    )

    iface._shutdown_event.wait.assert_called_once_with(timeout=30.0)
    assert iface._read_retry_count == 7
    iface._start_receive_thread.assert_not_called()


def test_receive_loop_outer_catch_routes_to_disconnect_handler(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Outer receive-loop exceptions should use normal disconnect handling.

    Raises
    ------
    RuntimeError
    """
    client = DummyClient()
    iface = _build_interface(monkeypatch, client)
    disconnect_calls: list[tuple[str, Any | None, Any | None]] = []

    def raising_wait_for_event(_name: str, timeout: float | None = None) -> bool:
        """Simulate a fatal receive-loop failure by always raising a RuntimeError.

        Parameters
        ----------
        _name : str
            Event name (unused in this stub).
        timeout : float | None
            Timeout value (unused in this stub).

        Raises
        ------
        RuntimeError
            Always raised to emulate an unexpected fatal error in the receive loop.
        """
        _ = timeout
        raise RuntimeError("fatal receive loop failure")

    def fake_handle_disconnect(
        source: str,
        client: Any | None = None,
        bleak_client: Any | None = None,
    ) -> bool:
        """Record the disconnect invocation and stop the receive loop.

        Parameters
        ----------
        source : str
        client : Any | None
        bleak_client : Any | None

        Returns
        -------
        bool
            `False` indicating the handler did not handle the disconnect.
        """
        disconnect_calls.append((source, client, bleak_client))
        iface._want_receive = False
        return False

    monkeypatch.setattr(
        iface.thread_coordinator,
        "_wait_for_event",
        raising_wait_for_event,
        raising=True,
    )
    monkeypatch.setattr(
        iface, "_handle_disconnect", fake_handle_disconnect, raising=True
    )

    iface._want_receive = True
    iface._receive_from_radio_impl()

    assert disconnect_calls
    source, disconnected_client, disconnected_bleak = disconnect_calls[0]
    assert source == "receive_thread_fatal"
    assert disconnected_client is client
    assert disconnected_bleak is None

    iface.close()


def test_receive_loop_waits_while_publish_pending(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Receive loop should pause reads while connect publication is pending."""
    client = DummyClient()
    iface = _build_interface(monkeypatch, client, start_receive_thread=False)
    wait_events: list[str] = []

    def _wait_for_event(event_name: str, timeout: float | None = None) -> bool:
        _ = timeout
        wait_events.append(event_name)
        if event_name == "read_trigger":
            return True
        if event_name == "reconnected_event":
            iface._want_receive = False
            return False
        return False

    monkeypatch.setattr(
        iface.thread_coordinator,
        "_wait_for_event",
        _wait_for_event,
        raising=True,
    )
    monkeypatch.setattr(
        iface.thread_coordinator,
        "_clear_event",
        lambda _event_name: None,
        raising=True,
    )
    monkeypatch.setattr(
        iface,
        "_read_from_radio_with_retries",
        lambda *_args, **_kwargs: pytest.fail(
            "read should be skipped while publish pending"
        ),
        raising=True,
    )

    with iface._state_lock:
        iface.client = client
        iface._client_publish_pending = True
    iface._want_receive = True

    iface._receive_from_radio_impl()

    assert "reconnected_event" in wait_events
    iface.close()


def test_receive_loop_waits_for_reconnect_when_client_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Receive loop should wait on reconnected_event when client is missing and auto-reconnect is enabled."""
    iface = _build_interface(monkeypatch, DummyClient(), start_receive_thread=False)
    wait_events: list[str] = []

    def _wait_for_event(event_name: str, timeout: float | None = None) -> bool:
        _ = timeout
        wait_events.append(event_name)
        if event_name == "read_trigger":
            return True
        if event_name == "reconnected_event":
            iface._want_receive = False
            return False
        return False

    monkeypatch.setattr(
        iface.thread_coordinator,
        "_wait_for_event",
        _wait_for_event,
        raising=True,
    )
    monkeypatch.setattr(
        iface.thread_coordinator,
        "_clear_event",
        lambda _event_name: None,
        raising=True,
    )

    with iface._state_lock:
        iface.client = None
        iface._client_publish_pending = False
    iface.auto_reconnect = True
    iface._want_receive = True

    iface._receive_from_radio_impl()

    assert wait_events.count("reconnected_event") >= 1
    iface.close()


def test_start_receive_thread_skips_when_interface_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Receive thread start helper should no-op once the interface is closed.

    Raises
    ------
    AssertionError
    """
    client = DummyClient()
    iface = _build_interface(monkeypatch, client)
    iface.close()

    def should_not_create_thread(*_args: object, **_kwargs: object) -> None:
        """Fail if thread creation is attempted after the interface has been closed.

        Raises
        ------
        AssertionError
            Always raised with the message "create_thread should not be called after close()".
        """
        raise AssertionError("create_thread should not be called after close()")

    monkeypatch.setattr(
        iface.thread_coordinator,
        "_create_thread",
        should_not_create_thread,
        raising=True,
    )

    iface._start_receive_thread(name="BLEReceiveAfterClose")


def test_start_receive_thread_clears_cached_thread_when_start_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify start failures clear cached receive-thread references.

    Parameters
    ----------
    monkeypatch : pytest.MonkeyPatch
        Fixture used to patch thread-coordinator start behavior.

    Returns
    -------
    None
    """
    from meshtastic.interfaces.ble.lifecycle_service import BLELifecycleService

    iface = _build_interface(monkeypatch, DummyClient(), start_receive_thread=False)
    with iface._state_lock:
        iface._want_receive = True
    thread_like = SimpleNamespace(
        name="BLEReceiveStartFailure",
        ident=None,
        is_alive=lambda: False,
    )
    monkeypatch.setattr(
        iface.thread_coordinator,
        "_create_thread",
        lambda **_kwargs: thread_like,
        raising=True,
    )

    def _raise_start_failure(_thread: object) -> None:
        assert iface._receiveThread is thread_like
        raise RuntimeError(START_FAILED_MSG)

    monkeypatch.setattr(
        iface.thread_coordinator,
        "_start_thread",
        _raise_start_failure,
        raising=True,
    )

    with pytest.raises(RuntimeError, match=START_FAILED_MSG):
        BLELifecycleService._start_receive_thread(iface, name="BLEReceiveStartFailure")

    assert iface._receiveThread is None


def test_start_receive_thread_facade_clears_cached_thread_when_start_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify facade start failures clear cached receive-thread references.

    Parameters
    ----------
    monkeypatch : pytest.MonkeyPatch
        Fixture used to patch thread-coordinator start behavior.

    Returns
    -------
    None
    """
    from meshtastic.interfaces.ble.interface import BLEInterface

    iface = _build_interface(monkeypatch, DummyClient(), start_receive_thread=False)
    monkeypatch.setattr(
        type(iface),
        "_start_receive_thread",
        BLEInterface._start_receive_thread,
        raising=True,
    )
    with iface._state_lock:
        iface._want_receive = True
    thread_like = SimpleNamespace(
        name="BLEReceiveStartFailure",
        ident=None,
        is_alive=lambda: False,
    )
    monkeypatch.setattr(
        iface.thread_coordinator,
        "_create_thread",
        lambda **_kwargs: thread_like,
        raising=True,
    )

    def _raise_start_failure(_thread: object) -> None:
        assert iface._receiveThread is thread_like
        raise RuntimeError(START_FAILED_MSG)

    monkeypatch.setattr(
        iface.thread_coordinator,
        "_start_thread",
        _raise_start_failure,
        raising=True,
    )

    with pytest.raises(RuntimeError, match=START_FAILED_MSG):
        iface._start_receive_thread(name="BLEReceiveStartFailure")

    assert iface._receiveThread is None


def test_start_receive_thread_clears_cached_thread_when_start_noops(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify no-op thread starts clear stale receive-thread placeholders.

    Parameters
    ----------
    monkeypatch : pytest.MonkeyPatch
        Fixture used to patch thread start behavior.

    Returns
    -------
    None
    """
    from meshtastic.interfaces.ble.lifecycle_service import BLELifecycleService

    iface = _build_interface(monkeypatch, DummyClient(), start_receive_thread=False)
    with iface._state_lock:
        iface._want_receive = True
    thread_like = SimpleNamespace(
        name="BLEReceiveStartNoop",
        ident=None,
        is_alive=lambda: False,
    )
    monkeypatch.setattr(
        iface.thread_coordinator,
        "_create_thread",
        lambda **_kwargs: thread_like,
        raising=True,
    )

    start_calls: list[object] = []

    def _record_noop_start(thread: object) -> None:
        start_calls.append(thread)
        assert iface._receiveThread is thread_like

    monkeypatch.setattr(
        iface.thread_coordinator,
        "_start_thread",
        _record_noop_start,
        raising=True,
    )

    BLELifecycleService._start_receive_thread(iface, name="BLEReceiveStartNoop")

    assert start_calls == [thread_like]
    assert iface._receiveThread is None


def test_find_device_multiple_matches_raises() -> None:
    """Providing an address that matches multiple devices should raise BLEError."""
    # BLEDevice and BLEInterface already imported at top as ble_mod.BLEDevice, ble_mod.BLEInterface

    # Intentional constructor bypass for isolated findDevice() behavior.
    iface = object.__new__(ble_mod.BLEInterface)
    devices = [
        _create_ble_device(address="AA:BB:CC:DD:EE:FF", name="Meshtastic-1"),
        _create_ble_device(address="AA-BB-CC-DD-EE-FF", name="Meshtastic-2"),
    ]
    iface._discovery_manager = SimpleNamespace(_discover_devices=lambda _addr: devices)  # type: ignore[assignment]

    with pytest.raises(BLEInterface.BLEError) as excinfo:
        BLEInterface.findDevice(iface, "aa bb cc dd ee ff")

    assert "Multiple Meshtastic BLE peripherals found matching" in str(excinfo.value)


def test_find_device_direct_connect_preserves_raw_address() -> None:
    """Direct-connect fallback should keep the raw BLE address format."""
    iface = object.__new__(ble_mod.BLEInterface)
    iface._discovery_manager = SimpleNamespace(_discover_devices=lambda _addr: [])  # type: ignore[assignment]

    address = "AA:BB:CC:DD:EE:FF"
    direct_device = BLEInterface.findDevice(iface, address)

    assert direct_device.address == address
    assert direct_device.name == address


def test_find_device_direct_connect_without_discovery_manager() -> None:
    """Verify direct-connect fallback works when discovery manager is missing.

    Returns
    -------
    None
    """
    iface = object.__new__(ble_mod.BLEInterface)
    iface._discovery_manager = None  # type: ignore[assignment]

    address = "AA:BB:CC:DD:EE:FF"
    direct_device = BLEInterface.findDevice(iface, address)

    assert direct_device.address == address
    assert direct_device.name == address


def test_wait_for_disconnect_notifications_skips_unconfigured_queuework(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify disconnect flush falls back to drain when queueWork is unconfigured.

    Parameters
    ----------
    monkeypatch : pytest.MonkeyPatch
        Fixture used to patch publish queue draining behavior.

    Returns
    -------
    None
    """
    from meshtastic.interfaces.ble.compatibility_service import (
        BLECompatibilityEventService,
    )

    iface = SimpleNamespace(
        error_handler=SimpleNamespace(safe_execute=lambda func, **_kwargs: func())
    )
    publishing_thread = MagicMock()
    queue_work = publishing_thread.queueWork
    drained: list[bool] = []
    monkeypatch.setattr(
        BLECompatibilityEventService,
        "drain_publish_queue",
        lambda *_args, **_kwargs: drained.append(True),
        raising=True,
    )

    BLECompatibilityEventService.wait_for_disconnect_notifications(
        iface,
        timeout=0.01,
        publishing_thread=publishing_thread,
    )

    assert drained == [True]
    queue_work.assert_not_called()


def test_publish_connection_status_runs_directly_when_queuework_unconfigured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify status publish runs inline when queueWork is unconfigured.

    Parameters
    ----------
    monkeypatch : pytest.MonkeyPatch
        Fixture used to patch publish transport.

    Returns
    -------
    None
    """
    from meshtastic import mesh_interface as mesh_iface_module
    from meshtastic.interfaces.ble.compatibility_service import (
        BLECompatibilityEventService,
    )

    sent: list[tuple[str, object, bool]] = []

    def _send_message(topic: str, *, interface: object, connected: bool) -> None:
        sent.append((topic, interface, connected))

    monkeypatch.setattr(
        mesh_iface_module,
        "pub",
        SimpleNamespace(sendMessage=_send_message),
        raising=True,
    )

    iface = SimpleNamespace()
    publishing_thread = MagicMock()
    queue_work = publishing_thread.queueWork

    BLECompatibilityEventService.publish_connection_status(
        iface,
        connected=True,
        publishing_thread=publishing_thread,
    )

    assert sent == [("meshtastic.connection.status", iface, True)]
    queue_work.assert_not_called()


def test_publish_connection_status_falls_back_inline_when_non_blocking_enqueue_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify status publish falls back inline when non-blocking enqueue is unavailable.

    Parameters
    ----------
    monkeypatch : pytest.MonkeyPatch
        Fixture used to patch queueWork failure behavior.

    Returns
    -------
    None
    """
    from meshtastic import mesh_interface as mesh_iface_module
    from meshtastic.interfaces.ble.compatibility_service import (
        BLECompatibilityEventService,
    )

    sent: list[tuple[str, object, bool]] = []

    def _send_message(topic: str, *, interface: object, connected: bool) -> None:
        sent.append((topic, interface, connected))

    monkeypatch.setattr(
        mesh_iface_module,
        "pub",
        SimpleNamespace(sendMessage=_send_message),
        raising=True,
    )

    queue_attempts: list[object] = []

    class _FailingPublishingThread:
        class QueueFailure(Exception):
            """Raised when queueWork fails in this test double."""

        def queueWork(self, callback: object) -> None:
            queue_attempts.append(callback)
            raise self.QueueFailure

    iface = SimpleNamespace()
    publishing_thread = _FailingPublishingThread()

    BLECompatibilityEventService.publish_connection_status(
        iface,
        connected=False,
        publishing_thread=publishing_thread,
    )

    assert len(queue_attempts) == 1
    assert sent == [("meshtastic.connection.status", iface, False)]


def test_discovery_manager_filters_meshtastic_devices(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """DiscoveryManager should return only devices advertising the Meshtastic service UUID."""

    filtered_device = _create_ble_device("AA:BB:CC:DD:EE:FF", "Filtered")
    other_device = _create_ble_device("11:22:33:44:55:66", "Other")

    discover_result = {
        "filtered": (
            filtered_device,
            SimpleNamespace(service_uuids=[SERVICE_UUID]),
        ),
        "other": (
            other_device,
            SimpleNamespace(service_uuids=["some-other-service"]),
        ),
    }
    monkeypatch.setattr(
        ble_mod,
        "BLEClient",
        lambda **_kwargs: _FakeDiscoveryClient(
            discover_result,
        ),
    )

    manager = DiscoveryManager()

    devices = manager._discover_devices(address=None)

    assert len(devices) == 1
    assert devices[0].address == filtered_device.address


def test_discovery_manager_filters_targeted_scan_to_whitelist_match(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Targeted discovery should keep only exact address/name matches."""
    target_device = _create_ble_device("AA:BB:CC:DD:EE:FF", "Target")
    other_meshtastic_device = _create_ble_device("11:22:33:44:55:66", "Other")

    discover_result = {
        "target": (
            target_device,
            SimpleNamespace(service_uuids=[]),
        ),
        "other": (
            other_meshtastic_device,
            SimpleNamespace(service_uuids=[SERVICE_UUID]),
        ),
    }
    monkeypatch.setattr(
        ble_mod,
        "BLEClient",
        lambda **_kwargs: _FakeDiscoveryClient(
            discover_result,
        ),
    )

    manager = DiscoveryManager()
    devices = manager._discover_devices(address="AA:BB:CC:DD:EE:FF")

    assert devices == [target_device]


def test_discovery_manager_rejects_non_callable_discover_method() -> None:
    """DiscoveryManager should reject clients missing callable discover entrypoints.

    Returns
    -------
    None
    """

    class InvalidDiscoveryClient:
        _discover = None

    manager = DiscoveryManager(
        client_factory=lambda **_kwargs: InvalidDiscoveryClient()
    )

    with pytest.raises(DiscoveryClientError, match=r"discover|_discover"):
        manager._discover_devices(address=None)

    assert manager._client is None


def test_discovery_manager_accepts_discover_underscore_only_factory() -> None:
    """Verify DiscoveryManager accepts clients exposing only ``_discover``.

    Returns
    -------
    None
    """
    filtered_device = _create_ble_device("AA:BB:CC:DD:EE:FF", "Filtered")
    discover_result = {
        "filtered": (
            filtered_device,
            SimpleNamespace(service_uuids=[SERVICE_UUID]),
        ),
    }

    class UnderscoreDiscoveryClient:
        @staticmethod
        def _discover(**_kwargs: object) -> dict[str, Any]:
            return discover_result

    manager = DiscoveryManager(
        client_factory=lambda **_kwargs: UnderscoreDiscoveryClient()
    )
    devices = manager._discover_devices(address=None)

    assert devices == [filtered_device]


def test_discovery_manager_prefers_configured_underscore_discover_over_unconfigured_mock_public_discover() -> None:
    """Verify discovery prefers configured ``_discover`` over unconfigured ``discover``.

    Returns
    -------
    None
    """
    filtered_device = _create_ble_device("AA:BB:CC:DD:EE:FF", "Filtered")
    discover_result = {
        "filtered": (
            filtered_device,
            SimpleNamespace(service_uuids=[SERVICE_UUID]),
        ),
    }
    client = MagicMock()
    client._discover.return_value = discover_result
    manager = DiscoveryManager(client_factory=lambda **_kwargs: client)

    devices = manager._discover_devices(address=None)

    assert devices == [filtered_device]
    client._discover.assert_called_once()
    client.discover.assert_not_called()

    client._discover.reset_mock()
    devices = manager.discover_devices(address=None)
    assert devices == [filtered_device]
    client._discover.assert_called_once()
    client.discover.assert_not_called()


def test_discovery_manager_discards_cached_client_on_non_kwarg_typeerror(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Non-kwarg TypeError from discover should invalidate the cached discovery client."""

    class _TypeErrorDiscoveryClient:
        def __init__(self) -> None:
            self.bleak_client = object()

        @staticmethod
        def _discover(**_kwargs: object) -> dict[str, Any]:
            raise TypeError("discovery invocation failed")

    client = _TypeErrorDiscoveryClient()
    manager = DiscoveryManager(client_factory=lambda **_kwargs: client)
    manager._client = cast(Any, client)

    closed_clients: list[object] = []
    monkeypatch.setattr(
        discovery_mod,
        "_close_discovery_client_best_effort",
        lambda stale_client: closed_clients.append(stale_client),
        raising=True,
    )

    with pytest.raises(DiscoveryClientError, match="invalid type"):
        manager._discover_devices(address=None)

    assert manager._client is None
    assert client in closed_clients


def test_discovery_manager_supports_factory_without_log_if_no_address_kwarg() -> None:
    """DiscoveryManager should call factories without log_if_no_address using signature-based fallback."""
    filtered_device = _create_ble_device("AA:BB:CC:DD:EE:FF", "Filtered")
    discover_result = {
        "filtered": (
            filtered_device,
            SimpleNamespace(service_uuids=[SERVICE_UUID]),
        ),
    }

    factory_calls = 0

    def _factory_without_kwargs() -> _FakeDiscoveryClient:
        nonlocal factory_calls
        factory_calls += 1
        return _FakeDiscoveryClient(discover_result)

    manager = DiscoveryManager(client_factory=_factory_without_kwargs)
    devices = manager._discover_devices(address=None)

    assert devices == [filtered_device]
    assert factory_calls == 1


def test_discovery_manager_uses_default_bleclient_when_ble_module_missing(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """DiscoveryManager should fall back to default BLEClient when module resolution fails."""
    filtered_device = _create_ble_device("AA:BB:CC:DD:EE:FF", "Filtered")
    discover_result = {
        "filtered": (
            filtered_device,
            SimpleNamespace(service_uuids=[SERVICE_UUID]),
        ),
    }

    class _DefaultClient:
        def __init__(self, **_kwargs: Any) -> None:
            self.bleak_client = None

        @staticmethod
        def _discover(**_kwargs: object) -> dict[str, Any]:
            return discover_result

    monkeypatch.setattr(discovery_mod, "resolve_ble_module", lambda: None)
    monkeypatch.setattr(discovery_mod, "BLEClient", _DefaultClient)
    manager = DiscoveryManager()

    with caplog.at_level(logging.DEBUG):
        devices = manager._discover_devices(address=None)

    assert devices == [filtered_device]
    assert "No BLE module found; using default BLEClient" in caplog.text


def test_discovery_manager_deduplicates_stale_client_cleanup_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Duplicate stale-client references should be closed only once."""

    class _ManagerWithStickySecondNone(DiscoveryManager):
        """DiscoveryManager test double that preserves _client on second None assignment."""

        def __init__(self, *args: Any, **kwargs: Any) -> None:
            self._none_assignments = 0
            super().__init__(*args, **kwargs)

        def __setattr__(self, name: str, value: Any) -> None:
            if name == "_client" and value is None and "_client" in self.__dict__:
                self._none_assignments += 1
                if self._none_assignments >= 2:
                    return
            super().__setattr__(name, value)

    class _InvalidDiscoveryClient:
        def __init__(self) -> None:
            self.bleak_client = object()

        @staticmethod
        def isConnected() -> bool:
            return False

    invalid_client = _InvalidDiscoveryClient()
    manager = _ManagerWithStickySecondNone(client_factory=lambda: invalid_client)
    manager._client = cast(Any, invalid_client)
    closed: list[int] = []
    monkeypatch.setattr(
        discovery_mod,
        "_close_discovery_client_best_effort",
        lambda stale_client: closed.append(id(stale_client)),
    )

    with pytest.raises(DiscoveryClientError, match="invalid type"):
        manager._discover_devices(address=None)

    assert closed == [id(invalid_client)]


def test_close_discovery_client_best_effort_closes_coroutine_when_task_creation_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Best-effort cleanup should close the coroutine when create_task fails."""

    class _AwaitableClose:
        def __init__(self) -> None:
            self.closed = False

        def __await__(self) -> Any:
            """Return an empty awaitable iterator that completes immediately."""
            return iter(())

        def close(self) -> None:
            """Track explicit coroutine close calls."""
            self.closed = True

    awaitable = _AwaitableClose()

    class _Client:
        def close(self) -> Any:
            """Return the awaitable close result used by this test."""
            return awaitable

    class _Loop:
        @staticmethod
        def create_task(_task: Any) -> None:
            """Simulate loop task scheduling failure."""
            raise RuntimeError("cannot schedule task")

    def _get_running_loop() -> _Loop:
        """Return the fake running event loop."""
        return _Loop()

    def _await_close_result_passthrough(awaitable: Any) -> Any:
        """Keep awaitable unchanged for deterministic unit-test behavior."""
        return awaitable

    def _wait_for_passthrough(
        awaitable: Any, _timeout: float | None = None, **_kwargs: Any
    ) -> Any:
        """Bypass timeout wrapping to keep this branch deterministic."""
        return awaitable

    monkeypatch.setattr(
        "meshtastic.interfaces.ble.discovery.asyncio.get_running_loop",
        _get_running_loop,
    )
    monkeypatch.setattr(
        discovery_mod, "_await_close_result", _await_close_result_passthrough
    )
    monkeypatch.setattr(
        "meshtastic.interfaces.ble.discovery.asyncio.wait_for",
        _wait_for_passthrough,
    )
    monkeypatch.setattr(
        "meshtastic.interfaces.ble.discovery.inspect.iscoroutine",
        lambda value: isinstance(value, _AwaitableClose),
    )

    assert awaitable.closed is False
    _close_discovery_client_best_effort(_Client())

    assert awaitable.closed is True


def test_finalize_discovery_close_task_discards_task_and_logs_exception(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """_finalize_discovery_close_task should drop retained tasks and log non-cancel exceptions."""

    class _Task:
        def exception(self) -> Exception:
            """Return a deterministic task failure for log assertion."""
            return RuntimeError("close task failed")

    task = _Task()
    with discovery_mod._PENDING_DISCOVERY_CLOSE_TASKS_LOCK:
        discovery_mod._PENDING_DISCOVERY_CLOSE_TASKS.add(cast(Any, task))

    with caplog.at_level(logging.DEBUG):
        discovery_mod._finalize_discovery_close_task(task)  # type: ignore[arg-type]

    with discovery_mod._PENDING_DISCOVERY_CLOSE_TASKS_LOCK:
        assert cast(Any, task) not in discovery_mod._PENDING_DISCOVERY_CLOSE_TASKS
    assert (
        "Async close/disconnect failed for discarded discovery client." in caplog.text
    )


def test_close_discovery_client_best_effort_tracks_pending_task_on_running_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Best-effort async close should retain task until done callback executes."""

    class _AwaitableClose:
        def __await__(self) -> Any:
            """Return an empty awaitable iterator that completes immediately."""
            return iter(())

    class _Client:
        def close(self) -> Any:
            """Return an awaitable close result for scheduling."""
            return _AwaitableClose()

    class _Task:
        def __init__(self) -> None:
            self._callbacks: list[Callable[[Any], None]] = []

        def add_done_callback(self, callback: Callable[[Any], None]) -> None:
            """Store done callbacks for explicit invocation by the test."""
            self._callbacks.append(callback)

        def exception(self) -> None:
            """Expose successful task completion."""
            return None

    task = _Task()

    class _Loop:
        @staticmethod
        def create_task(_awaitable: Any) -> _Task:
            """Return the retained task used for callback assertions."""
            return task

    def _get_running_loop() -> _Loop:
        """Return the fake running event loop."""
        return _Loop()

    def _await_close_result_passthrough(awaitable: Any) -> Any:
        """Keep awaitable unchanged for deterministic unit-test behavior."""
        return awaitable

    def _wait_for_passthrough(
        awaitable: Any, _timeout: float | None = None, **_kwargs: Any
    ) -> Any:
        """Bypass timeout wrapping to keep this branch deterministic."""
        return awaitable

    monkeypatch.setattr(
        "meshtastic.interfaces.ble.discovery.asyncio.get_running_loop",
        _get_running_loop,
    )
    monkeypatch.setattr(
        discovery_mod, "_await_close_result", _await_close_result_passthrough
    )
    monkeypatch.setattr(
        "meshtastic.interfaces.ble.discovery.asyncio.wait_for",
        _wait_for_passthrough,
    )

    _close_discovery_client_best_effort(_Client())

    with discovery_mod._PENDING_DISCOVERY_CLOSE_TASKS_LOCK:
        assert cast(Any, task) in discovery_mod._PENDING_DISCOVERY_CLOSE_TASKS
    assert len(task._callbacks) == 1

    task._callbacks[0](task)
    with discovery_mod._PENDING_DISCOVERY_CLOSE_TASKS_LOCK:
        assert cast(Any, task) not in discovery_mod._PENDING_DISCOVERY_CLOSE_TASKS


def test_discovery_manager_raises_when_factory_returns_none() -> None:
    """DiscoveryManager should raise DiscoveryClientError for None-returning factories."""
    manager = DiscoveryManager(client_factory=lambda: None)

    with pytest.raises(DiscoveryClientError, match="returned None"):
        manager._discover_devices(address=None)


def test_parse_scan_response_prefers_exact_name_before_normalized_match() -> None:
    """Targeted scan should prefer an exact name match over normalized-name candidates."""
    exact_name_device = _create_ble_device("AA:BB:CC:DD:EE:FF", "My Device")
    normalized_only_device = _create_ble_device("11:22:33:44:55:66", "my device")

    response = {
        "exact": (exact_name_device, SimpleNamespace(service_uuids=[])),
        "normalized": (normalized_only_device, SimpleNamespace(service_uuids=[])),
    }

    devices = _parse_scan_response(response, whitelist_address="My Device")

    assert devices == [exact_name_device]


def test_parse_scan_response_skips_malformed_tuple_payloads() -> None:
    """Malformed discover tuple entries should be ignored, preserving only valid BLEDevice entries."""
    valid_device = _create_ble_device("AA:BB:CC:DD:EE:FF", "Valid")
    response = {
        "valid": (valid_device, SimpleNamespace(service_uuids=[SERVICE_UUID])),
        "invalid_device": (
            "not-a-device",
            SimpleNamespace(service_uuids=[SERVICE_UUID]),
        ),
        "invalid_adv": (valid_device, object()),
    }

    devices = _parse_scan_response(response, whitelist_address=None)

    assert devices == [valid_device]


def test_looks_like_ble_address_accepts_mac_and_uuid_shapes() -> None:
    """Address-shape detection should support MAC-style and UUID-style identifiers."""
    assert _looks_like_ble_address("AA:BB:CC:DD:EE:FF")
    assert _looks_like_ble_address("aabbccddeeff")
    assert _looks_like_ble_address("00112233445566778899aabbccddeeff")
    assert _looks_like_ble_address("00112233-4455-6677-8899-aabbccddeeff")
    assert not _looks_like_ble_address("Meshtastic Device")


def test_filter_devices_rejects_ambiguous_normalized_name_matches() -> None:
    """Name matching should reject ambiguous normalized-name collisions."""
    devices = [
        _create_ble_device("AA:BB:CC:DD:EE:FF", "My Device"),
        _create_ble_device("11:22:33:44:55:66", "my device"),
    ]

    matches = _filter_devices_for_target_identifier(devices, "MY DEVICE")

    assert matches == []


def test_ble_interface_with_timeout_wrapper_returns_result() -> None:
    """BLEInterface._with_timeout should delegate to with_timeout and return the awaited value."""

    async def _ready() -> str:
        return "ok"

    assert (
        asyncio.run(BLEInterface._with_timeout(_ready(), timeout=1.0, label="ble-op"))
        == "ok"
    )


def test_ble_interface_sanitize_address_wrapper_delegates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """_sanitize_address should delegate to sanitize_address helper."""
    iface = object.__new__(BLEInterface)
    monkeypatch.setattr(
        "meshtastic.interfaces.ble.interface.sanitize_address",
        lambda address: "normalized" if address else None,
    )

    assert iface._sanitize_address("AA-BB-CC-DD-EE-FF") == "normalized"


def test_discovery_manager_destructor_does_not_close_client() -> None:
    """DiscoveryManager.__del__ should avoid active client close I/O during GC."""

    class StubDiscoveryClient:
        """Discovery client stub used for destructor behavior checks.

        Methods
        -------
        close()
        """

        def __init__(self) -> None:
            """Initialize the test stub and reset its close-call counter.

            Sets the `close_calls` attribute to 0; tests increment this counter when the stub's `close()` is invoked to verify that discovery clients are not closed unexpectedly.
            """
            self.close_calls = 0

        def close(self) -> None:
            """Record that the client's close method was invoked by incrementing an internal call counter.

            This method exists for tests to track how many times close() was called on the object by incrementing the `close_calls` attribute.
            """
            self.close_calls += 1

    manager = DiscoveryManager()
    client = StubDiscoveryClient()
    manager._client = cast(BLEClient, client)

    manager.__del__()

    assert client.close_calls == 0
    assert manager._client is None


def test_discovery_manager_close_is_idempotent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """close() should clear and clean up the persistent discovery client only once."""
    manager = DiscoveryManager()
    client = object()
    close_calls: list[int] = []
    manager._client = cast(BLEClient, client)

    monkeypatch.setattr(
        discovery_mod,
        "_close_discovery_client_best_effort",
        lambda stale_client: close_calls.append(id(stale_client)),
    )

    manager.close()
    manager.close()

    assert close_calls == [id(client)]
    assert manager._client is None


def test_discovery_manager_destructor_tolerates_unusable_lock() -> None:
    """DiscoveryManager.__del__ should fall back when _client_lock is not lock-like."""
    manager = object.__new__(DiscoveryManager)
    cast(Any, manager)._client_lock = object()
    cast(Any, manager)._client = object()

    manager.__del__()

    assert cast(Any, manager)._client is None


def test_connection_validator_enforces_state() -> None:
    """ConnectionValidator should block connections when interface is closing or already connecting."""

    state_manager = BLEStateManager()
    validator = ConnectionValidator(
        state_manager, state_manager._lock, BLEInterface.BLEError
    )

    validator._validate_connection_request()

    assert state_manager._transition_to(ConnectionState.CONNECTING) is True
    assert state_manager._transition_to(ConnectionState.CONNECTED) is True
    assert state_manager._transition_to(ConnectionState.DISCONNECTING) is True
    with pytest.raises(BLEInterface.BLEError) as excinfo:
        validator._validate_connection_request()
    assert "closing" in str(excinfo.value)

    assert state_manager._transition_to(ConnectionState.DISCONNECTED) is True
    assert state_manager._transition_to(ConnectionState.CONNECTING) is True
    with pytest.raises(BLEInterface.BLEError) as excinfo:
        validator._validate_connection_request()
    assert "connection in progress" in str(excinfo.value)


def test_connection_validator_existing_client_checks() -> None:
    """check_existing_client should allow reuse only when the requested identifier matches."""

    state_manager = BLEStateManager()
    validator = ConnectionValidator(
        state_manager, state_manager._lock, BLEInterface.BLEError
    )
    client = DummyClient()
    cast(Any, client).isConnected = lambda: True

    ble_like = cast(BLEClient, client)
    assert validator._check_existing_client(ble_like, None, None) is True
    assert validator._check_existing_client(ble_like, "dummy", "dummy") is True
    assert (
        validator._check_existing_client(client, "something-else", None) is False  # type: ignore[arg-type]
    )


def test_get_existing_client_if_valid_uses_last_request_snapshot() -> None:
    """_get_existing_client_if_valid should validate against a lock-protected request snapshot."""

    iface = object.__new__(BLEInterface)
    cast(Any, iface)._state_lock = threading.RLock()
    cast(Any, iface)._last_connection_request = "old-request"
    cast(Any, iface)._state_manager = SimpleNamespace(_is_connected=True)
    cast(Any, iface)._disconnect_notified = False

    class _Client:
        def isConnected(self) -> bool:
            cast(Any, iface)._last_connection_request = "new-request"
            return True

    class _Validator:
        def __init__(self) -> None:
            self.seen_last_request: str | None = None

        def _check_existing_client(
            self,
            _client: Any,
            _normalized_request: str | None,
            last_request: str | None,
        ) -> bool:
            self.seen_last_request = last_request
            return last_request == "old-request"

    client = _Client()
    validator = _Validator()
    cast(Any, iface).client = client
    cast(Any, iface)._connection_validator = validator

    result = BLEInterface._get_existing_client_if_valid(iface, normalized_request="any")

    assert cast(object, result) is client
    assert validator.seen_last_request == "old-request"


def test_close_idempotent(monkeypatch: pytest.MonkeyPatch) -> None:
    """Test that close() is idempotent and only calls disconnect once."""
    client = DummyClient()
    iface = _build_interface(monkeypatch, client)

    iface.close()
    iface.close()
    iface.close()  # Call multiple times to ensure idempotency

    assert client.disconnect_calls == 1
    assert client.close_calls == 1


@pytest.mark.parametrize("exc_cls", [BleakError, RuntimeError, OSError])
def test_close_handles_errors(
    monkeypatch: pytest.MonkeyPatch,
    exc_cls: type[Exception],
) -> None:
    """Test that close() handles various exception types gracefully."""
    # pub already imported at top as mesh_iface_module.pub

    calls: list[tuple[str, dict[str, object]]] = []

    def _capture(topic: str, **kwargs: object) -> None:
        """Record a published pubsub message for test inspection.

        Appends (topic, kwargs) to the module-level `calls` list.

        Parameters
        ----------
        topic : str
            Pubsub topic identifier.
        **kwargs
            Additional message fields to capture.
        """
        calls.append((topic, kwargs))

    monkeypatch.setattr(pub, "sendMessage", _capture)

    client = DummyClient(disconnect_exception=exc_cls("boom"))
    iface = _build_interface(monkeypatch, client)

    iface.close()

    assert client.disconnect_calls == 1
    assert client.close_calls == 1
    assert (
        sum(
            1
            for t, kw in calls
            if t == "meshtastic.connection.status" and kw.get("connected") is False
        )
        == 1
    )

    client = DummyClient(disconnect_exception=OSError("Permission denied"))
    iface = _build_interface(monkeypatch, client)

    iface.close()

    assert client.disconnect_calls == 1
    assert client.close_calls == 1


def test_close_skips_disconnect_when_interpreter_finalizing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """close() should avoid scheduling disconnect coroutines during finalization."""
    client = DummyClient()
    iface = _build_interface(monkeypatch, client)

    monkeypatch.setattr(
        "meshtastic.interfaces.ble.connection.sys.is_finalizing",
        lambda: True,
    )

    iface.close()

    assert client.disconnect_calls == 0
    assert client.close_calls == 0


def test_close_closes_discovery_manager_before_receive_thread_join(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """close() should stop discovery before attempting receive-thread joins."""
    client = DummyClient()
    iface = _build_interface(monkeypatch, client, start_receive_thread=False)
    discovery_closed = threading.Event()
    join_called = threading.Event()
    stop_worker = threading.Event()

    class _DiscoveryManager:
        def close(self) -> None:
            discovery_closed.set()

    cast(Any, iface)._discovery_manager = _DiscoveryManager()
    receive_thread = threading.Thread(
        target=lambda: stop_worker.wait(1.0),
        name="BLEReceiveTest",
    )
    receive_thread.start()
    iface._receiveThread = receive_thread

    def _assert_join_after_discovery_close(
        _thread: threading.Thread, timeout: float | None = None
    ) -> None:
        """Assert discovery closes before join and then join the receive thread."""
        assert discovery_closed.is_set()
        join_called.set()
        stop_worker.set()
        _thread.join(timeout=timeout)

    monkeypatch.setattr(
        iface.thread_coordinator,
        "_join_thread",
        _assert_join_after_discovery_close,
    )

    iface.close()
    assert discovery_closed.is_set()
    assert join_called.is_set()
    receive_thread.join(timeout=0.5)
    assert not receive_thread.is_alive()


def test_close_clears_ble_threads(monkeypatch: pytest.MonkeyPatch) -> None:
    """Closing the interface should leave no BLE* threads running."""
    # threading already imported at top

    client = DummyClient()
    iface = _build_interface(monkeypatch, client)

    iface.close()

    # Poll for thread cleanup with a reasonable timeout
    max_wait_time = 1.0  # Maximum time to wait for thread cleanup
    poll_interval = 0.05  # Time between checks
    deadline = time.monotonic() + max_wait_time
    lingering = []  # Initialize to ensure it's defined outside the loop

    while time.monotonic() < deadline:
        # Check for specific BLE interface threads that should be cleaned up
        # Exclude singleton threads that persist across interface instances
        lingering = [
            thread.name
            for thread in threading.enumerate()
            if thread.name.startswith("BLE")
            and thread.name not in ("BLEClient", "BLECoroutineRunner")
        ]

        if not lingering:
            break  # No lingering threads found

        time.sleep(poll_interval)

    assert (
        not lingering
    ), f"Found lingering BLE threads after {max_wait_time}s: {lingering}"


@pytest.mark.parametrize("exc_type", [RuntimeError, OSError])
def test_receive_thread_specific_exceptions(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    exc_type: type[Exception],
) -> None:
    """Verify that the BLE receive thread treats specific exceptions as fatal: it logs a fatal error message and invokes the interface's close().

    The test injects a client whose read_gatt_char raises the given exception type,
    triggers the receive loop, and asserts that the fatal log entry is present and that close() was called.
    """
    # logging and threading already imported at top

    # Set logging level to DEBUG to capture debug messages
    caplog.set_level(logging.DEBUG)

    class ExceptionClient(DummyClient):
        """Mock client that raises specific exceptions for testing."""

        def __init__(self, exception_type: type[Exception]) -> None:
            """Create a test BLE client configured to raise the given exception from its faulting methods.

            Parameters
            ----------
            exception_type : type | Exception
                Exception class or exception instance that the client will raise when its faulting methods are invoked.
            """
            super().__init__()
            self.exception_type = exception_type

        def read_gatt_char(self, *_args: object, **_kwargs: object) -> bytes:
            """Raise the client's configured exception to simulate a failing GATT characteristic read.

            Raises
            ------
            Exception
                An instance of `self.exception_type` constructed with the message "test".
            """
            raise self.exception_type("test")

    caplog.clear()

    client = ExceptionClient(exc_type)
    iface = _build_interface(monkeypatch, client, start_receive_thread=False)
    close_called = _attach_close_monitor(monkeypatch, iface)

    # Exercise the receive loop synchronously for deterministic assertions.
    iface._want_receive = True
    with iface._state_lock:
        cast(Any, iface).client = client

    iface._read_trigger.set()
    iface._receive_from_radio_impl()

    assert "Fatal error in BLE receive thread" in caplog.text
    assert (
        close_called.is_set()
    ), f"Expected close() to be called for {exc_type.__name__}"

    # Clean up
    iface._want_receive = False
    caplog.clear()
    try:
        iface.close()
    except Exception as exc:  # noqa: BLE001 - cleanup best-effort in tests
        # Log for visibility; still allow test to proceed with cleanup.
        logging.warning("Cleanup error in iface.close(): %r", exc)


def test_bleak_error_transient_retry_logic(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Verify that BleakError in the receive thread goes through transient retry logic.

    The interface should retry on transient BleakError before giving up and closing.

    Raises
    ------
    BleakError
    """
    caplog.set_level(logging.DEBUG)

    class BleakErrorClient(DummyClient):
        """Mock client that raises BleakError for testing retry logic."""

        def __init__(self) -> None:
            """Initialize the instance and set the read operation counter to 0."""
            super().__init__()
            self.read_count = 0

        def read_gatt_char(self, *_args: object, **_kwargs: object) -> bytes:
            """Simulate a GATT characteristic read that increments self.read_count and always fails.

            Increments self.read_count and then raises a BleakError with the message "transient error".

            Raises
            ------
            BleakError
                Always raised with message "transient error".
            """
            self.read_count += 1
            raise BleakError("transient error")

    client = BleakErrorClient()
    iface = _build_interface(monkeypatch, client, start_receive_thread=False)
    close_called = _attach_close_monitor(monkeypatch, iface)

    iface._want_receive = True

    with iface._state_lock:
        cast(Any, iface).client = client

    iface._read_trigger.set()
    iface._receive_from_radio_impl()

    assert "Transient BLE read error, retrying" in caplog.text
    assert "Fatal BLE read error after retries" in caplog.text
    assert close_called.is_set()

    # Clean up
    iface._want_receive = False
    try:
        iface.close()
    except Exception as exc:  # noqa: BLE001 - cleanup best-effort in tests
        logging.warning("Cleanup error in iface.close(): %r", exc)


def test_log_notification_registration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Test that log notifications are properly registered for both legacy and current log UUIDs."""
    # UUID constants already imported at top as ble_mod.FROMNUM_UUID, ble_mod.LEGACY_LOGRADIO_UUID, ble_mod.LOGRADIO_UUID

    class MockClientWithLogChars(DummyClient):
        """Mock client that has log characteristics."""

        def __init__(self) -> None:
            """Initialize the mock BLE client and its notification/characteristic tracking.

            Attributes
            ----------
            start_notify_calls : list
                Recorded calls to start_notify as tuples of the arguments passed.
            has_characteristic_map : dict
                Maps characteristic UUID strings to booleans indicating presence. Initially sets
                LEGACY_LOGRADIO_UUID, LOGRADIO_UUID, and FROMNUM_UUID to True.
            """
            super().__init__()
            self.start_notify_calls: list[tuple[object, object]] = []
            self.has_characteristic_map = {
                LEGACY_LOGRADIO_UUID: True,
                LOGRADIO_UUID: True,
                FROMNUM_UUID: True,
            }

        def has_characteristic(self, uuid: str) -> bool:
            """Determine whether the client exposes a GATT characteristic identified by the given UUID.

            Parameters
            ----------
            uuid : uuid.UUID or hashable
                Characteristic UUID or key used to look up the client's characteristic map.

            Returns
            -------
            bool
                `True` if the UUID is present in the client's characteristic map, `False` otherwise.
            """
            return self.has_characteristic_map.get(uuid, False)

        def start_notify(self, *_args: object, **_kwargs: object) -> None:
            """Record a notification registration by saving the characteristic UUID and its handler.

            If called with at least two positional arguments, treats the first as the characteristic UUID and the second as the notification handler, and appends the pair to self.start_notify_calls. Any additional positional or keyword arguments are accepted and ignored.
            """
            # Extract uuid and handler from args if available
            if len(_args) >= 2:
                uuid, handler = _args[0], _args[1]
                self.start_notify_calls.append((uuid, handler))

    client = MockClientWithLogChars()
    iface = _build_interface(monkeypatch, client)

    # Call _register_notifications to test log notification setup
    iface._register_notifications(cast(BLEClient, client))

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

    assert callable(
        legacy_call[1]
    ), "Legacy log notification should register a callable handler"
    assert callable(
        current_call[1]
    ), "Current log notification should register a callable handler"
    assert callable(
        fromnum_call[1]
    ), "FROMNUM notification should register a callable handler"

    iface.close()


def test_register_notifications_retries_fromnum_notify_acquired_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """_register_notifications should retry FROMNUM notify once on BlueZ 'Notify acquired'."""

    class MockClientNotifyAcquired(DummyClient):
        """Mock client that fails the first FROMNUM notify start with Notify acquired."""

        def __init__(self) -> None:
            super().__init__()
            self.fromnum_start_attempts = 0

        def has_characteristic(self, uuid: str) -> bool:
            return uuid == FROMNUM_UUID

        def start_notify(self, *args: Any, **kwargs: Any) -> None:
            _ = kwargs
            if args and args[0] == FROMNUM_UUID:
                self.fromnum_start_attempts += 1
                if self.fromnum_start_attempts == 1:
                    raise BleakDBusError(
                        "org.bluez.Error.Failed",
                        ["Notify acquired"],
                    )

    client = MockClientNotifyAcquired()
    iface = _build_interface(monkeypatch, client, start_receive_thread=False)

    iface._register_notifications(cast(BLEClient, client))

    assert client.fromnum_start_attempts == 2
    assert client.stop_notify_calls == [FROMNUM_UUID]
    with iface._state_lock:
        assert iface._fromnum_notify_enabled is True

    iface.close()


def test_register_notifications_re_raises_non_notify_acquired_dbus_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """_register_notifications should re-raise FROMNUM start_notify DBus errors not matching Notify acquired."""

    class MockClientFatalFromNumNotify(DummyClient):
        """Mock client that always raises a non-recoverable DBus notify error."""

        def has_characteristic(self, uuid: str) -> bool:
            return uuid == FROMNUM_UUID

        def start_notify(self, *args: Any, **kwargs: Any) -> None:
            _ = kwargs
            if args and args[0] == FROMNUM_UUID:
                raise BleakDBusError(
                    "org.bluez.Error.Failed",
                    ["AlreadyConnected"],
                )

    client = MockClientFatalFromNumNotify()
    iface = _build_interface(monkeypatch, client, start_receive_thread=False)

    with pytest.raises(BleakDBusError):
        iface._register_notifications(cast(BLEClient, client))

    assert client.stop_notify_calls == []

    iface.close()


def test_register_notifications_falls_back_to_polling_after_repeated_notify_acquired(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Repeated FROMNUM notify-acquired errors should not fail connect; polling fallback is enabled."""

    class MockClientPersistentNotifyAcquired(DummyClient):
        """Mock client that always returns Notify acquired for FROMNUM start_notify."""

        def __init__(self) -> None:
            super().__init__()
            self.fromnum_start_attempts = 0

        def has_characteristic(self, uuid: str) -> bool:
            return uuid == FROMNUM_UUID

        def start_notify(self, *args: Any, **kwargs: Any) -> None:
            _ = kwargs
            if args and args[0] == FROMNUM_UUID:
                self.fromnum_start_attempts += 1
                raise BleakDBusError(
                    "org.bluez.Error.Failed",
                    ["Notify acquired"],
                )

    client = MockClientPersistentNotifyAcquired()
    iface = _build_interface(monkeypatch, client, start_receive_thread=False)

    iface._register_notifications(cast(BLEClient, client))

    expected_attempts = ble_mod.BLEConfig.SERVICE_CHARACTERISTIC_RETRY_COUNT + 1
    assert client.fromnum_start_attempts == expected_attempts
    assert len(client.stop_notify_calls) == expected_attempts
    assert all(call == FROMNUM_UUID for call in client.stop_notify_calls)
    with iface._state_lock:
        assert iface._fromnum_notify_enabled is False

    iface.close()


def test_read_from_radio_with_retries_polling_mode_does_single_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Polling fallback mode should perform a single read attempt on empty payloads."""

    class EmptyReadClient(DummyClient):
        """Mock client that records read count and always returns empty payloads."""

        def __init__(self) -> None:
            super().__init__()
            self.read_count = 0
            self.last_timeout: float | None = None

        def read_gatt_char(self, *_args: Any, **_kwargs: Any) -> bytes:
            self.read_count += 1
            timeout_value = _kwargs.get("timeout")
            self.last_timeout = cast(float | None, timeout_value)
            return b""

    client = EmptyReadClient()
    iface = _build_interface(monkeypatch, client, start_receive_thread=False)

    result = iface._read_from_radio_with_retries(
        cast(BLEClient, client),
        retry_on_empty=False,
    )

    assert result is None
    assert client.read_count == 1
    assert client.last_timeout == ble_mod.BLEConfig.RECEIVE_WAIT_TIMEOUT

    iface.close()


def test_close_unsubscribes_tracked_notifications(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """close() should best-effort stop tracked notifications before client teardown."""
    client = DummyClient()
    iface = _build_interface(monkeypatch, client, start_receive_thread=False)

    iface._register_notifications(cast(BLEClient, client))
    assert len(iface._notification_manager) > 0

    iface.close()

    assert FROMNUM_UUID in client.stop_notify_calls


def test_reconnect_scheduler_tracks_threads() -> None:
    """ReconnectScheduler should start at most one reconnect thread and respect closing state."""

    state_manager = BLEStateManager()
    shutdown_event = threading.Event()

    class StubCoordinator:
        """Thread coordinator stub used by reconnect scheduler tests."""

        def __init__(self) -> None:
            """Initialize the instance and prepare storage for items created during tests.

            Creates an empty `created` list used to record items that this helper constructs.
            """
            self.created: list[SimpleNamespace] = []

        def _create_thread(
            self,
            target: Callable[..., object],
            name: str,
            *,
            daemon: bool = True,
            args: tuple[object, ...] = (),
            kwargs: dict[str, object] | None = None,
        ) -> SimpleNamespace:
            """Create a lightweight thread-like SimpleNamespace, record it in self.created, and return it.

            Parameters
            ----------
            target : callable
                The callable intended to run when the thread is started.
            name : str
                Identifier for the thread-like object.
            daemon : bool
                Whether the thread-like object is considered a daemon. (Default value = True)
            args : tuple
                Positional arguments associated with the target. (Default value = ())
            kwargs : dict | None
                Keyword arguments associated with the target; treated as {} when None. (Default value = None)

            Returns
            -------
            SimpleNamespace
                An object with attributes `target`, `args`, `name`, `daemon`, `kwargs`, and `started`, plus an `is_alive()` callable that returns whether `started` is True.
            """
            thread = SimpleNamespace(
                target=target,
                args=args,
                name=name,
                daemon=daemon,
                kwargs=kwargs if kwargs is not None else {},
                started=False,
            )
            thread.is_alive = lambda: thread.started
            self.created.append(thread)
            return thread

        @staticmethod
        def _start_thread(thread: SimpleNamespace) -> None:
            """Mark a thread-like object's `started` attribute as True.

            Parameters
            ----------
            thread : object
                Thread-like object with a writable `started` attribute.
            """
            thread.started = True

    worker = SimpleNamespace(
        attempt_reconnect_loop=lambda *_args, **_kwargs: None,
        _is_connection_closing=False,
        _can_initiate_connection=True,
    )
    coordinator = StubCoordinator()
    scheduler = ReconnectScheduler(  # noqa: PLR0913  # type: ignore[arg-type]
        state_manager,
        state_manager._lock,
        coordinator,  # type: ignore[arg-type]
        worker,  # type: ignore[arg-type]
    )

    assert scheduler._schedule_reconnect(True, shutdown_event) is True
    assert len(coordinator.created) == 1
    assert scheduler._schedule_reconnect(True, shutdown_event) is False

    scheduler._clear_thread_reference()
    assert scheduler._reconnect_thread is None

    assert state_manager._transition_to(ConnectionState.CONNECTING) is True
    assert state_manager._transition_to(ConnectionState.CONNECTED) is True
    assert state_manager._transition_to(ConnectionState.DISCONNECTING) is True
    worker._is_connection_closing = True
    assert scheduler._schedule_reconnect(True, shutdown_event) is False


def test_reconnect_worker_successful_attempt() -> None:
    """ReconnectWorker should reconnect and clear thread references on success; cleanup/resubscribe are handled by the interface layer, not the worker."""

    class StubPolicy:
        """Reconnect policy stub for successful reconnect tests."""

        def __init__(self) -> None:
            """Initialize the stub retry policy used by reconnect tests.

            Sets initial state for test assertions.

            Attributes
            ----------
            reset_called : bool
                True if reset() has been invoked.
            _attempt_count : int
                Number of connection attempts recorded.
            """
            self.reset_called = False
            self._attempt_count = 0

        def _reset(self) -> None:
            """Reset the retry policy to its initial state.

            Sets the internal attempt counter to 0 and records that a reset occurred by setting `reset_called` to True.
            """
            self.reset_called = True
            self._attempt_count = 0

        def _get_attempt_count(self) -> int:
            """Return the internal attempt count for ReconnectWorker tests."""
            return self._attempt_count

        def _next_attempt(self) -> tuple[float, bool]:
            """Determine the delay before the next retry and whether another attempt should be made.

            Increments the internal attempt counter as a side effect.

            Returns
            -------
            tuple
                (delay_seconds, continue_retry)
                delay_seconds (float): Seconds to wait before the next attempt.
                continue_retry (bool): `True` to perform another attempt, `False` otherwise.
            """
            self._attempt_count += 1
            return 0.1, False

    class DummyInterface:
        """Minimal interface stub used by reconnect worker tests.

        Methods
        -------
        connect(address)
        """

        BLEError = RuntimeError

        def __init__(self) -> None:
            """Create a minimal stub interface for reconnect-related tests.

            Initializes lightweight test doubles and records connect invocations.

            Attributes
            ----------
            _reconnect_policy : StubPolicy
                Retry/backoff policy used by reconnect attempts.
            _notification_manager : _ReconnectTestNotificationManager
                Tracks cleanup and resubscribe requests.
            _state_manager : types.SimpleNamespace
                Exposes `is_closing` (bool) to simulate shutdown state.
            _reconnect_scheduler : _ReconnectTestScheduler
                Manages reconnect thread reference and clearing.
            auto_reconnect : bool
                Whether automatic reconnect attempts are enabled.
            _is_connection_closing : bool
                Simulates an in-progress connection close.
            _is_connection_connected : bool
                Simulates an active connection state.
            address : str
                Device address used for connect attempts.
            client : object
                Placeholder BLE client object.
            connect_calls : list
                Records addresses passed to `connect` for assertions in tests.
            """
            self._reconnect_policy = StubPolicy()
            self._notification_manager = _ReconnectTestNotificationManager()
            self._state_manager = SimpleNamespace(is_closing=False)
            self._reconnect_scheduler = _ReconnectTestScheduler()
            self.auto_reconnect = True
            self._is_connection_closing = False
            self._is_connection_connected = False
            self.address = "addr"
            self.client = object()
            self.connect_calls: list[str] = []

        def connect(self, address: str, **_kwargs: object) -> None:
            """Record that a connection was attempted for the given device address by appending it to this instance's `connect_calls` list.

            Parameters
            ----------
            address : str
                Bluetooth address or device identifier that was attempted and will be appended to `connect_calls`.
            """
            self.connect_calls.append(address)

    iface = DummyInterface()
    worker = ReconnectWorker(iface, iface._reconnect_policy)  # type: ignore[arg-type]
    worker._attempt_reconnect_loop(
        threading.Event(),
        on_exit=iface._reconnect_scheduler._clear_thread_reference,
    )

    assert iface.connect_calls == ["addr"]
    assert iface._notification_manager.cleaned == 0
    assert len(iface._notification_manager.resubscribed) == 0
    assert iface._reconnect_policy.reset_called is True
    assert iface._reconnect_scheduler.cleared is True


def test_reconnect_worker_respects_retry_limits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ensure ReconnectWorker respects the retry policy and stops after the allowed attempts when connect continuously fails.

    Simulates an interface whose connect() always raises and a LimitedPolicy that permits a single retry. Verifies that:
    - connect() is attempted the expected number of times (2 attempts),
    - no notification resubscriptions occur,
    - the retry delay from the policy (0.25) was waited once,
    - the reconnect policy was reset,
    - the reconnect scheduler cleared its thread reference.

    Raises
    ------
    BLEError
    """

    sleep_calls: list[float] = []

    # Mock shutdown_event.wait to capture the sleep delay instead of actually waiting
    def mock_wait(timeout: float | None = None) -> bool:
        """Simulate waiting for a shutdown event while recording requested timeouts.

        Records the provided timeout value into the surrounding `sleep_calls` list when not None, and always returns `False` to indicate the wait timed out (not interrupted by a shutdown/notification).

        Parameters
        ----------
        timeout : float | None
            Duration in seconds to wait; if None, no value is recorded. (Default value = None)

        Returns
        -------
        bool
            `False` to indicate a timeout (i.e., the wait was not interrupted).
        """
        if timeout is not None:
            sleep_calls.append(timeout)
        # Return False to simulate timeout (not interrupted by shutdown)
        return False

    class LimitedPolicy:
        """Reconnect policy stub with a bounded retry window."""

        def __init__(self) -> None:
            """Initialize a stub reconnect policy for tests, resetting counters and flags.

            Attributes
            ----------
            reset_called : bool
                True if reset() has been invoked.
            attempts : int
                Number of connection attempts recorded.
            """
            self.reset_called = False
            self.attempts = 0

        def _reset(self) -> None:
            """Mark the retry policy as reset and clear its attempt counter.

            Sets the internal `reset_called` flag to True and resets `attempts` to 0.
            """
            self.reset_called = True
            self.attempts = 0

        def _get_attempt_count(self) -> int:
            """Return the internal attempt count for ReconnectWorker tests."""
            return self.attempts

        def _next_attempt(self) -> tuple[float, bool]:
            """Return the delay before the next retry and whether another retry should be attempted.

            Returns
            -------
            tuple
                (delay_seconds, continue_flag)
                delay_seconds (float): Seconds to wait before the next retry (0.25).
                continue_flag (bool): `True` if another retry should be attempted for the current policy cycle, `False` otherwise.
            """
            self.attempts += 1
            return 0.25, self.attempts < 2

    class FailingInterface:
        """Interface stub whose connect path always fails.

        Methods
        -------
        connect(*_args, **_kwargs)
        """

        BLEError = RuntimeError

        def __init__(self) -> None:
            """Initialize a minimal stub interface used by reconnect tests.

            Attributes
            ----------
            _reconnect_policy : LimitedPolicy
                Policy controlling reconnect attempts.
            _notification_manager : _ReconnectTestNotificationManager
                Manages notification cleanup and resubscription.
            _state_manager : SimpleNamespace
                Runtime state flags (contains `is_closing`).
            _reconnect_scheduler : _ReconnectTestScheduler
                Scheduler that manages reconnect threads.
            auto_reconnect : bool
                Whether automatic reconnect attempts are enabled.
            _is_connection_closing : bool
                Indicates an in-progress connection close.
            _is_connection_connected : bool
                Indicates whether the interface is currently connected.
            address : str
                Remote device address used for connection attempts.
            client : object | None
                Placeholder for the BLE client instance (initially None).
            connect_attempts : int
                Counter of connect() invocation attempts.
            """
            self._reconnect_policy = LimitedPolicy()
            self._notification_manager = _ReconnectTestNotificationManager(
                fail_on_resubscribe=True
            )
            self._state_manager = SimpleNamespace(is_closing=False)
            self._reconnect_scheduler = _ReconnectTestScheduler()
            self.auto_reconnect = True
            self._is_connection_closing = False
            self._is_connection_connected = False
            self.address = "addr"
            self.client = None
            self.connect_attempts = 0

        def connect(self, *_args: object, **_kwargs: object) -> None:
            """Simulate a failing connection attempt for tests and record the attempt.

            Increments the instance's `connect_attempts` counter and raises an error to emulate a failed connection.

            Raises
            ------
            self.BLEError
                raised with message "boom".
            BLEError
            """
            self.connect_attempts += 1
            raise self.BLEError("boom")

    iface = FailingInterface()
    worker = ReconnectWorker(iface, iface._reconnect_policy)  # type: ignore[arg-type]
    shutdown_event = threading.Event()
    monkeypatch.setattr(shutdown_event, "wait", mock_wait)

    worker._attempt_reconnect_loop(
        shutdown_event,
        on_exit=iface._reconnect_scheduler._clear_thread_reference,
    )

    assert iface.connect_attempts == 2
    assert iface._notification_manager.cleaned == 0
    assert sleep_calls == [0.25]
    assert iface._reconnect_policy.reset_called is True
    assert iface._reconnect_scheduler.cleared is True
