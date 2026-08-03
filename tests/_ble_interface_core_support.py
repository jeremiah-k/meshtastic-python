"""Shared helpers for decomposed legacy BLE core tests."""

# pylint: disable=redefined-outer-name

import asyncio
import threading
from types import SimpleNamespace, TracebackType
from typing import TYPE_CHECKING, Any, Callable, Literal, Protocol, cast

import pytest
from bleak.backends.device import BLEDevice

# Import meshtastic modules for use in tests
from meshtastic.interfaces.ble import (
    BLEInterface,
)
from meshtastic.interfaces.ble.state import BLEStateManager

# Import common fixtures

pytestmark = pytest.mark.unit

_MAX_SPURIOUS_CONNECT_WAIT_CALLS_BEFORE_FAIL = 10
_MAX_SPURIOUS_CLOSE_WAIT_CALLS_BEFORE_FAIL = 50
START_FAILED_MSG = "start failed"
SAFE_EXECUTE_UNEXPECTED_ERROR_MSG = (
    "safe_execute() got an unexpected keyword argument 'error_msg'"
)
SAFE_EXECUTE_POSITIONAL_MISMATCH_ERROR_MSG = (
    "takes 1 positional argument but 2 positional arguments were given"
)
SAFE_EXECUTE_DIFFERENT_TYPE_ERROR_MSG = "different type error"
SAFE_EXECUTE_KEYWORD_CALL_FAILED_MSG = "keyword call failed"
SAFE_EXECUTE_HANDLER_TYPE_ERROR_MSG = "handler raised type error"
SAFE_EXECUTE_CALLABLE_ONLY_ERROR_MSG = "callable-only failed"
SAFE_EXECUTE_LEGACY_POSITIONAL_MISMATCH_ERROR_MSG = "legacy positional mismatch"


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


def _clear_management_handler(iface: BLEInterface) -> None:
    """Reset cached management-command handler for collaborator-refresh tests."""
    iface._management_command_handler = None


if TYPE_CHECKING:
    # pylint: disable=unnecessary-ellipsis

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
    from pubsub import pub as pub


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
