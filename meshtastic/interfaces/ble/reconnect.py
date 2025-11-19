"""BLE reconnect logic."""
import logging
import random
import time
from threading import Event, RLock, Thread, current_thread
from typing import Optional, Tuple, TYPE_CHECKING

from .config import BLEConfig

if TYPE_CHECKING:
    from meshtastic.ble_interface import BLEInterface

logger = logging.getLogger(__name__)


def _sleep(delay: float) -> None:
    """
    Sleep for the given number of seconds; provided as a wrapper to make sleeping testable.
    
    Parameters:
        delay (float): Number of seconds to sleep.
    """
    time.sleep(delay)


class ReconnectPolicy:
    """
    Unified reconnection / retry policy with jittered exponential backoff.
    """

    def __init__(
        self,
        *,
        initial_delay: float = 1.0,
        max_delay: float = 30.0,
        backoff: float = 2.0,
        jitter_ratio: float = 0.1,
        max_retries: Optional[int] = None,
        random_source=None,
    ):
        """
        Initialize a ReconnectPolicy configured for jittered exponential backoff.
        
        Parameters:
            initial_delay (float): Base delay for attempt 0 in seconds; must be > 0.
            max_delay (float): Upper bound for computed delays in seconds; must be >= initial_delay.
            backoff (float): Multiplicative backoff factor applied per attempt; must be > 1.0.
            jitter_ratio (float): Fraction [0.0, 1.0] controlling random jitter applied to the computed delay.
            max_retries (Optional[int]): Maximum number of retry attempts, or `None` for unlimited retries.
            random_source: Optional random-like module or object with `random()` used to generate jitter; defaults to the standard `random` module.
        
        Raises:
            ValueError: If any argument violates its validation constraints (see parameter descriptions).
        """
        if initial_delay <= 0:
            raise ValueError(f"initial_delay must be > 0, got {initial_delay}")
        if max_delay < initial_delay:
            raise ValueError(
                f"max_delay ({max_delay}) must be >= initial_delay ({initial_delay})"
            )
        if backoff <= 1.0:
            raise ValueError(f"backoff must be > 1.0, got {backoff}")
        if not 0.0 <= jitter_ratio <= 1.0:
            raise ValueError(
                f"jitter_ratio must be between 0.0 and 1.0, got {jitter_ratio}"
            )
        if max_retries is not None and max_retries < 0:
            raise ValueError(f"max_retries must be >= 0 or None, got {max_retries}")
        self.initial_delay = initial_delay
        self.max_delay = max_delay
        self.backoff = backoff
        self.jitter_ratio = jitter_ratio
        self.max_retries = max_retries
        self._random = random_source or random
        self._attempt_count = 0

    def reset(self) -> None:
        """Reset internal state to begin a fresh retry cycle."""
        self._attempt_count = 0

    def get_delay(self, attempt: Optional[int] = None) -> float:
        """
        Compute the delay for a reconnect attempt, applying exponential backoff and symmetric jitter.
        
        Parameters:
            attempt (Optional[int]): Zero-based attempt index to calculate the delay for. If omitted, the current internal attempt count is used.
        
        Returns:
            float: Delay in seconds after applying exponential backoff and jitter.
        """
        if attempt is None:
            attempt = self._attempt_count
        delay = min(self.initial_delay * (self.backoff**attempt), self.max_delay)
        jitter = delay * self.jitter_ratio * (self._random.random() * 2.0 - 1.0)
        return delay + jitter

    def should_retry(self, attempt: Optional[int] = None) -> bool:
        """
        Decide whether another retry is allowed by the policy.
        
        Parameters:
            attempt (Optional[int]): Attempt index to evaluate. If None, the policy's current attempt count is used.
        
        Returns:
            True if retries are unlimited or the specified attempt is less than `max_retries`, False otherwise.
        """
        if attempt is None:
            attempt = self._attempt_count
        return self.max_retries is None or attempt < self.max_retries

    def next_attempt(self) -> Tuple[float, bool]:
        """
        Return the delay for the current attempt and whether another retry is permitted, then increment the internal attempt counter.
        
        Returns:
            (delay, should_retry): 
                delay (float): Computed backoff delay in seconds for the current attempt.
                should_retry (bool): `True` if another retry is allowed, `False` otherwise.
        """
        delay = self.get_delay()
        should_retry = self.should_retry()
        self._attempt_count += 1
        return delay, should_retry

    def get_attempt_count(self) -> int:
        """
        Get the number of reconnect attempts that have been performed.
        
        Returns:
            attempt_count (int): The current reconnect attempt count.
        """
        return self._attempt_count

    def sleep_with_backoff(self, attempt: int) -> None:
        """
        Pause execution for the jittered backoff delay corresponding to the given attempt.
        
        Parameters:
            attempt (int): Zero-based attempt index used to compute the backoff delay.
        """
        _sleep(self.get_delay(attempt))

    def clone(self, *, random_source=None) -> "ReconnectPolicy":
        """
        Create a copy of the policy preserving its configuration.
        
        Parameters:
            random_source (optional): Random-like object used to generate jitter. If omitted, the cloned policy uses the same random source as this instance.
        
        Returns:
            ReconnectPolicy: A new ReconnectPolicy configured identically to this instance, using `random_source` if provided.
        """
        return ReconnectPolicy(
            initial_delay=self.initial_delay,
            max_delay=self.max_delay,
            backoff=self.backoff,
            jitter_ratio=self.jitter_ratio,
            max_retries=self.max_retries,
            random_source=random_source or self._random,
        )


