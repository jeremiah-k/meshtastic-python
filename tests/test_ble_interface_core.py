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
from typing import (
    TYPE_CHECKING,
    Any,
    Callable,
    Literal,
    Protocol,
    cast,
)

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
    CONNECTION_ERROR_LOST_OWNERSHIP,
    ERROR_INTERFACE_CLOSING,
    ERROR_MANAGEMENT_ADDRESS_EMPTY,
    ERROR_MANAGEMENT_AWAIT_TIMEOUT_INVALID,
    ERROR_MANAGEMENT_CONNECTING,
    ERROR_MANAGEMENT_TARGET_CHANGED,
    ERROR_TRUST_ADDRESS_NOT_RESOLVED,
    ERROR_TRUST_BLUETOOTHCTL_MISSING,
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


def _pin_trust_environment(
    monkeypatch: pytest.MonkeyPatch,
    *,
    run: Callable[..., object] | None = None,
) -> None:
    """Pin trust() host dependencies so guard-path tests stay hermetic."""
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


def test_ble_interface_repr_includes_non_default_flags(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """repr() should include non-default flags and debug output."""
    iface = _build_interface(monkeypatch, DummyClient(), start_receive_thread=False)

    def _debug_sink(_line: str) -> None:
        return None

    iface.debugOut = _debug_sink
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

    iface.pair(confirm=True, await_timeout=12.5)
    assert client.pair_calls == 1
    assert client.pair_kwargs == [{"confirm": True}]
    assert client.pair_await_timeouts == [12.5]
    iface.close()


def test_ble_interface_unpair_prefers_active_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """unpair() should delegate to the active matching client when connected."""
    client = DummyClient()
    iface = _build_interface(monkeypatch, client, start_receive_thread=False)

    iface.unpair(await_timeout=8.0)
    assert client.unpair_calls == 1
    assert client.unpair_await_timeouts == [8.0]
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

    iface.pair("mesh-node", confirm=True, await_timeout=7.0)

    assert existing_client.pair_calls == 1
    assert existing_client.pair_kwargs == [{"confirm": True}]
    assert existing_client.pair_await_timeouts == [7.0]
    iface.close()


def test_ble_interface_pair_uses_temporary_client_when_disconnected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """pair() should create and clean up a temporary BLEClient when no active client matches."""
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

    iface.pair("mesh-node", confirm=True, await_timeout=7.0)
    assert pair_kwargs == [{"confirm": True}]
    assert pair_await_timeouts == [7.0]
    assert cleanup_calls == [temp_client]
    iface.close()


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

    def _record_pair(*, await_timeout: float | None = None, **kwargs: object) -> None:
        _ = (await_timeout, kwargs)
        command_calls.append("pair")

    def _record_unpair(*, await_timeout: float | None = None) -> None:
        _ = await_timeout
        command_calls.append("unpair")

    client.pair = _record_pair
    client.unpair = _record_unpair
    replacement_client.pair = _record_pair
    replacement_client.unpair = _record_unpair

    @contextlib.contextmanager
    def _swap_target_gate(_target_address: str) -> Iterator[None]:
        with iface._state_lock:
            iface.client = replacement_client
            iface.address = replacement_address
            iface._state_manager._reset_to_disconnected()
            assert iface._state_manager._transition_to(ConnectionState.CONNECTING)
            assert iface._state_manager._transition_to(ConnectionState.CONNECTED)
        yield

    monkeypatch.setattr(iface, "_management_target_gate", _swap_target_gate)

    with pytest.raises(BLEInterface.BLEError, match=ERROR_MANAGEMENT_TARGET_CHANGED):
        if method_name == "pair":
            iface.pair(confirm=True, await_timeout=7.0)
        else:
            iface.unpair(await_timeout=7.0)

    assert command_calls == []
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
    [None, 0.0, -1.0, float("nan"), float("inf"), cast(Any, True)],
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
            iface.client = replacement_client
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


def test_ble_interface_trust_prefers_stderr_in_failure_details(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """trust() should surface stderr first when bluetoothctl fails."""
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

    with pytest.raises(BLEInterface.BLEError, match="specific failure"):
        iface.trust("mesh-node")

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
    with pytest.raises(BLEInterface.BLEError, match="only supported on Linux"):
        iface.trust("AA:BB:CC:DD:EE:FF")
    iface.close()


def test_ble_interface_trust_rejects_non_positive_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """trust() should fail fast on non-positive timeouts."""
    iface = _build_interface(monkeypatch, DummyClient(), start_receive_thread=False)
    _pin_trust_environment(monkeypatch)

    with pytest.raises(BLEInterface.BLEError, match=ERROR_TRUST_INVALID_TIMEOUT):
        iface.trust("AA:BB:CC:DD:EE:FF", timeout=0)

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

    with pytest.raises(BLEInterface.BLEError, match="closing"):
        iface.trust("mesh-node")

    assert find_device_called is False
    assert subprocess_called is False


def test_ble_interface_trust_does_not_hold_interface_locks_during_subprocess(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """trust() should let close() mark shutdown before bluetoothctl returns."""
    iface = _build_interface(monkeypatch, DummyClient(), start_receive_thread=False)
    trust_target = "AA:BB:CC:DD:EE:FF"
    with iface._state_lock:
        assert iface.client is not None
        iface.client.address = trust_target
        iface.client.bleak_client.address = trust_target
        iface.address = trust_target
    run_started = threading.Event()
    allow_run_return = threading.Event()
    close_done = threading.Event()
    close_started = threading.Event()
    trust_errors: list[BaseException] = []

    def _blocking_run(*_args: object, **_kwargs: object) -> SimpleNamespace:
        run_started.set()
        assert allow_run_return.wait(timeout=1.0)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    _pin_trust_environment(monkeypatch, run=_blocking_run)

    def _run_trust() -> None:
        try:
            iface.trust(trust_target, timeout=7.0)
        except BaseException as exc:  # pragma: no cover - failure captured below
            trust_errors.append(exc)

    def _close_iface() -> None:
        try:
            close_started.set()
            iface.close()
        finally:
            close_done.set()

    trust_thread = threading.Thread(target=_run_trust, daemon=True)
    trust_thread.start()
    assert run_started.wait(timeout=1.0)

    close_thread = threading.Thread(target=_close_iface, daemon=True)
    close_thread.start()
    assert close_started.wait(timeout=1.0)
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
    with iface._state_lock:
        assert iface._closed is True


def test_ble_interface_close_serializes_with_management_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """close() should not mark the interface closed while a management op holds the lock."""
    iface = _build_interface(monkeypatch, DummyClient(), start_receive_thread=False)
    close_done = threading.Event()
    close_started = threading.Event()

    def _close_iface() -> None:
        try:
            close_started.set()
            iface.close()
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
    assert close_done.is_set() is True


def test_ble_interface_close_does_not_wait_for_connect_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """close() should still start shutdown while the connect lock is held."""
    iface = _build_interface(monkeypatch, DummyClient(), start_receive_thread=False)
    close_done = threading.Event()

    def _close_iface() -> None:
        try:
            iface.close()
        finally:
            close_done.set()

    with iface._connect_lock:
        close_thread = threading.Thread(target=_close_iface, daemon=True)
        close_thread.start()
        assert close_done.wait(timeout=1.0)
        with iface._state_lock:
            assert iface._closed is True

    close_thread.join(timeout=2.0)
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
    iface = object.__new__(BLEInterface)
    iface._state_manager = BLEStateManager()
    iface._state_lock = threading.RLock()
    iface._connect_lock = threading.RLock()
    iface._disconnect_lock = threading.Lock()
    iface._closed = False
    iface.address = None
    iface.client = None
    iface._disconnect_notified = False
    iface._last_connection_request = None
    iface.pair_on_connect = False
    iface._connection_alias_key = None
    iface._ever_connected = False
    iface._read_retry_count = 0

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
            iface.client = client
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
        assert iface.client is not connected_client
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
        iface.client = active_client  # type: ignore[assignment]
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

    assert iface.client is active_client
    assert iface._disconnect_notified is False
    assert reconnect_calls == []
    assert disconnected_calls == []

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
        iface: BLEInterface, _address: str | None = None
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
        _ = _address
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
        iface: BLEInterface, _address: str | None = None
    ) -> DummyClient:
        _ = _address
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
            iface.client = connected_client
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

    assert result is connected_client
    assert finalized_lock_states == [False]
    iface.close()


def test_connect_raises_when_client_becomes_stale_after_gate_finalization(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """connect() should not return a client that lost ownership after finalization."""
    target_address = "AA:BB:CC:DD:EE:03"
    replacement_address = "AA:BB:CC:DD:EE:04"
    iface = object.__new__(BLEInterface)
    iface._state_manager = BLEStateManager()
    iface._state_lock = threading.RLock()
    iface._connect_lock = threading.RLock()
    iface._disconnect_lock = threading.Lock()
    iface._closed = False
    iface.address = None
    iface.client = None
    iface._disconnect_notified = False
    iface._last_connection_request = None
    iface.pair_on_connect = False
    iface._connection_alias_key = None
    iface._ever_connected = False
    iface._read_retry_count = 0
    connected_callbacks: list[bool] = []
    connected_client = DummyClient()
    connected_client.address = target_address
    connected_client.bleak_client = SimpleNamespace(address=target_address)
    finalized_clients: list[BLEClient] = []

    monkeypatch.setattr(iface, "_connected", lambda: connected_callbacks.append(True))
    monkeypatch.setattr(iface, "_validate_connection_preconditions", lambda: None)
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
            iface.client = connected_client
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
            iface.client = replacement_client
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

    assert finalized_clients == [connected_client]
    assert connected_callbacks == []


def test_connect_raises_when_shutdown_wins_after_gate_finalization(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """connect() should surface shutdown when close() wins after gate finalization."""
    target_address = "AA:BB:CC:DD:EE:05"
    iface = object.__new__(BLEInterface)
    iface._state_manager = BLEStateManager()
    iface._state_lock = threading.RLock()
    iface._connect_lock = threading.RLock()
    iface._disconnect_lock = threading.Lock()
    iface._closed = False
    iface.address = None
    iface.client = None
    iface._disconnect_notified = False
    iface._last_connection_request = None
    iface.pair_on_connect = False
    iface._connection_alias_key = None
    iface._ever_connected = False
    iface._read_retry_count = 0
    connected_callbacks: list[bool] = []
    connected_client = DummyClient()
    connected_client.address = target_address
    connected_client.bleak_client = SimpleNamespace(address=target_address)
    finalized_clients: list[BLEClient] = []

    monkeypatch.setattr(iface, "_connected", lambda: connected_callbacks.append(True))
    monkeypatch.setattr(iface, "_validate_connection_preconditions", lambda: None)
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
            iface.client = connected_client
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

    assert finalized_clients == [connected_client]
    assert connected_callbacks == []


def test_transient_read_retry_uses_zero_based_delay(monkeypatch):
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


def test_receive_loop_outer_catch_routes_to_disconnect_handler(monkeypatch):
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


def test_start_receive_thread_skips_when_interface_closed(monkeypatch):
    """Receive thread start helper should no-op once the interface is closed.

    Raises
    ------
    AssertionError
    """
    client = DummyClient()
    iface = _build_interface(monkeypatch, client)
    iface.close()

    def should_not_create_thread(*_args, **_kwargs):
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


def test_find_device_multiple_matches_raises():
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


def test_find_device_direct_connect_preserves_raw_address():
    """Direct-connect fallback should keep the raw BLE address format."""
    iface = object.__new__(ble_mod.BLEInterface)
    iface._discovery_manager = SimpleNamespace(
        _discover_devices=lambda _addr: []
    )  # type: ignore[assignment]

    address = "AA:BB:CC:DD:EE:FF"
    direct_device = BLEInterface.findDevice(iface, address)

    assert direct_device.address == address
    assert direct_device.name == address


def test_discovery_manager_filters_meshtastic_devices(monkeypatch):
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


def test_discovery_manager_filters_targeted_scan_to_whitelist_match(monkeypatch):
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
    """DiscoveryManager should reject duck-typed clients whose _discover is not callable."""

    class InvalidDiscoveryClient:
        _discover = None

    manager = DiscoveryManager(
        client_factory=lambda **_kwargs: InvalidDiscoveryClient()
    )

    with pytest.raises(DiscoveryClientError, match="_discover"):
        manager._discover_devices(address=None)

    assert manager._client is None


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
        def _discover(**_kwargs: Any) -> dict[str, Any]:
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

    monkeypatch.setattr(discovery_mod.asyncio, "get_running_loop", _get_running_loop)
    monkeypatch.setattr(
        discovery_mod, "_await_close_result", _await_close_result_passthrough
    )
    monkeypatch.setattr(discovery_mod.asyncio, "wait_for", _wait_for_passthrough)
    monkeypatch.setattr(
        discovery_mod.inspect,
        "iscoroutine",
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
        assert task not in discovery_mod._PENDING_DISCOVERY_CLOSE_TASKS
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

    monkeypatch.setattr(discovery_mod.asyncio, "get_running_loop", _get_running_loop)
    monkeypatch.setattr(
        discovery_mod, "_await_close_result", _await_close_result_passthrough
    )
    monkeypatch.setattr(discovery_mod.asyncio, "wait_for", _wait_for_passthrough)

    _close_discovery_client_best_effort(_Client())

    with discovery_mod._PENDING_DISCOVERY_CLOSE_TASKS_LOCK:
        assert task in discovery_mod._PENDING_DISCOVERY_CLOSE_TASKS
    assert len(task._callbacks) == 1

    task._callbacks[0](task)
    with discovery_mod._PENDING_DISCOVERY_CLOSE_TASKS_LOCK:
        assert task not in discovery_mod._PENDING_DISCOVERY_CLOSE_TASKS


def test_discovery_manager_raises_when_factory_returns_none() -> None:
    """DiscoveryManager should raise DiscoveryClientError for None-returning factories."""
    manager = DiscoveryManager(client_factory=lambda: None)

    with pytest.raises(DiscoveryClientError, match="returned None"):
        manager._discover_devices(address=None)


def test_parse_scan_response_prefers_exact_name_before_normalized_match():
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


def test_filter_devices_rejects_ambiguous_normalized_name_matches():
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


def test_discovery_manager_destructor_tolerates_unusable_lock() -> None:
    """DiscoveryManager.__del__ should fall back when _client_lock is not lock-like."""
    manager = object.__new__(DiscoveryManager)
    manager._client_lock = cast(Any, object())  # type: ignore[attr-defined]
    manager._client = cast(Any, object())  # type: ignore[attr-defined]

    manager.__del__()

    assert manager._client is None  # type: ignore[attr-defined]


def test_connection_validator_enforces_state():
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


def test_connection_validator_existing_client_checks():
    """check_existing_client should allow reuse only when the requested identifier matches."""

    state_manager = BLEStateManager()
    validator = ConnectionValidator(
        state_manager, state_manager._lock, BLEInterface.BLEError
    )
    client = DummyClient()
    client.isConnected = lambda: True

    ble_like = cast(BLEClient, client)
    assert validator._check_existing_client(ble_like, None, None) is True
    assert validator._check_existing_client(ble_like, "dummy", "dummy") is True
    assert (
        validator._check_existing_client(client, "something-else", None) is False  # type: ignore[arg-type]
    )


def test_get_existing_client_if_valid_uses_last_request_snapshot():
    """_get_existing_client_if_valid should validate against a lock-protected request snapshot."""

    iface = object.__new__(BLEInterface)
    iface._state_lock = threading.RLock()  # type: ignore[attr-defined]
    iface._last_connection_request = "old-request"  # type: ignore[attr-defined]
    iface._state_manager = SimpleNamespace(_is_connected=True)  # type: ignore[attr-defined]
    iface._disconnect_notified = False  # type: ignore[attr-defined]

    class _Client:
        def isConnected(self) -> bool:
            iface._last_connection_request = "new-request"  # type: ignore[attr-defined]
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
    iface.client = client  # type: ignore[attr-defined]
    iface._connection_validator = validator  # type: ignore[attr-defined]

    result = BLEInterface._get_existing_client_if_valid(iface, normalized_request="any")

    assert result is client
    assert validator.seen_last_request == "old-request"


def test_close_idempotent(monkeypatch):
    """Test that close() is idempotent and only calls disconnect once."""
    client = DummyClient()
    iface = _build_interface(monkeypatch, client)

    iface.close()
    iface.close()
    iface.close()  # Call multiple times to ensure idempotency

    assert client.disconnect_calls == 1
    assert client.close_calls == 1


@pytest.mark.parametrize("exc_cls", [BleakError, RuntimeError, OSError])
def test_close_handles_errors(monkeypatch, exc_cls):
    """Test that close() handles various exception types gracefully."""
    # pub already imported at top as mesh_iface_module.pub

    calls = []

    def _capture(topic, **kwargs):
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


def test_close_skips_disconnect_when_interpreter_finalizing(monkeypatch):
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

    iface._discovery_manager = _DiscoveryManager()  # type: ignore[assignment]
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


def test_close_clears_ble_threads(monkeypatch):
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
def test_receive_thread_specific_exceptions(monkeypatch, caplog, exc_type):
    """Verify that the BLE receive thread treats specific exceptions as fatal: it logs a fatal error message and invokes the interface's close().

    The test injects a client whose read_gatt_char raises the given exception type,
    triggers the receive loop, and asserts that the fatal log entry is present and that close() was called.
    """
    # logging and threading already imported at top

    # Set logging level to DEBUG to capture debug messages
    caplog.set_level(logging.DEBUG)

    class ExceptionClient(DummyClient):
        """Mock client that raises specific exceptions for testing."""

        def __init__(self, exception_type):
            """Create a test BLE client configured to raise the given exception from its faulting methods.

            Parameters
            ----------
            exception_type : type | Exception
                Exception class or exception instance that the client will raise when its faulting methods are invoked.
            """
            super().__init__()
            self.exception_type = exception_type

        def read_gatt_char(self, *_args, **_kwargs):
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
        iface.client = client  # type: ignore[assignment]

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


def test_bleak_error_transient_retry_logic(monkeypatch, caplog):
    """Verify that BleakError in the receive thread goes through transient retry logic.

    The interface should retry on transient BleakError before giving up and closing.

    Raises
    ------
    BleakError
    """
    caplog.set_level(logging.DEBUG)

    class BleakErrorClient(DummyClient):
        """Mock client that raises BleakError for testing retry logic."""

        def __init__(self):
            """Initialize the instance and set the read operation counter to 0."""
            super().__init__()
            self.read_count = 0

        def read_gatt_char(self, *_args, **_kwargs):
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
        iface.client = client  # type: ignore[assignment]

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


def test_log_notification_registration(monkeypatch):
    """Test that log notifications are properly registered for both legacy and current log UUIDs."""
    # UUID constants already imported at top as ble_mod.FROMNUM_UUID, ble_mod.LEGACY_LOGRADIO_UUID, ble_mod.LOGRADIO_UUID

    class MockClientWithLogChars(DummyClient):
        """Mock client that has log characteristics."""

        def __init__(self):
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
            self.start_notify_calls = []
            self.has_characteristic_map = {
                LEGACY_LOGRADIO_UUID: True,
                LOGRADIO_UUID: True,
                FROMNUM_UUID: True,
            }

        def has_characteristic(self, uuid):
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

        def start_notify(self, *_args, **_kwargs):
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


def test_close_unsubscribes_tracked_notifications(monkeypatch):
    """close() should best-effort stop tracked notifications before client teardown."""
    client = DummyClient()
    iface = _build_interface(monkeypatch, client, start_receive_thread=False)

    iface._register_notifications(cast(BLEClient, client))
    assert len(iface._notification_manager) > 0

    iface.close()

    assert FROMNUM_UUID in client.stop_notify_calls


def test_reconnect_scheduler_tracks_threads():
    """ReconnectScheduler should start at most one reconnect thread and respect closing state."""

    state_manager = BLEStateManager()
    shutdown_event = threading.Event()

    class StubCoordinator:
        """Thread coordinator stub used by reconnect scheduler tests."""

        def __init__(self):
            """Initialize the instance and prepare storage for items created during tests.

            Creates an empty `created` list used to record items that this helper constructs.
            """
            self.created = []

        def _create_thread(self, target, name, *, daemon=True, args=(), kwargs=None):
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
        def _start_thread(thread):
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


def test_reconnect_worker_successful_attempt():
    """ReconnectWorker should reconnect and clear thread references on success; cleanup/resubscribe are handled by the interface layer, not the worker."""

    class StubPolicy:
        """Reconnect policy stub for successful reconnect tests."""

        def __init__(self):
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

        def _reset(self):
            """Reset the retry policy to its initial state.

            Sets the internal attempt counter to 0 and records that a reset occurred by setting `reset_called` to True.
            """
            self.reset_called = True
            self._attempt_count = 0

        def _get_attempt_count(self):
            """Return the internal attempt count for ReconnectWorker tests."""
            return self._attempt_count

        def _next_attempt(self):
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

        def __init__(self):
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
            self.connect_calls = []

        def connect(self, address):
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


def test_reconnect_worker_respects_retry_limits(monkeypatch):
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

    sleep_calls = []

    # Mock shutdown_event.wait to capture the sleep delay instead of actually waiting
    def mock_wait(timeout=None):
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

        def __init__(self):
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

        def _reset(self):
            """Mark the retry policy as reset and clear its attempt counter.

            Sets the internal `reset_called` flag to True and resets `attempts` to 0.
            """
            self.reset_called = True
            self.attempts = 0

        def _get_attempt_count(self):
            """Return the internal attempt count for ReconnectWorker tests."""
            return self.attempts

        def _next_attempt(self):
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

        def __init__(self):
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

        def connect(self, *_args, **_kwargs):
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
