"""BLE reconnection logic and scheduling."""

import logging
from threading import Event, RLock
from typing import TYPE_CHECKING, Callable, Optional

from bleak.exc import BleakDBusError, BleakDeviceNotFoundError, BleakError

from meshtastic.interfaces.ble.constants import DBUS_ERROR_RECONNECT_DELAY, BLEConfig
from meshtastic.interfaces.ble.coordination import ThreadCoordinator, ThreadLike
from meshtastic.interfaces.ble.gating import (
    addr_key,
    is_currently_connected_elsewhere,
)
from meshtastic.interfaces.ble.policies import ReconnectPolicy
from meshtastic.interfaces.ble.state import BLEStateManager

if TYPE_CHECKING:
    from meshtastic.interfaces.ble.interface import BLEInterface

logger = logging.getLogger("meshtastic.ble")


class ReconnectScheduler:
    """Manage lifecycle of the reconnect worker thread."""

    def __init__(
        self,
        state_manager: BLEStateManager,
        state_lock: RLock,
        thread_coordinator: ThreadCoordinator,
        interface: "BLEInterface",
    ) -> None:
        """
        Create a ReconnectScheduler that coordinates background BLE reconnection attempts for a given interface.

        Parameters
        ----------
            state_manager (BLEStateManager): Queries BLE lifecycle state (e.g., whether the interface is closing or a connection is in progress) to decide if reconnects should be scheduled.
            state_lock (RLock): Re-entrant lock that protects access to shared BLE state and the scheduler's internal thread reference.
            thread_coordinator (ThreadCoordinator): Responsible for creating and managing the reconnect worker thread.
            interface (BLEInterface): BLE interface used to perform connection attempts and to which the scheduler will be attached.

        """
        self.state_manager = state_manager
        self.state_lock = state_lock
        self.thread_coordinator = thread_coordinator
        self.interface = interface
        self._reconnect_policy = ReconnectPolicy(
            initial_delay=BLEConfig.AUTO_RECONNECT_INITIAL_DELAY,
            max_delay=BLEConfig.AUTO_RECONNECT_MAX_DELAY,
            backoff=BLEConfig.AUTO_RECONNECT_BACKOFF,
            jitter_ratio=BLEConfig.AUTO_RECONNECT_JITTER_RATIO,
            max_retries=None,
        )
        self._reconnect_worker = ReconnectWorker(interface, self._reconnect_policy)
        self._reconnect_thread: Optional[ThreadLike] = None

    def schedule_reconnect(self, auto_reconnect: bool, shutdown_event: Event) -> bool:
        """
        Schedule a background BLE reconnect worker if auto-reconnect is enabled and no worker is currently running.

        Parameters
        ----------
            auto_reconnect (bool): Whether automatic reconnection is enabled; scheduling is skipped when False.
            shutdown_event (Event): Event the worker observes to stop retrying early during shutdown.

        Returns
        -------
            True if a new reconnect worker thread was created and started, False otherwise.

        """
        if not auto_reconnect:
            return False
        # Use state manager instead of boolean flag
        if self.state_manager.is_closing or not self.state_manager.can_connect:
            logger.debug(
                "Skipping auto-reconnect scheduling because interface is closing or connection already in progress."
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
                kwargs={"on_exit": self.clear_thread_reference},
                name="BLEAutoReconnect",
                daemon=True,
            )
            # Set the thread reference before starting to prevent race conditions
            self._reconnect_thread = thread
        # Start outside the lock: the reference is already set, so concurrent
        # schedulers will see a non-None _reconnect_thread and exit early.
        self.thread_coordinator.start_thread(thread)
        return True

    def clear_thread_reference(self) -> None:
        """
        Clear the internal reference to the running reconnect thread.

        This operation acquires the scheduler's state_lock and sets the internal reconnect thread reference to None to record that no background reconnect worker is active.
        """
        with self.state_lock:
            # Always clear the reference once the worker loop exits to match legacy behavior.
            self._reconnect_thread = None


