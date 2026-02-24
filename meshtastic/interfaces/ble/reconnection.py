"""BLE reconnection logic and scheduling."""

import logging
from threading import Event, RLock
from typing import TYPE_CHECKING, Any, Callable, NamedTuple, cast

from bleak.exc import BleakDBusError, BleakDeviceNotFoundError, BleakError

from meshtastic.interfaces.ble.constants import DBUS_ERROR_RECONNECT_DELAY, BLEConfig
from meshtastic.interfaces.ble.coordination import ThreadCoordinator, ThreadLike
from meshtastic.interfaces.ble.gating import (
    _addr_key,
    _is_currently_connected_elsewhere,
)
from meshtastic.interfaces.ble.policies import ReconnectPolicy
from meshtastic.interfaces.ble.state import BLEStateManager

if TYPE_CHECKING:
    from meshtastic.interfaces.ble.interface import BLEInterface

logger = logging.getLogger("meshtastic.ble")


class ReconnectPolicyMissingMethodError(AttributeError):
    """Raised when a required reconnect-policy method is missing."""

    def __init__(self, method_name: str) -> None:
        """Initialize with the missing method name."""
        self.method_name = method_name
        super().__init__(f"ReconnectPolicy missing method '{method_name}'")


class NextAttempt(NamedTuple):
    """Validated reconnect scheduling decision from policy.next_attempt()."""

    delay: float
    should_retry: bool


