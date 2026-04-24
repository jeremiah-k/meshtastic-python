"""Targeted runtime tests for BLE connection shutdown behavior."""

from __future__ import annotations

from threading import Event, RLock
from typing import cast
from unittest.mock import MagicMock

import pytest

from bleak.exc import BleakDBusError

from meshtastic.interfaces.ble.client import BLEClient
from meshtastic.interfaces.ble.connection import (
    ClientManager,
    ConnectionOrchestrator,
)
from meshtastic.interfaces.ble.constants import DISCONNECT_TIMEOUT_SECONDS
from meshtastic.interfaces.ble.errors import BLEErrorHandler

pytestmark = pytest.mark.unit


class _ErrorHandler:
    """Minimal error handler compatible with ClientManager cleanup hooks."""

    def safe_cleanup(self, func, cleanup_name=None):  # type: ignore[no-untyped-def]
        func()
        return True


class _DummyClient:
    """Minimal BLE client double for shutdown behavior tests."""

    def __init__(self, *, connected: bool, close_accepts_timeout: bool = True) -> None:
        self._closed = False
        self._connected = connected
        self.bleak_client = object()
        self.disconnect_calls = 0
        self.close_calls = 0
        self.last_disconnect_timeout: float | None = None
        self.last_close_timeout: float | None = None
        self._close_accepts_timeout = close_accepts_timeout

    def is_connected(self) -> bool:
        return self._connected

    def disconnect(self, *, await_timeout: float) -> None:
        self.disconnect_calls += 1
        self.last_disconnect_timeout = await_timeout

    def close(self, timeout: float | None = None) -> None:  # type: ignore[no-untyped-def]
        self.close_calls += 1
        self.last_close_timeout = timeout


class _LegacyDummyClient:
    """Legacy BLE client double that does not accept timeout in close()."""

    def __init__(self, *, connected: bool) -> None:
        self._connected = connected
        self.bleak_client = object()
        self.disconnect_calls = 0
        self.close_calls = 0
        self.last_disconnect_timeout: float | None = None

    def is_connected(self) -> bool:
        return self._connected

    def disconnect(self, *, await_timeout: float) -> None:
        self.disconnect_calls += 1
        self.last_disconnect_timeout = await_timeout

    def close(self) -> None:
        self.close_calls += 1


def _make_client_manager() -> ClientManager:
    return ClientManager(
        state_manager=MagicMock(),
        state_lock=RLock(),
        thread_coordinator=MagicMock(),
        error_handler=cast(BLEErrorHandler, _ErrorHandler()),
    )


def test_safe_close_client_skips_disconnect_for_disconnected_client() -> None:
    """Disconnected clients should not trigger an unnecessary disconnect timeout path."""
    manager = _make_client_manager()
    client = _DummyClient(connected=False)
    done = Event()

    manager._safe_close_client(cast(BLEClient, client), event=done)

    assert done.is_set()
    assert client.disconnect_calls == 0
    assert client.close_calls == 1


def test_safe_close_client_disconnects_connected_client_before_close() -> None:
    """Connected clients should still run bounded disconnect before close."""
    manager = _make_client_manager()
    client = _DummyClient(connected=True)
    done = Event()

    manager._safe_close_client(cast(BLEClient, client), event=done)

    assert done.is_set()
    assert client.disconnect_calls == 1
    assert client.last_disconnect_timeout == DISCONNECT_TIMEOUT_SECONDS
    assert client.close_calls == 1


def test_safe_close_client_passes_timeout_to_close() -> None:
    """Bounded disconnect timeout should flow through to client.close()."""
    manager = _make_client_manager()
    client = _DummyClient(connected=True)
    done = Event()
    custom_timeout = 3.5

    manager._safe_close_client(
        cast(BLEClient, client),
        event=done,
        disconnect_timeout=custom_timeout,
    )

    assert done.is_set()
    assert client.disconnect_calls == 1
    assert client.last_disconnect_timeout == custom_timeout
    assert client.close_calls == 1
    assert client.last_close_timeout == custom_timeout


def test_safe_close_client_fallback_for_legacy_close() -> None:
    """Legacy client.close() without timeout should still be called once."""
    manager = _make_client_manager()
    client = _LegacyDummyClient(connected=True)
    done = Event()

    manager._safe_close_client(
        cast(BLEClient, client),
        event=done,
        disconnect_timeout=2.0,
    )

    assert done.is_set()
    assert client.disconnect_calls == 1
    assert client.close_calls == 1


def test_safe_close_client_reraises_unrelated_typeerror() -> None:
    """Unrelated TypeError from close() should propagate, not be swallowed."""
    manager = _make_client_manager()

    class _BadClient:
        def __init__(self) -> None:
            self.bleak_client = object()
            self._closed = False

        def is_connected(self) -> bool:
            return True

        def disconnect(self, *, await_timeout: float) -> None:
            pass

        def close(self, timeout: float | None = None) -> None:
            raise TypeError("something else broke")

    client = _BadClient()
    done = Event()

    with pytest.raises(TypeError, match="something else broke"):
        manager._safe_close_client(cast(BLEClient, client), event=done)


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

    with pytest.raises(BleakDBusError, match="Device or resource busy"):
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
    orchestrator._client_manager_safe_close_client.assert_called_with(client)


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