class ReconnectWorker:
    """Perform blocking reconnect attempts with policy-driven backoff."""

    def __init__(
        self, interface: "BLEInterface", reconnect_policy: ReconnectPolicy
    ) -> None:
        """
        Create a ReconnectWorker bound to a BLE interface and a reconnect policy.

        Parameters:
            interface (BLEInterface): Interface used to initiate connection attempts and to query or modify connection state.
            reconnect_policy (ReconnectPolicy): Backoff and retry policy that controls reconnect timing and attempt state.
        """
        self.interface = interface
        self.reconnect_policy = reconnect_policy

    def _should_abort_reconnect(self, auto_reconnect: bool, context: str = "") -> bool:
        """
        Decide whether the reconnect process should stop based on the interface state and the auto_reconnect flag.

        Parameters:
            auto_reconnect (bool): Whether automatic reconnect is enabled.
            context (str): Optional context string used in debug messages.

        Returns:
            bool: True if reconnection should be aborted, False otherwise.
        """
        if self.interface.is_connection_closing:
            logger.debug(
                "Auto-reconnect aborted%s: interface is closing.",
                f" ({context})" if context else "",
            )
            return True
        if not auto_reconnect:
            logger.debug(
                "Auto-reconnect aborted%s: auto-reconnect disabled.",
                f" ({context})" if context else "",
            )
            return True
        return False

    def attempt_reconnect_loop(  # pylint: disable=R0911
        self,
        auto_reconnect: bool,
        shutdown_event: Event,
        *,
        on_exit: Optional[Callable[[], None]] = None,
    ) -> None:
        """
        Perform the blocking auto-reconnect loop for the bound BLE interface using the configured backoff policy.

        Attempts reconnects until a connection succeeds, the reconnect policy stops further retries, the provided shutdown_event is set, or auto_reconnect is False. Between failed attempts the loop waits according to the policy (adjusted for certain BLE/DBus errors) and exits promptly if shutdown_event is signaled. The optional on_exit callback is invoked when the loop terminates.

        Parameters:
            auto_reconnect (bool): If False, the loop will exit immediately without attempting reconnects.
            shutdown_event (threading.Event): Event that causes the loop to stop as soon as it is set.
            on_exit (Optional[Callable[[], None]]): Callback invoked once when the loop ends.
        """
        self.reconnect_policy.reset()
        interface = self.interface
        override_delay: Optional[float] = None

        try:
            while not shutdown_event.is_set():
                override_delay = None
                if self._should_abort_reconnect(auto_reconnect, "loop start"):
                    return
                attempt_num = self.reconnect_policy.get_attempt_count() + 1
                try:
                    if interface.is_connection_connected:
                        return
                    device_addr = addr_key(getattr(interface, "address", None))
                    # Check if already connected elsewhere before attempting.
                    # connect() enforces this gate as well; this early check avoids
                    # scheduling a full connect path when we already know it will fail.
                    if device_addr and is_currently_connected_elsewhere(
                        device_addr, owner=interface
                    ):
                        logger.info(
                            "Skipping reconnect attempt %d: address %s already connected elsewhere",
                            attempt_num,
                            device_addr,
                        )
                        # Avoid spinning; wait before re-checking the gate.
                        if shutdown_event.wait(
                            timeout=BLEConfig.AUTO_RECONNECT_INITIAL_DELAY
                        ):
                            return
                        continue
                    logger.info(
                        "Attempting BLE auto-reconnect (attempt %d).",
                        attempt_num,
                    )
                    interface.connect(interface.address)
                    logger.info(
                        "BLE auto-reconnect succeeded after %d attempts.",
                        attempt_num,
                    )
                except interface.BLEError as err:
                    if self._should_abort_reconnect(auto_reconnect, "BLEError"):
                        return
                    logger.warning(
                        "Auto-reconnect attempt %d failed: %s",
                        attempt_num,
                        err,
                    )
                except BleakDBusError:
                    if self._should_abort_reconnect(auto_reconnect, "DBusError"):
                        return
                    # DBus errors are often transient on Linux; log as warning since we'll retry
                    logger.warning(
                        "DBus error during auto-reconnect attempt %d",
                        attempt_num,
                        exc_info=True,
                    )
                    # Use longer delay for DBus errors to allow system Bluetooth stack to recover
                    # override_delay is None at loop start; coalesce to 0 for max() comparison
                    override_delay = max(
                        override_delay if override_delay is not None else 0,
                        DBUS_ERROR_RECONNECT_DELAY,
                    )
                    # State transition to ERROR and DISCONNECTED is already handled by
                    # the connection orchestrator, so we don't need to do it here
                except BleakError as err:
                    if self._should_abort_reconnect(auto_reconnect, "BleakError"):
                        return
                    logger.warning(
                        "Auto-reconnect attempt %d failed with BLE error: %s",
                        attempt_num,
                        err,
                    )
                    # Give the adapter a respite before retrying and avoid thrashing scans.
                    delay_hint = (
                        DBUS_ERROR_RECONNECT_DELAY
                        if isinstance(err, BleakDeviceNotFoundError)
                        else BLEConfig.AUTO_RECONNECT_INITIAL_DELAY
                    )
                    # override_delay is None at loop start; coalesce to 0 for max() comparison
                    override_delay = max(
                        override_delay if override_delay is not None else 0, delay_hint
                    )
                except Exception:
                    if self._should_abort_reconnect(auto_reconnect, "unexpected error"):
                        return
                    logger.exception(
                        "Unexpected error during auto-reconnect attempt %d",
                        attempt_num,
                    )
                else:
                    return

                if self._should_abort_reconnect(auto_reconnect, "pre-sleep"):
                    return
                (
                    sleep_delay,
                    should_retry,
                ) = self.reconnect_policy.next_attempt()
                if override_delay is not None:
                    sleep_delay = max(sleep_delay, override_delay)
                if not should_retry:
                    logger.info("Auto-reconnect reached maximum retry limit.")
                    return
                logger.debug(
                    "Waiting %.2f seconds before next reconnect attempt.",
                    sleep_delay,
                )
                # Allow prompt shutdown without waiting the full backoff.
                if shutdown_event.wait(timeout=sleep_delay):
                    logger.debug("Reconnect wait interrupted by shutdown signal.")
                    return
        finally:
            if on_exit is not None:
                on_exit()
