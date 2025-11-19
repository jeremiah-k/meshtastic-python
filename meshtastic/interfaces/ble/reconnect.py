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
        self._random = random_source or random
        self._attempt_count = 0

    def reset(self) -> None:
        """Reset internal state to begin a fresh retry cycle."""
        self._attempt_count = 0

    def get_delay(self, attempt: Optional[int] = None) -> float:
        """
        Compute the jittered delay for the provided attempt (defaults to current attempt count).
        """
        if attempt is None:
            attempt = self._attempt_count
        delay = min(self.initial_delay * (self.backoff**attempt), self.max_delay)
        jitter = delay * self.jitter_ratio * (self._random.random() * 2.0 - 1.0)
        return delay + jitter

    def should_retry(self, attempt: Optional[int] = None) -> bool:
        """
        Determine whether another retry should be attempted.
        """
        if attempt is None:
            attempt = self._attempt_count
        return self.max_retries is None or attempt < self.max_retries

    def next_attempt(self) -> Tuple[float, bool]:
        """
        Advance the attempt counter and return (delay, should_retry).
        """
        delay = self.get_delay()
        should_retry = self.should_retry()
        self._attempt_count += 1
        return delay, should_retry

    def get_attempt_count(self) -> int:
        """Expose the current attempt count (primarily for logging)."""
        return self._attempt_count

    def sleep_with_backoff(self, attempt: int) -> None:
        """Sleep for the jittered delay associated with the supplied attempt."""
        _sleep(self.get_delay(attempt))


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
        state_manager: "BLEStateManager",
        state_lock: RLock,
        thread_coordinator: "ThreadCoordinator",
        interface: "BLEInterface",
    ):
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
            if self._reconnect_thread is current_thread():
                self._reconnect_thread = None


class ReconnectWorker:
    """Perform blocking reconnect attempts with policy-driven backoff."""

    def __init__(self, interface: "BLEInterface", reconnect_policy: ReconnectPolicy):
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
                except BLEError as err:
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
            if hasattr(self.interface, "_reconnect_scheduler"):
                self.interface._reconnect_scheduler.clear_thread_reference()