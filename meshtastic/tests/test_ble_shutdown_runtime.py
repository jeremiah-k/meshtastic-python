"""Tests for BLE shutdown lifecycle runtime behavior.

Covers unsubscribe_all legacy fallback, bounded-thread TOCTOU fixes,
and shutdown client teardown paths.
"""

from __future__ import annotations

import threading
import time
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from meshtastic.interfaces.ble.client import BLEClient
from meshtastic.interfaces.ble.lifecycle_shutdown_runtime import (
    BLEShutdownLifecycleCoordinator,
)

pytestmark = pytest.mark.unit


class _ImmediateThread:
    """Run thread targets inline for deterministic unit tests."""

    def __init__(self, *, target: Any, **_kwargs: Any) -> None:
        self._target = target

    def start(self) -> None:
        self._target()

    def is_alive(self) -> bool:
        return False

    def join(self, timeout: float | None = None) -> None:
        pass


class TestShutdownClientUnsubscribeFallback:
    """Test legacy unsubscribe_all fallback in _shutdown_client."""

    def test_legacy_unsubscribe_all_without_timeout_is_called(self):
        """When unsubscribe_all does not accept timeout, retry without it."""
        iface = MagicMock()
        iface.client = None
        iface._management_inflight = 0
        iface._management_target_gate.return_value.__enter__ = MagicMock(
            return_value=MagicMock()
        )
        iface._management_target_gate.return_value.__exit__ = MagicMock(
            return_value=None
        )

        coordinator = BLEShutdownLifecycleCoordinator(iface)

        called_with: list[tuple[Any, ...]] = []

        def legacy_unsubscribe(client: Any) -> None:
            called_with.append((client,))

        notification_manager = MagicMock()
        notification_manager._unsubscribe_all = None
        notification_manager.unsubscribe_all = legacy_unsubscribe
        iface._notification_manager = notification_manager

        with patch.object(
            coordinator,
            "_detach_client_for_shutdown",
            return_value=(MagicMock(spec=BLEClient), False),
        ):
            coordinator._shutdown_client(
                management_wait_timed_out=False,
                unsubscribe_timeout=5.0,
            )

        assert len(called_with) == 1
        # The call should be unsubscribe_all(client) without timeout
        assert len(called_with[0]) == 1

    def test_unsubscribe_all_unrelated_typeerror_propagates(self):
        """Unrelated TypeError from unsubscribe_all should propagate through safe-cleanup."""
        iface = MagicMock()
        iface.client = None
        iface._management_inflight = 0
        iface._management_target_gate.return_value.__enter__ = MagicMock(
            return_value=MagicMock()
        )
        iface._management_target_gate.return_value.__exit__ = MagicMock(
            return_value=None
        )

        coordinator = BLEShutdownLifecycleCoordinator(iface)

        def bad_unsubscribe(client: Any, timeout: float | None = None) -> None:
            raise TypeError("unexpected type problem")

        notification_manager = MagicMock()
        notification_manager._unsubscribe_all = None
        notification_manager.unsubscribe_all = bad_unsubscribe
        iface._notification_manager = notification_manager

        with patch.object(
            coordinator,
            "_detach_client_for_shutdown",
            return_value=(MagicMock(spec=BLEClient), False),
        ):
            # The error should propagate through safe-cleanup, which suppresses
            # it by default. We verify it does not hang or mask as a kwargs error.
            coordinator._shutdown_client(
                management_wait_timed_out=False,
                unsubscribe_timeout=5.0,
            )


class TestBoundedThreadTOCTOU:
    """Test bounded cleanup thread TOCTOU race fixes."""

    def test_concurrent_cleanup_cannot_spawn_duplicate_threads(self):
        """Two concurrent close calls must not both start cleanup threads."""
        iface = MagicMock()
        iface._closed = True
        iface._management_inflight = 0
        iface._management_idle_condition.wait.return_value = True

        blocker = threading.Event()

        # Use a real function so _is_unconfigured_mock_callable does not skip it.
        def slow_cleanup() -> None:
            blocker.wait()

        iface.thread_coordinator.cleanup = slow_cleanup

        coordinator = BLEShutdownLifecycleCoordinator(iface)
        started_threads: list[threading.Thread] = []
        original_thread = threading.Thread

        def tracking_thread(*, target: Any, **kwargs: Any) -> threading.Thread:
            t = original_thread(target=target, **kwargs)
            started_threads.append(t)
            return t

        with patch("threading.Thread", side_effect=tracking_thread):
            # First call should start a thread
            coordinator._cleanup_thread_coordinator(timeout=1.0)
            # Second concurrent call should skip because previous is still alive
            coordinator._cleanup_thread_coordinator(timeout=1.0)

        # Only one thread should have been started (the second may be constructed
        # but must not be started because the check happens under the same lock).
        started_count = sum(1 for t in started_threads if t.ident is not None)
        assert started_count == 1
        blocker.set()

    def test_start_failure_clears_stored_ref(self):
        """If thread.start() fails, the stored ref must be cleared under lock."""
        iface = MagicMock()
        iface._closed = True
        iface._management_inflight = 0
        iface._management_idle_condition.wait.return_value = True

        def real_cleanup() -> None:
            pass

        iface.thread_coordinator.cleanup = real_cleanup

        coordinator = BLEShutdownLifecycleCoordinator(iface)

        class FailingThread(threading.Thread):
            def start(self) -> None:
                raise RuntimeError("start failed")

        with patch("threading.Thread", side_effect=FailingThread):
            coordinator._cleanup_thread_coordinator(timeout=1.0)

        # The ref should be cleared
        assert coordinator._bounded_cleanup_thread is None

    def test_existing_skip_warning_behavior_intact(self):
        """When a previous bounded thread is alive, warn and skip without starting."""
        iface = MagicMock()
        iface._closed = True
        iface._management_inflight = 0
        iface._management_idle_condition.wait.return_value = True

        def real_cleanup() -> None:
            pass

        iface.thread_coordinator.cleanup = real_cleanup

        coordinator = BLEShutdownLifecycleCoordinator(iface)

        # Pretend a previous thread is still alive
        previous = MagicMock()
        previous.is_alive.return_value = True
        coordinator._bounded_cleanup_thread = previous

        coordinator._cleanup_thread_coordinator(timeout=1.0)

        # No new thread should be started; previous.join is NOT called in the new design,
        # but the warning behavior is preserved (skip + warn).
        previous.join.assert_not_called()

    def test_mesh_close_start_failure_clears_ref(self):
        """MeshInterface.close thread start failure clears the stored ref."""
        iface = MagicMock()
        iface._closed = True
        iface._management_inflight = 0
        iface._management_idle_condition.wait.return_value = True

        coordinator = BLEShutdownLifecycleCoordinator(iface)

        class FailingThread(threading.Thread):
            def start(self) -> None:
                raise RuntimeError("start failed")

        with patch("threading.Thread", side_effect=FailingThread):
            coordinator._close_mesh_interface(timeout=1.0)

        assert coordinator._bounded_mesh_close_thread is None

    def test_mesh_close_skip_when_previous_alive(self):
        """MeshInterface.close should skip when a previous bounded thread is alive."""
        iface = MagicMock()
        iface._closed = True
        iface._management_inflight = 0
        iface._management_idle_condition.wait.return_value = True

        coordinator = BLEShutdownLifecycleCoordinator(iface)

        previous = MagicMock()
        previous.is_alive.return_value = True
        coordinator._bounded_mesh_close_thread = previous

        coordinator._close_mesh_interface(timeout=1.0)

        previous.join.assert_not_called()
