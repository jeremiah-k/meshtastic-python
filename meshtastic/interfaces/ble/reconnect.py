"""Auto-reconnect scheduling and worker logic."""

from __future__ import annotations

import sys
from threading import Event, Thread, current_thread as threading_current_thread
from typing import Optional, TYPE_CHECKING

from .config import (
    BLEConfig,
    ReconnectPolicy,
    logger,
    _sleep as config_sleep,
)
from .coordination import ThreadCoordinator
from .state import BLEStateManager

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .interface import BLEInterface
    from .config import ReconnectPolicy


class ReconnectScheduler:
    """Manage lifecycle of the reconnect worker thread."""

    def __init__(
        self,
        state_manager: BLEStateManager,
        state_lock,
        thread_coordinator: ThreadCoordinator,
        interface: "BLEInterface",
        reconnect_policy: "ReconnectPolicy | None" = None,
    ):
        self.state_manager = state_manager
        self.state_lock = state_lock
        self.thread_coordinator = thread_coordinator
        self.interface = interface
        self._reconnect_policy = reconnect_policy or ReconnectPolicy()
        self._reconnect_worker = ReconnectWorker(interface, self._reconnect_policy)
        self._reconnect_thread: Optional[Thread] = None

    def schedule_reconnect(self, auto_reconnect: bool, shutdown_event: Event) -> bool:
        if not auto_reconnect:
            return False
        if self.state_manager.is_closing:
            logger.debug(
                "Skipping auto-reconnect scheduling because interface is closing."
            )
            return False

        with self.state_lock:
            if self._reconnect_thread and self._reconnect_thread.is_alive():
                logger.debug(
                    "Auto-reconnect already in progress; skipping new attempt."
                )
                return False

            thread = self.thread_coordinator.create_thread(
                target=self._reconnect_worker.attempt_reconnect_loop,
                args=(auto_reconnect, shutdown_event),
                name="BLEAutoReconnect",
                daemon=True,
            )
            self._reconnect_thread = thread
            self.thread_coordinator.start_thread(thread)
            return True

    def clear_thread_reference(self) -> None:
        with self.state_lock:
            if self._reconnect_thread is _current_thread():
                self._reconnect_thread = None


class ReconnectWorker:
    """Perform blocking reconnect attempts with policy-driven backoff."""

    def __init__(self, interface: "BLEInterface", reconnect_policy: "ReconnectPolicy"):
        self.interface = interface
        self.reconnect_policy = reconnect_policy

    def attempt_reconnect_loop(
        self, auto_reconnect: bool, shutdown_event: Event
    ) -> None:
        self.reconnect_policy.reset()
        try:
            while not shutdown_event.is_set():
                if self.interface._state_manager.is_closing or not auto_reconnect:
                    logger.debug(
                        "Auto-reconnect aborted because interface is closing or disabled."
                    )
                    return
                try:
                    attempt_num = self.reconnect_policy.get_attempt_count() + 1
                    logger.info(
                        "Attempting BLE auto-reconnect (attempt %d).", attempt_num
                    )
                    self.interface._notification_manager.cleanup_all()
                    self.interface.connect(self.interface.address)
                    timeout = (
                        BLEConfig.NOTIFICATION_START_TIMEOUT
                        if BLEConfig.NOTIFICATION_START_TIMEOUT is not None
                        else BLEConfig.GATT_IO_TIMEOUT
                    )
                    if self.interface.client:
                        self.interface._notification_manager.resubscribe_all(
                            self.interface.client,
                            timeout=timeout,
                        )
                    logger.info(
                        "BLE auto-reconnect succeeded after %d attempts.", attempt_num
                    )
                    return
                except self.interface.BLEError as err:
                    if self.interface._state_manager.is_closing or not auto_reconnect:
                        logger.debug(
                            "Auto-reconnect cancelled after failure due to shutdown/disable."
                        )
                        return
                    logger.warning(
                        "Auto-reconnect attempt %d failed: %s",
                        self.reconnect_policy.get_attempt_count(),
                        err,
                    )
                except Exception:
                    if self.interface._state_manager.is_closing or not auto_reconnect:
                        logger.debug(
                            "Auto-reconnect cancelled after unexpected failure due to shutdown/disable."
                        )
                        return
                    logger.exception(
                        "Unexpected error during auto-reconnect attempt %d",
                        self.reconnect_policy.get_attempt_count(),
                    )

                if self.interface.is_connection_closing or not auto_reconnect:
                    return
                (
                    sleep_delay,
                    should_retry,
                ) = self.reconnect_policy.next_attempt()
                if not should_retry:
                    logger.info("Auto-reconnect reached maximum retry limit.")
                    return
                logger.debug(
                    "Waiting %.2f seconds before next reconnect attempt.", sleep_delay
                )
                _sleep(sleep_delay)
        finally:
            self.interface._reconnect_scheduler.clear_thread_reference()


def _current_thread():
    """Resolve current_thread allowing monkeypatching via the public shim."""
    ble_module = sys.modules.get("meshtastic.ble_interface")
    if ble_module and hasattr(ble_module, "current_thread"):
        return ble_module.current_thread()
    return threading_current_thread()


def _sleep(delay: float) -> None:
    """Sleep helper that honors monkeypatches via the public shim."""
    ble_module = sys.modules.get("meshtastic.ble_interface")
    func = getattr(ble_module, "_sleep", None) if ble_module else None
    (func or config_sleep)(delay)


__all__ = ["ReconnectScheduler", "ReconnectWorker"]
