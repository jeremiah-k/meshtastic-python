"""BLE Reconnect Policy"""

import random
import logging
from threading import Event, RLock, Thread, current_thread
from typing import Optional, Tuple, TYPE_CHECKING

from .config import BLEConfig
from .util import _sleep
from .exceptions import BLEError


if TYPE_CHECKING:
    from ..core import BLEInterface
    from ..state import BLEStateManager
    from ..util import ThreadCoordinator
    from ..gatt import NotificationManager
    from ..client import BLEClient


logger = logging.getLogger(__name__)


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
        Initialize a ReconnectPolicy that produces jittered exponential backoff delays and controls retry counts.
        
        Parameters:
            initial_delay (float): Base delay in seconds for the first retry; must be greater than 0.
            max_delay (float): Maximum delay in seconds to cap exponential growth; must be >= initial_delay.
            backoff (float): Multiplicative backoff factor applied each attempt; must be greater than 1.0.
            jitter_ratio (float): Fractional jitter to apply to the computed delay, between 0.0 and 1.0 inclusive.
            max_retries (Optional[int]): Maximum number of retry attempts, or None for unlimited retries; if provided must be >= 0.
            random_source: Optional random number source providing a .random() method; defaults to the module-level random.
        
        Raises:
            ValueError: If any parameter is out of its allowed range.
        
        Initial state:
            Sets internal attempt count to 0 and stores provided configuration for delay calculations.
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
        Compute the jittered delay for a reconnect attempt.
        
        If `attempt` is None, the policy's current attempt count is used.
        
        Parameters:
            attempt (Optional[int]): Zero-based attempt index; when omitted, uses the current attempt count.
        
        Returns:
            float: Delay in seconds for the given attempt, including jitter.
        """
        if attempt is None:
            attempt = self._attempt_count
        delay = min(self.initial_delay * (self.backoff**attempt), self.max_delay)
        jitter = delay * self.jitter_ratio * (self._random.random() * 2.0 - 1.0)
        return delay + jitter

    def should_retry(self, attempt: Optional[int] = None) -> bool:
        """
        Determine if another reconnect attempt is allowed under the current retry policy.
        
        Parameters:
            attempt (Optional[int]): Attempt index to evaluate; if omitted, the policy's current attempt count is used.
        
        Returns:
            `True` if retries are unlimited or the provided attempt is less than `max_retries`, `False` otherwise.
        """
        if attempt is None:
            attempt = self._attempt_count
        return self.max_retries is None or attempt < self.max_retries

    def next_attempt(self) -> Tuple[float, bool]:
        """
        Compute the delay for the current attempt and indicate whether further retries are permitted.
        
        This evaluates the jittered backoff delay and retry allowance for the current attempt, then increments the internal attempt counter. The increment occurs after determining `should_retry`, which makes it possible to perform up to `max_retries + 1` total attempts when callers rely solely on this method.
        
        Returns:
            delay (float): Jittered backoff delay for the current attempt.
            should_retry (bool): `true` if additional retry attempts are allowed, `false` otherwise.
        """
        delay = self.get_delay()
        should_retry = self.should_retry()
        self._attempt_count += 1
        return delay, should_retry

    def get_attempt_count(self) -> int:
        """
        Return the current reconnect attempt count.
        
        Returns:
            attempt_count (int): Number of reconnect attempts recorded by this policy.
        """
        return self._attempt_count

    def sleep_with_backoff(self, attempt: int) -> None:
        """
        Sleep for the jittered exponential-backoff delay corresponding to the given attempt number.
        
        Parameters:
            attempt (int): Attempt index where 0 represents the first attempt; the delay is computed using the policy's backoff, max_delay, and jitter_ratio.
        """
        _sleep(self.get_delay(attempt))


class _PolicyFactory:
    """Descriptor that creates a fresh ReconnectPolicy on each access."""

    def __init__(self, **kwargs):
        """
        Store keyword arguments for the factory so each access can create a new ReconnectPolicy with the same configuration.
        
        Parameters:
            **kwargs (dict): Keyword arguments that will be forwarded to each ReconnectPolicy created by this factory.
        """
        self._kwargs = kwargs

    def __get__(self, instance, owner):
        """
        Return a fresh ReconnectPolicy instance configured with the factory's stored keyword arguments.
        
        @returns A new ReconnectPolicy instantiated with the factory's stored `kwargs`.
        """
        return ReconnectPolicy(**self._kwargs)


class RetryPolicy:
    """
    Static retry policy presets for BLE operations.
    """

    EMPTY_READ = _PolicyFactory(
        initial_delay=BLEConfig.EMPTY_READ_RETRY_DELAY,
        max_delay=1.0,
        backoff=1.5,
        jitter_ratio=0.1,
        max_retries=BLEConfig.EMPTY_READ_MAX_RETRIES,
    )

    TRANSIENT_ERROR = _PolicyFactory(
        initial_delay=BLEConfig.TRANSIENT_READ_RETRY_DELAY,
        max_delay=2.0,
        backoff=1.5,
        jitter_ratio=0.1,
        max_retries=BLEConfig.TRANSIENT_READ_MAX_RETRIES,
    )

    AUTO_RECONNECT = _PolicyFactory(
        initial_delay=BLEConfig.AUTO_RECONNECT_INITIAL_DELAY,
        max_delay=BLEConfig.AUTO_RECONNECT_MAX_DELAY,
        backoff=BLEConfig.AUTO_RECONNECT_BACKOFF,
        jitter_ratio=BLEConfig.AUTO_RECONNECT_JITTER_RATIO,
        max_retries=None,
    )


class ReconnectScheduler:
    """Manage lifecycle of the reconnect worker thread."""

    def __init__(  # type: ignore
        self,
        state_manager: "BLEStateManager",
        state_lock: RLock,
        thread_coordinator: "ThreadCoordinator",
        interface: "BLEInterface",
    ):
        """
        Initialize the ReconnectScheduler and prepare its reconnect policy, worker, and thread reference.
        
        Parameters:
            state_manager (BLEStateManager): Manager for BLE connection state.
            state_lock (RLock): Lock protecting access to shared BLE state.
            thread_coordinator (ThreadCoordinator): Utility used to create and start threads.
            interface (BLEInterface): BLE interface used by the reconnect worker for connection attempts.
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
        Schedule and start a daemon worker thread to perform auto-reconnect attempts when appropriate.
        
        If auto_reconnect is enabled and the interface is not closing and no reconnect thread is already running, this creates, stores, and starts a new daemon reconnect thread that runs the reconnect worker loop. The thread reference is kept on the scheduler so callers can detect an active reconnect attempt.
        
        Parameters:
            auto_reconnect (bool): Whether automatic reconnects are enabled.
            shutdown_event (Event): Event that signals the worker to stop.
        
        Returns:
            bool: `True` if a new reconnect thread was created and started, `False` otherwise.
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
        Clear the stored reconnect thread reference if it matches the currently executing thread.
        
        Acquires the scheduler's state_lock and sets `_reconnect_thread` to `None` when it equals the current thread; leaves it unchanged otherwise.
        """
        with self.state_lock:
            if self._reconnect_thread is current_thread():
                self._reconnect_thread = None


class ReconnectWorker:
    """Perform blocking reconnect attempts with policy-driven backoff."""

    def __init__(self, interface: "BLEInterface", reconnect_policy: ReconnectPolicy):
        """
        Create a ReconnectWorker bound to a BLE interface and a reconnect policy.
        
        Parameters:
            interface (BLEInterface): The BLE interface used to perform connect attempts, manage notifications, and inspect connection state.
            reconnect_policy (ReconnectPolicy): Policy that governs backoff, jitter, and retry limits for reconnect attempts.
        """
        self.interface = interface
        self.reconnect_policy = reconnect_policy

    def attempt_reconnect_loop(
        self, auto_reconnect: bool, shutdown_event: Event
    ) -> None:
        """
        Run a blocking loop that attempts to reconnect the BLE interface using the configured reconnect policy.
        
        This method resets the reconnect policy and repeatedly attempts to re-establish the interface connection until one of the following occurs: the reconnect succeeds, the provided shutdown_event is set, the interface is closing, auto_reconnect is disabled, or the reconnect policy indicates no more retries. Each failed attempt will trigger the policy to produce a jittered exponential backoff delay before the next attempt. On a successful reconnect, notifications are cleaned up and resubscribed as appropriate. The reconnect scheduler's thread reference is cleared when the loop finishes.
        
        Parameters:
        	auto_reconnect (bool): If False, the loop will exit immediately without attempting reconnection.
        	shutdown_event (threading.Event): Event that, when set, causes the loop to stop and return.
        """
        self.reconnect_policy.reset()
        try:
            while not shutdown_event.is_set():
                if self.interface._state_manager.is_closing or not auto_reconnect:
                    logger.debug(
                        "Auto-reconnect aborted because interface is closing or disabled."
                    )
                    return
                attempt_num = self.reconnect_policy.get_attempt_count() + 1
                try:
                    logger.info(
                        "Attempting BLE auto-reconnect (attempt %d).", attempt_num
                    )
                    self.interface._notification_manager.cleanup_all()
                    self.interface.connect(self.interface.address)
                    if getattr(self.interface, "client", None) is not None:
                        timeout = (
                            BLEConfig.NOTIFICATION_START_TIMEOUT
                            if BLEConfig.NOTIFICATION_START_TIMEOUT is not None
                            else BLEConfig.GATT_IO_TIMEOUT
                        )
                        self.interface._notification_manager.resubscribe_all(
                            self.interface.client,
                            timeout=timeout,
                        )

                    logger.info(
                        "BLE auto-reconnect succeeded after %d attempts.", attempt_num
                    )
                    return
                except BLEError as err:
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
            if hasattr(self.interface, "_reconnect_scheduler"):
                self.interface._reconnect_scheduler.clear_thread_reference()
