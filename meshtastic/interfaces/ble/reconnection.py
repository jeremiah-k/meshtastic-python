"""BLE reconnection logic and scheduling."""

import logging
from threading import Event, RLock, Thread
from typing import TYPE_CHECKING, Optional

from bleak.exc import BleakDBusError, BleakDeviceNotFoundError, BleakError

from meshtastic.interfaces.ble.constants import DBUS_ERROR_RECONNECT_DELAY, BLEConfig
from meshtastic.interfaces.ble.coordination import ThreadCoordinator
from meshtastic.interfaces.ble.gating import (
    _addr_key,
    _is_currently_connected_elsewhere,
    addr_lock_context,
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
    ):
        """
        Initialize a ReconnectScheduler to manage background BLE reconnection attempts.

        Parameters:
            state_manager (BLEStateManager): Provides BLE state queries (e.g., is_closing, can_connect) used to decide whether to schedule reconnect attempts.
            state_lock (RLock): Re-entrant lock protecting access to shared BLE state and the internal reconnect thread reference.
            thread_coordinator (ThreadCoordinator): Factory/manager used to create threads for the reconnect worker.
            interface (BLEInterface): BLE interface used to perform connection attempts and to attach the scheduler.

        Initializes a ReconnectPolicy from BLEConfig, creates a ReconnectWorker with the policy and interface, and sets the internal reconnect thread reference to None.
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
        self._reconnect_thread: Optional[Thread] = None

    def schedule_reconnect(self, auto_reconnect: bool, shutdown_event: Event) -> bool:
        """
        Start a background BLE reconnect worker when auto-reconnect is enabled and no reconnect worker is currently active.

        Parameters:
                auto_reconnect (bool): Whether automatic reconnection is enabled; scheduling is skipped when False.
                shutdown_event (Event): Event used by the worker to detect shutdown and stop retrying.

        Returns:
                `true` if a new reconnect worker thread was created and started, `false` otherwise.
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
                name="BLEAutoReconnect",
                daemon=True,
            )
            # Set the thread reference before starting to prevent race conditions
            self._reconnect_thread = thread

        # Start the thread outside the lock to avoid holding the lock during thread start
        self.thread_coordinator.start_thread(thread)
        return True

    def clear_thread_reference(self) -> None:
        """
        Clear the scheduler's stored reconnect thread reference.

        Sets the internal `_reconnect_thread` to `None` while holding `self.state_lock` to mark that the reconnect worker has exited.
        """
        with self.state_lock:
            # Always clear the reference once the worker loop exits to match legacy behavior.
            self._reconnect_thread = None


class ReconnectWorker:
    """Perform blocking reconnect attempts with policy-driven backoff."""

    def __init__(self, interface: "BLEInterface", reconnect_policy: ReconnectPolicy):
        """
        Initialize a ReconnectWorker bound to a BLE interface and a reconnect policy.

        Parameters:
            interface ("BLEInterface"): BLE interface used to perform connection attempts and to query or modify connection state.
            reconnect_policy (ReconnectPolicy): Policy that controls backoff timing, retry limits, and attempt state for reconnect attempts.
        """
        self.interface = interface
        self.reconnect_policy = reconnect_policy

    def attempt_reconnect_loop(  # pylint: disable=R0911
        self, auto_reconnect: bool, shutdown_event: Event
    ) -> None:
        """
        Run the blocking BLE auto-reconnect loop using the configured backoff policy.

        The loop repeatedly attempts to reconnect the interface until a connection succeeds, the
        reconnect policy stops further retries, the provided shutdown_event is set, or auto_reconnect
        is False. Between failed attempts the loop respects the policy's backoff delay (adjusted for
        specific BLE/DBus errors) and allows early exit when shutdown_event is signaled. On exit the
        scheduler's thread reference is cleared.

        Parameters:
            auto_reconnect (bool): If False, exit immediately without attempting reconnects.
            shutdown_event (threading.Event): Event that, when set, causes the loop to stop as soon as possible.
        """
        self.reconnect_policy.reset()

        override_delay: Optional[float] = None
        addr_key = _addr_key(getattr(self.interface, "address", None))

        # Use context manager for automatic holder count management
        with addr_lock_context(addr_key) as gate:
            try:
                while not shutdown_event.is_set():
                    override_delay = None
                    if self.interface.is_connection_closing or not auto_reconnect:
                        logger.debug(
                            "Auto-reconnect aborted because interface is closing or disabled."
                        )
                        return
                    attempt_num = self.reconnect_policy.get_attempt_count() + 1
                    try:
                        with gate:
                            if self.interface.is_connection_connected:
                                return
                            # Check if already connected elsewhere before attempting
                            if _is_currently_connected_elsewhere(addr_key):
                                logger.debug(
                                    "Skipping reconnect attempt %d: address %s already connected elsewhere",
                                    attempt_num,
                                    addr_key or "unknown",
                                )
                                return
                            logger.info(
                                "Attempting BLE auto-reconnect (attempt %d).",
                                attempt_num,
                            )
                            self.interface.connect(self.interface.address)
                            logger.info(
                                "BLE auto-reconnect succeeded after %d attempts.",
                                attempt_num,
                            )
                            return
                    except self.interface.BLEError as err:
                        if self.interface.is_connection_closing or not auto_reconnect:
                            logger.debug(
                                "Auto-reconnect cancelled after failure due to shutdown/disable."
                            )
                            return
                        logger.warning(
                            "Auto-reconnect attempt %d failed: %s",
                            attempt_num,
                            err,
                        )
                    except BleakDBusError:
                        if self.interface.is_connection_closing or not auto_reconnect:
                            logger.debug(
                                "Auto-reconnect cancelled after DBus failure due to shutdown/disable."
                            )
                            return
                        logger.exception(
                            "DBus error during auto-reconnect attempt %d",
                            attempt_num,
                        )
                        # Use longer delay for DBus errors to allow system Bluetooth stack to recover
                        override_delay = max(
                            override_delay or 0, DBUS_ERROR_RECONNECT_DELAY
                        )
                        # State transition to ERROR and DISCONNECTED is already handled by
                        # the connection orchestrator, so we don't need to do it here
                    except BleakError as err:
                        if self.interface.is_connection_closing or not auto_reconnect:
                            logger.debug(
                                "Auto-reconnect cancelled after bleak failure due to shutdown/disable."
                            )
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
                            else BLEConfig.SEND_PROPAGATION_DELAY
                        )
                        override_delay = max(override_delay or 0, delay_hint)
                    except Exception:
                        if self.interface.is_connection_closing or not auto_reconnect:
                            logger.debug(
                                "Auto-reconnect cancelled after unexpected failure due to shutdown/disable."
                            )
                            return
                        logger.exception(
                            "Unexpected error during auto-reconnect attempt %d",
                            attempt_num,
                        )

                    if self.interface.is_connection_closing or not auto_reconnect:
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
                self.interface._reconnect_scheduler.clear_thread_reference()
