"""Targeted runtime tests for BLE connection shutdown behavior."""

from __future__ import annotations

from threading import Event, RLock
from unittest.mock import MagicMock

import pytest

from meshtastic.interfaces.ble.connection import ClientManager

pytestmark = pytest.mark.unit


class _ErrorHandler:
    """Minimal error handler compatible with ClientManager cleanup hooks."""

    def safe_cleanup(self, func, cleanup_name=None):  # type: ignore[no-untyped-def]
        func()
        return True


class _DummyClient:
    """Minimal BLE client double for shutdown behavior tests."""

    def __init__(self, *, connected: bool) -> None:
        self._closed = False
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
        error_handler=_ErrorHandler(),
    )


def test_safe_close_client_skips_disconnect_for_disconnected_client() -> None:
    """Disconnected clients should not trigger an unnecessary disconnect timeout path."""
    manager = _make_client_manager()
    client = _DummyClient(connected=False)
    done = Event()

    manager._safe_close_client(client, event=done)

    assert done.is_set()
    assert client.disconnect_calls == 0
    assert client.close_calls == 1


def test_safe_close_client_disconnects_connected_client_before_close() -> None:
    """Connected clients should still run bounded disconnect before close."""
    manager = _make_client_manager()
    client = _DummyClient(connected=True)
    done = Event()

    manager._safe_close_client(client, event=done)

    assert done.is_set()
    assert client.disconnect_calls == 1
    assert client.close_calls == 1
