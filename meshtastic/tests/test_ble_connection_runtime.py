"""Targeted runtime tests for BLE connection shutdown behavior."""

from __future__ import annotations

import asyncio
from concurrent.futures import Future
from threading import Event, RLock, Thread
from typing import Any, Coroutine, cast
from unittest.mock import MagicMock

import pytest
from bleak.exc import BleakDBusError

from meshtastic.interfaces.ble.client import BLEClient
from meshtastic.interfaces.ble.constants import DISCONNECT_TIMEOUT_SECONDS
from meshtastic.interfaces.ble.connection import (
    ClientManager,
    ConnectionOrchestrator,
)
from meshtastic.interfaces.ble.errors import BLEDBusTransportError, BLEErrorHandler
from meshtastic.interfaces.ble.runner import BLECoroutineRunner

pytestmark = pytest.mark.unit


class _ErrorHandler:
    """Minimal error handler compatible with ClientManager cleanup hooks."""

    def safe_cleanup(self, func, cleanup_name=None):  # type: ignore[no-untyped-def]
        func()
        return True


class _DummyClient:
    """Minimal BLE client double for shutdown behavior tests."""

    def __init__(self) -> None:
        self.close_calls: int = 0
        self.last_close_timeout: float | None = None

    def close(self, timeout: float | None = None) -> None:
        self.close_calls += 1
        self.last_close_timeout = timeout


class _LegacyDummyClient:
    """Legacy BLE client double that does not accept timeout in close()."""

    def __init__(self) -> None:
        self.close_calls: int = 0

    def close(self) -> None:
        self.close_calls += 1


class _ImmediateRunner:
    """Execute transport coroutines on an isolated event-loop thread."""

    _thread: Thread | None = None

    def _run_coroutine_threadsafe(self, coro: Coroutine[Any, Any, Any]) -> Future[Any]:
        future: Future[Any] = Future()

        def _run() -> None:
            try:
                future.set_result(asyncio.run(coro))
            except Exception as exc:  # noqa: BLE001 - mirror Future propagation
                future.set_exception(exc)

        thread = Thread(target=_run)
        self._thread = thread
        thread.start()
        thread.join()
        self._thread = None
        return future


class _BleakTransport:
    """Bleak transport double that records cleanup attempts."""

    def __init__(
        self,
        *,
        connected: bool,
        disconnect_error: Exception | None = None,
    ) -> None:
        self.address: str = "AA:BB:CC:DD:EE:FF"
        self.is_connected: bool = connected
        self.disconnect_calls: int = 0
        self.disconnect_error: Exception | None = disconnect_error

    async def disconnect(self) -> None:
        self.disconnect_calls += 1
        if self.disconnect_error is not None:
            raise self.disconnect_error


def _make_ble_client(
    *,
    connected: bool,
    disconnect_error: Exception | None = None,
) -> tuple[BLEClient, _BleakTransport]:
    client = BLEClient()
    transport = _BleakTransport(
        connected=connected,
        disconnect_error=disconnect_error,
    )
    client.bleak_client = cast(Any, transport)
    client._runner = cast(BLECoroutineRunner, _ImmediateRunner())
    return client, transport


def _make_client_manager() -> ClientManager:
    return ClientManager(
        state_manager=MagicMock(),
        state_lock=RLock(),
        thread_coordinator=MagicMock(),
        error_handler=cast(BLEErrorHandler, _ErrorHandler()),
    )


def test_safe_close_client_releases_transport_after_remote_disconnect() -> None:
    """Manager cleanup should release transport after a remote disconnect."""
    manager = _make_client_manager()
    client, transport = _make_ble_client(connected=False)
    done = Event()

    manager._safe_close_client(
        client,
        event=done,
        disconnect_timeout=1.5,
    )

    assert done.is_set()
    assert transport.disconnect_calls == 1
    assert client.bleak_client is None