class RetryPolicy:
    """
    Static retry policy presets for BLE operations.
    """

    EMPTY_READ = ReconnectPolicy(
        initial_delay=BLEConfig.EMPTY_READ_RETRY_DELAY,
        max_delay=1.0,
        backoff=1.5,
        jitter_ratio=0.1,
        max_retries=BLEConfig.EMPTY_READ_MAX_RETRIES,
    )

    TRANSIENT_ERROR = ReconnectPolicy(
        initial_delay=BLEConfig.TRANSIENT_READ_RETRY_DELAY,
        max_delay=2.0,
        backoff=1.5,
        jitter_ratio=0.1,
        max_retries=BLEConfig.TRANSIENT_READ_MAX_RETRIES,
    )

    AUTO_RECONNECT = ReconnectPolicy(
        initial_delay=BLEConfig.AUTO_RECONNECT_INITIAL_DELAY,
        max_delay=BLEConfig.AUTO_RECONNECT_MAX_DELAY,
        backoff=BLEConfig.AUTO_RECONNECT_BACKOFF,
        jitter_ratio=BLEConfig.AUTO_RECONNECT_JITTER_RATIO,
        max_retries=None,
    )


class ReconnectScheduler:
    """Manage lifecycle of the reconnect worker thread."""

    def __init__(
        self,
        state_manager,
        state_lock: RLock,
        thread_coordinator,
        interface: "BLEInterface",
    ):
        """
        Initialize the reconnect scheduler and prepare its worker and backoff policy.
        
        Parameters:
            state_manager: Object that tracks and provides the interface's runtime state.
            state_lock (RLock): Reentrant lock used to synchronize access to shared state.
            thread_coordinator: Component responsible for creating/starting background threads.
            interface (BLEInterface): BLE interface instance this scheduler will manage.
        
        Side effects:
            - Stores the provided references.
            - Clones the default `AUTO_RECONNECT` policy into `_reconnect_policy`.
            - Creates a `ReconnectWorker` assigned to `_reconnect_worker`.
            - Initializes `_reconnect_thread` to `None`.
        """
        self.state_manager = state_manager
        self.state_lock = state_lock
        self.thread_coordinator = thread_coordinator
        self.interface = interface
        self._reconnect_policy = RetryPolicy.AUTO_RECONNECT.clone()
        self._reconnect_worker = ReconnectWorker(interface, self._reconnect_policy)
        self._reconnect_thread: Optional[Thread] = None

    def schedule_reconnect(self, auto_reconnect: bool, shutdown_event: Event) -> bool:
        """
        Schedule a background BLE auto-reconnect worker if conditions allow.
        
        Parameters:
        	auto_reconnect (bool): Whether auto-reconnect is enabled; scheduling is skipped when False.
        	shutdown_event (Event): Event that the worker will observe to stop retrying.
        
        Returns:
        	scheduled (bool): `True` if a new reconnect thread was created and started, `False` if scheduling was skipped because auto-reconnect is disabled, the interface is closing, or a reconnect thread is already running.
        """
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
        """
        Clear the stored reconnect thread reference if called from that same thread.
        
        This acquires the scheduler's state lock and sets the internal _reconnect_thread to None only when the currently executing thread is the one recorded, preventing accidental clearing from other threads.
        """
        with self.state_lock:
            if self._reconnect_thread is current_thread():
                self._reconnect_thread = None


class ReconnectWorker:
    """Perform blocking reconnect attempts with policy-driven backoff."""

    def __init__(self, interface: "BLEInterface", reconnect_policy: ReconnectPolicy):
        """
        Create a ReconnectWorker that performs blocking reconnect attempts for a BLE interface.
        
        Parameters:
            interface (BLEInterface): The BLE interface instance the worker will operate on (used to connect and resubscribe notifications).
            reconnect_policy (ReconnectPolicy): Policy controlling backoff, jitter, and retry limits for reconnect attempts.
        """
        self.interface = interface
        self.reconnect_policy = reconnect_policy

    def attempt_reconnect_loop(
        self, auto_reconnect: bool, shutdown_event: Event
    ) -> None:
        """
        Run the blocking auto-reconnect loop that attempts BLE reconnection until success, shutdown, or retry limit.
        
        Resets the associated reconnect policy then repeatedly tries to reconnect and resubscribe notifications. The loop exits and returns when:
        - a reconnect succeeds,
        - the provided shutdown_event is set,
        - auto_reconnect is False,
        - the interface is closing, or
        - the reconnect policy indicates no further retries.
        
        On BLE-related errors the attempt is logged and the loop uses the reconnect policy to determine a jittered backoff before retrying. Unexpected exceptions are logged and treated similarly. Always clears the scheduler's thread reference in a finally block.
        
        Parameters:
            auto_reconnect (bool): Current setting that enables or disables automatic reconnect attempts; the loop stops if this becomes False.
            shutdown_event (Event): Event that, when set, causes the loop to stop promptly.
        """
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
                        attempt_num,
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
                        attempt_num,
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