class ReconnectScheduler:
    """Manage lifecycle of the reconnect worker thread."""

    def __init__(
        self,
        state_manager: BLEStateManager,
        state_lock: RLock,
        thread_coordinator: ThreadCoordinator,
        interface: "BLEInterface",
    ) -> None:
        """Manage scheduling of a background reconnect worker for a BLE interface.

        Parameters
        ----------
        state_manager : BLEStateManager
            Observes BLE lifecycle state used to decide whether reconnects may
            be scheduled (e.g., whether the interface is closing or a
            connection is active).
        state_lock : RLock
            Re-entrant lock protecting shared BLE state and the scheduler's internal thread reference.
        thread_coordinator : ThreadCoordinator
            Factory/manager used to create and start the worker thread that performs reconnect attempts.
        interface : 'BLEInterface'
            The BLE interface whose connection the scheduler will attempt to restore.
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
        self._reconnect_thread: ThreadLike | None = None

    def _schedule_reconnect(self, auto_reconnect: bool, shutdown_event: Event) -> bool:
        """Schedule a background BLE reconnect worker when auto-reconnect is enabled and no worker is already running.

        Parameters
        ----------
        auto_reconnect : bool
            Whether automatic reconnection is enabled; scheduling is skipped when False.
        shutdown_event : Event
            Event the worker observes to stop retrying early during shutdown.

        Returns
        -------
        bool
            True if a new reconnect worker thread was created and started, False otherwise.


        """
        if not auto_reconnect:
            return False

        with self.state_lock:
            is_closing = self.interface._is_connection_closing
            can_initiate_connection = self.interface._can_initiate_connection
            if is_closing or not can_initiate_connection:
                logger.debug(
                    "Skipping auto-reconnect scheduling because interface is closing or connection already in progress."
                )
                return False
            if self._reconnect_thread is not None:
                logger.debug(
                    "Auto-reconnect already in progress; skipping new attempt."
                )
                return False

            thread = self.thread_coordinator._create_thread(
                target=self._reconnect_worker._attempt_reconnect_loop,
                args=(auto_reconnect, shutdown_event),
                kwargs={"on_exit": self._clear_thread_reference},
                name="BLEAutoReconnect",
                daemon=True,
            )
            # Set the thread reference before starting to prevent race conditions
            self._reconnect_thread = thread
        # Start outside the lock: the reference is already set, so concurrent
        # schedulers will see a non-None _reconnect_thread and exit early.
        try:
            self.thread_coordinator._start_thread(thread)
        except Exception:
            self._clear_thread_reference()
            raise
        return True

    def _clear_thread_reference(self) -> None:
        """Clear the internal reference to the running reconnect thread.

        This operation acquires the scheduler's state_lock and sets the internal
        reconnect thread reference to None to record that no background
        reconnect worker is active.

        Returns
        -------
        None
        """
        with self.state_lock:
            # Always clear the reference once the worker loop exits to match legacy behavior.
            self._reconnect_thread = None


class ReconnectWorker:
    """Perform blocking reconnect attempts with policy-driven backoff."""

    def __init__(
        self, interface: "BLEInterface", reconnect_policy: ReconnectPolicy
    ) -> None:
        """Initialize the ReconnectWorker bound to a BLE interface and a reconnect policy.

        Parameters
        ----------
        interface : 'BLEInterface'
            Interface used to initiate connection attempts and to query or modify connection state.
        reconnect_policy : ReconnectPolicy
            Backoff and retry policy that controls reconnect timing and attempt state.
        """
        self.interface = interface
        self.reconnect_policy = reconnect_policy

    def _call_policy(self, method_name: str, *args: Any) -> Any:
        """Call a policy method with fallback to underscored version for backward compatibility.

        Parameters
        ----------
        method_name : str
            Name of the public method to call on the policy.
        *args
            Arguments to pass to the policy method.

        Returns
        -------
        Any
            The return value from the policy method.
        """
        method = getattr(self.reconnect_policy, method_name, None)
        if callable(method):
            return method(*args)
        # Backward compatibility for test doubles that only expose underscored methods.
        fallback = getattr(self.reconnect_policy, f"_{method_name}", None)
        if callable(fallback):
            return fallback(*args)
        raise ReconnectPolicyMissingMethodError(method_name)

    def _should_abort_reconnect(self, auto_reconnect: bool, context: str = "") -> bool:
        """Return whether the reconnect process should be aborted based on the interface state and the auto-reconnect setting.

        Parameters
        ----------
        auto_reconnect : bool
            Whether automatic reconnect is enabled.
        context : str
            Optional context string included in debug messages. (Default value = '')

        Returns
        -------
        bool
            `True` if reconnection should be aborted, `False` otherwise.
        """
        if self.interface._is_connection_closing:
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

    def _validate_next_attempt(self, value: Any) -> NextAttempt | None:
        """Validate reconnect-policy next_attempt() output.

        Parameters
        ----------
        value : Any
            Raw value returned by reconnect_policy.next_attempt().

        Returns
        -------
        NextAttempt | None
            (delay, should_retry) on valid input, otherwise None.
        """
        if not isinstance(value, tuple) or len(value) != 2:
            logger.error(
                "Reconnect policy next_attempt returned invalid value: %r",
                value,
            )
            return None
        sleep_delay, should_retry = value
        if (
            not isinstance(sleep_delay, (int, float))
            or isinstance(sleep_delay, bool)
            or not isinstance(should_retry, bool)
        ):
            logger.error(
                "Reconnect policy next_attempt returned invalid value: %r",
                value,
            )
            return None
        sleep_delay = float(sleep_delay)
        if sleep_delay < 0.0:
            logger.error(
                "Reconnect policy next_attempt returned negative delay: %r",
                value,
            )
            return None
        return NextAttempt(delay=sleep_delay, should_retry=should_retry)

    def _attempt_reconnect_loop(  # pylint: disable=R0911
        self,
        auto_reconnect: bool,
        shutdown_event: Event,
        *,
        on_exit: Callable[[], None] | None = None,
    ) -> None:
        """Run the reconnect loop that attempts to restore the bound BLE interface using the configured backoff policy.

        Attempts reconnects until a connection succeeds, the reconnect policy
        stops further retries, the provided shutdown_event is set, or
        auto_reconnect is False. Between failed attempts the loop waits
        according to the policy; certain BLE/DBus errors may increase the
        delay. The optional on_exit callback is invoked unconditionally when the
        loop ends.

        Parameters
        ----------
        auto_reconnect : bool
            If False, the loop will exit without attempting reconnects.
        shutdown_event : Event
            Event that stops the loop when set.
        on_exit : Callable[[], None] | None
            Optional callback called once when the loop finishes (successful connect, abort, or exception). (Default value = None)
        """
        try:
            self._call_policy("reset")
            interface = self.interface
            override_delay: float | None = None
            while not shutdown_event.is_set():
                override_delay = None
                if self._should_abort_reconnect(auto_reconnect, "loop start"):
                    return
                attempt_num = 0
                try:
                    attempt_num = cast(int, self._call_policy("get_attempt_count")) + 1
                    if interface._is_connection_connected:
                        return
                    device_addr = _addr_key(getattr(interface, "address", None))
                    # Check if already connected elsewhere before attempting.
                    # connect() enforces this gate as well; this early check avoids
                    # scheduling a full connect path when we already know it will fail.
                    if device_addr and _is_currently_connected_elsewhere(
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
                except ReconnectPolicyMissingMethodError as err:
                    logger.exception(
                        "Reconnect policy missing required method '%s'",
                        err.method_name,
                    )
                    return
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
                    override_delay = DBUS_ERROR_RECONNECT_DELAY
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
                    override_delay = delay_hint
                except Exception:
                    if self._should_abort_reconnect(auto_reconnect, "unexpected error"):
                        return
                    logger.exception(
                        "Unexpected error during auto-reconnect attempt %d",
                        attempt_num,
                    )
                else:
                    logger.info(
                        "BLE auto-reconnect succeeded after %d attempts.",
                        attempt_num,
                    )
                    return

                if self._should_abort_reconnect(auto_reconnect, "pre-sleep"):
                    return
                try:
                    next_attempt = self._call_policy("next_attempt")
                except ReconnectPolicyMissingMethodError as err:
                    logger.exception(
                        "Reconnect policy missing required method '%s'",
                        err.method_name,
                    )
                    return
                validated_next_attempt = self._validate_next_attempt(next_attempt)
                if validated_next_attempt is None:
                    return
                sleep_delay = validated_next_attempt.delay
                should_retry = validated_next_attempt.should_retry
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
        except ReconnectPolicyMissingMethodError as err:
            policy_name = type(self.reconnect_policy).__name__
            logger.exception(
                "Reconnect policy missing required method '%s'; aborting reconnect loop (policy_name=%s, policy=%r)",
                err.method_name,
                policy_name,
                self.reconnect_policy,
            )
        except Exception:
            logger.exception(
                "Unexpected error during reconnect loop setup; aborting reconnect"
            )
        finally:
            if on_exit is not None:
                try:
                    on_exit()
                except Exception:
                    logger.exception("Reconnect loop exit callback failed")