def test_safe_close_client_disconnects_connected_transport_once() -> None:
    """Manager cleanup should disconnect a connected transport exactly once."""
    manager = _make_client_manager()
    client, transport = _make_ble_client(connected=True)
    done = Event()

    manager._safe_close_client(client, event=done)

    assert done.is_set()
    assert transport.disconnect_calls == 1
    assert client.bleak_client is None


def test_safe_close_client_clears_transport_after_disconnect_failure() -> None:
    """Cleanup should finish and clear the transport after disconnect failure."""
    manager = _make_client_manager()
    client, transport = _make_ble_client(
        connected=False,
        disconnect_error=RuntimeError("transport cleanup failed"),
    )
    done = Event()

    manager._safe_close_client(client, event=done)

    assert done.is_set()
    assert transport.disconnect_calls == 1
    assert client.bleak_client is None


def test_close_forwards_timeout_as_disconnect_await_timeout() -> None:
    """BLEClient.close(timeout=...) must forward the value as disconnect(await_timeout=...)."""
    client, _transport = _make_ble_client(connected=True)
    captured: dict[str, float | None] = {}

    def _spy_disconnect(*, await_timeout: float | None = None) -> None:
        captured["await_timeout"] = await_timeout

    client.disconnect = _spy_disconnect  # type: ignore[assignment]

    custom_timeout = 4.25
    client.close(timeout=custom_timeout)

    assert captured.get("await_timeout") == custom_timeout


def test_close_default_timeout_uses_disconnect_constant() -> None:
    """close() with no timeout must forward DISCONNECT_TIMEOUT_SECONDS as await_timeout."""
    client, _transport = _make_ble_client(connected=True)
    captured: dict[str, float | None] = {}

    def _spy_disconnect(*, await_timeout: float | None = None) -> None:
        captured["await_timeout"] = await_timeout

    client.disconnect = _spy_disconnect  # type: ignore[assignment]

    client.close()

    assert captured.get("await_timeout") == DISCONNECT_TIMEOUT_SECONDS


def test_close_is_idempotent_and_disconnects_once() -> None:
    """Repeated close() calls must disconnect the transport at most once."""
    client, transport = _make_ble_client(connected=True)

    client.close()
    client.close()

    assert transport.disconnect_calls == 1
    assert client.bleak_client is None


def test_safe_close_client_passes_timeout_to_close() -> None:
    """Bounded disconnect timeout should flow through to client.close()."""
    manager = _make_client_manager()
    client = _DummyClient()
    done = Event()
    custom_timeout = 3.5

    manager._safe_close_client(
        cast(BLEClient, client),
        event=done,
        disconnect_timeout=custom_timeout,
    )

    assert done.is_set()
    assert client.close_calls == 1
    assert client.last_close_timeout == custom_timeout


def test_safe_close_client_fallback_for_legacy_close() -> None:
    """Legacy client.close() without timeout should still be called once."""
    manager = _make_client_manager()
    client = _LegacyDummyClient()
    done = Event()

    manager._safe_close_client(
        cast(BLEClient, client),
        event=done,
        disconnect_timeout=2.0,
    )

    assert done.is_set()
    assert client.close_calls == 1


def test_safe_close_client_reraises_unrelated_typeerror() -> None:
    """Unrelated TypeError from close() should propagate, not be swallowed."""
    manager = _make_client_manager()

    class _BadClient:
        def close(self, timeout: float | None = None) -> None:
            raise TypeError("something else broke")

    client = _BadClient()
    done = Event()

    with pytest.raises(TypeError, match="something else broke"):
        manager._safe_close_client(cast(BLEClient, client), event=done)
    assert done.is_set()


def test_stale_cleanup_retry_failure_does_not_fall_through() -> None:
    """Stale-cleanup retry failure must propagate, not trigger generic retry."""
    orchestrator = ConnectionOrchestrator(
        interface=MagicMock(),
        validator=MagicMock(),
        client_manager=MagicMock(),
        discovery_manager=MagicMock(),
        state_manager=MagicMock(),
        state_lock=RLock(),
        thread_coordinator=MagicMock(),
    )

    client = MagicMock()
    retry_err = BleakDBusError("org.bluez.Error.Failed", ["Device or resource busy"])

    orchestrator._raise_if_interface_closing = MagicMock()  # type: ignore[method-assign]
    orchestrator._client_manager_create_client = MagicMock(return_value=client)  # type: ignore[method-assign]
    orchestrator._client_manager_connect_client = MagicMock(  # type: ignore[method-assign]
        side_effect=BleakDBusError(
            "org.bluez.Error.InProgress", ["operation already in progress"]
        )
    )
    orchestrator._client_manager_safe_close_client = MagicMock()  # type: ignore[method-assign]
    orchestrator._should_attempt_stale_bluez_cleanup = MagicMock(return_value=True)  # type: ignore[method-assign]
    orchestrator._attempt_stale_bluez_cleanup = MagicMock(return_value=True)  # type: ignore[method-assign]
    orchestrator._retry_direct_connect_after_cleanup = MagicMock(  # type: ignore[method-assign]
        side_effect=retry_err
    )

    with pytest.raises(BLEDBusTransportError, match="BLE DBus transport error"):
        orchestrator._attempt_direct_connect(
            target_address="AA:BB:CC:DD:EE:FF",
            explicit_address=True,
            normalized_target="aabbccddeeff",
            on_disconnect_func=MagicMock(),
            pair_on_connect=False,
            direct_connect_timeout=5.0,
            register_notifications_func=MagicMock(),
            on_connected_func=MagicMock(),
            emit_connected_side_effects=True,
        )

    # Generic retry path must not be reached.
    orchestrator._client_manager_safe_close_client.assert_called_once_with(client)


def test_stale_cleanup_retry_mismatch_still_propagates() -> None:
    """BLEAddressMismatchError from stale-cleanup retry must propagate."""
    from meshtastic.interfaces.ble.errors import BLEAddressMismatchError

    orchestrator = ConnectionOrchestrator(
        interface=MagicMock(),
        validator=MagicMock(),
        client_manager=MagicMock(),
        discovery_manager=MagicMock(),
        state_manager=MagicMock(),
        state_lock=RLock(),
        thread_coordinator=MagicMock(),
    )

    client = MagicMock()
    mismatch = BLEAddressMismatchError("address mismatch")

    orchestrator._raise_if_interface_closing = MagicMock()  # type: ignore[method-assign]
    orchestrator._client_manager_create_client = MagicMock(return_value=client)  # type: ignore[method-assign]
    orchestrator._client_manager_connect_client = MagicMock(  # type: ignore[method-assign]
        side_effect=BleakDBusError("org.bluez.Error.InProgress", ["in progress"])
    )
    orchestrator._client_manager_safe_close_client = MagicMock()  # type: ignore[method-assign]
    orchestrator._should_attempt_stale_bluez_cleanup = MagicMock(return_value=True)  # type: ignore[method-assign]
    orchestrator._attempt_stale_bluez_cleanup = MagicMock(return_value=True)  # type: ignore[method-assign]
    orchestrator._retry_direct_connect_after_cleanup = MagicMock(  # type: ignore[method-assign]
        side_effect=mismatch
    )

    with pytest.raises(BLEAddressMismatchError, match="address mismatch"):
        orchestrator._attempt_direct_connect(
            target_address="AA:BB:CC:DD:EE:FF",
            explicit_address=True,
            normalized_target="aabbccddeeff",
            on_disconnect_func=MagicMock(),
            pair_on_connect=False,
            direct_connect_timeout=5.0,
            register_notifications_func=MagicMock(),
            on_connected_func=MagicMock(),
            emit_connected_side_effects=True,
        )
