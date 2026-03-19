"""Receive lifecycle coordinator runtime ownership for BLE."""

import threading
import time
from collections.abc import Callable
from typing import TYPE_CHECKING

from meshtastic.interfaces.ble.constants import logger
from meshtastic.interfaces.ble.coordination import ThreadLike
from meshtastic.interfaces.ble.lifecycle_primitives import _LifecycleThreadAccess
from meshtastic.interfaces.ble.utils import _thread_start_probe

if TYPE_CHECKING:
    from meshtastic.interfaces.ble.interface import BLEInterface

RECEIVE_START_PENDING_TIMEOUT_SECONDS = 1.0


class BLEReceiveLifecycleCoordinator:
    """Own receive-loop intent and receive-thread lifecycle behavior."""

    def __init__(self, iface: "BLEInterface") -> None:
        """Bind receive lifecycle ownership to a specific interface."""
        self._iface = iface
        self._thread_access = _LifecycleThreadAccess(iface)

    def set_receive_wanted(self, *, want_receive: bool) -> None:
        """Request or clear receive-loop intent."""
        with self._iface._state_lock:
            self._iface._want_receive = want_receive

    def should_run_receive_loop(self) -> bool:
        """Return whether receive loop should continue running."""
        with self._iface._state_lock:
            return self._iface._want_receive and not self._iface._closed

    @staticmethod
    def _is_thread_start_failure_confirmed(thread: ThreadLike) -> bool:
        """Return whether startup probes confirm that ``thread`` failed to start."""
        started_event = getattr(thread, "_started", None)
        is_started = getattr(started_event, "is_set", None)
        if callable(is_started):
            try:
                return not bool(is_started())
            except Exception:  # noqa: BLE001 - probe remains best effort
                return False
        return isinstance(thread, threading.Thread)

    def _check_receive_start_conditions(
        self,
        *,
        name: str,
        reset_recovery: bool,
        create_runtime_thread: Callable[..., ThreadLike],
    ) -> tuple[ThreadLike | None, int | None]:
        """Validate start preconditions and create a staged receive thread."""
        iface = self._iface
        with iface._state_lock:
            if iface._closed or not iface._want_receive:
                logger.debug(
                    "Skipping receive thread start (%s): interface is closing/stopped.",
                    name,
                )
                return None, None
            existing = iface._receiveThread
            existing_start_pending = bool(getattr(iface, "_receive_start_pending", False))
            existing_is_alive = (
                _thread_start_probe(existing)[1] if existing is not None else False
            )
            if existing is threading.current_thread():
                if not reset_recovery:
                    iface._receive_start_pending = False
                    iface._receive_start_pending_since = None
                    logger.debug(
                        "Starting replacement receive thread (%s) from recovery while current receive thread unwinds.",
                        name,
                    )
                else:
                    now = time.monotonic()
                    pending_since = getattr(iface, "_receive_start_pending_since", None)
                    if (
                        not existing_start_pending
                        or not isinstance(pending_since, (float, int))
                    ):
                        iface._receive_start_pending_since = now
                        pending_age = 0.0
                    else:
                        pending_age = now - float(pending_since)
                        if pending_age >= RECEIVE_START_PENDING_TIMEOUT_SECONDS:
                            iface._receive_start_pending_since = now
                            pending_age = 0.0
                    iface._receive_start_pending = True
                    logger.debug(
                        "Deferring receive thread start (%s): current receive thread is still unwinding (pending %.3fs).",
                        name,
                        pending_age,
                    )
                    return None, None
            if existing is not None and existing is not threading.current_thread():
                if existing_is_alive:
                    iface._receive_start_pending = False
                    iface._receive_start_pending_since = None
                    logger.debug(
                        "Skipping receive thread start (%s): %s is already running.",
                        name,
                        existing.name,
                    )
                    return None, None
                if existing_start_pending:
                    pending_since = getattr(iface, "_receive_start_pending_since", None)
                    now = time.monotonic()
                    if not isinstance(pending_since, (float, int)):
                        iface._receive_start_pending_since = now
                        pending_age = 0.0
                    else:
                        pending_age = now - float(pending_since)
                    if pending_age < RECEIVE_START_PENDING_TIMEOUT_SECONDS:
                        logger.debug(
                            "Skipping receive thread start (%s): %s start still pending.",
                            name,
                            existing.name,
                        )
                        return None, None
                    logger.debug(
                        "Receive thread start pending timed out for %s after %.3fs; replacing stale pending reference.",
                        existing.name,
                        pending_age,
                    )
                    iface._receive_start_pending = False
                    iface._receive_start_pending_since = None
                else:
                    if not self._is_thread_start_failure_confirmed(existing):
                        pending_since = getattr(
                            iface, "_receive_start_pending_since", None
                        )
                        if not isinstance(pending_since, (float, int)):
                            iface._receive_start_pending_since = time.monotonic()
                        iface._receive_start_pending = True
                        logger.debug(
                            "Skipping receive thread start (%s): %s liveness probe inconclusive.",
                            name,
                            existing.name,
                        )
                        return None, None
                    iface._receive_start_pending = False
                    iface._receive_start_pending_since = None
            thread = create_runtime_thread(
                target=iface._receive_from_radio_impl,
                name=name,
                daemon=True,
            )
            iface._receiveThread = thread
            iface._receive_start_pending = True
            iface._receive_start_pending_since = time.monotonic()
            recovery_attempts_before_start = (
                iface._receive_recovery_attempts if reset_recovery else None
            )
            return thread, recovery_attempts_before_start

    def _create_and_start_receive_thread(
        self,
        thread: ThreadLike,
        *,
        start_runtime_thread: Callable[[ThreadLike], None],
    ) -> None:
        """Start staged receive thread and clear stale reference on failure."""
        iface = self._iface
        try:
            start_runtime_thread(thread)
        except (SystemExit, KeyboardInterrupt):  # pylint: disable=W0706
            with iface._state_lock:
                if iface._receiveThread is thread:
                    iface._receiveThread = None
                    iface._receive_start_pending = False
                    iface._receive_start_pending_since = None
            raise
        except Exception:  # noqa: BLE001 - start failure must clear stale thread reference
            with iface._state_lock:
                if iface._receiveThread is thread:
                    iface._receiveThread = None
                    iface._receive_start_pending = False
                    iface._receive_start_pending_since = None
            raise

    def _probe_receive_thread_start(self, thread: ThreadLike, *, name: str) -> bool:
        """Probe receive-thread startup and clear stale references on failure."""
        iface = self._iface
        _, thread_is_alive = _thread_start_probe(thread)
        if thread_is_alive:
            with iface._state_lock:
                if iface._receiveThread is thread:
                    iface._receive_start_pending = False
                    iface._receive_start_pending_since = None
            return True
        start_failure_confirmed = self._is_thread_start_failure_confirmed(thread)

        if start_failure_confirmed:
            with iface._state_lock:
                if iface._receiveThread is thread:
                    iface._receiveThread = None
                    iface._receive_start_pending = False
                    iface._receive_start_pending_since = None
            logger.debug(
                "Receive thread %s did not start; cleared stale thread reference.",
                name,
            )
        else:
            with iface._state_lock:
                if iface._receiveThread is thread:
                    iface._receive_start_pending = True
                    pending_since = getattr(iface, "_receive_start_pending_since", None)
                    if not isinstance(pending_since, (float, int)):
                        iface._receive_start_pending_since = time.monotonic()
            logger.debug(
                "Receive thread %s start probe inconclusive; keeping thread reference.",
                name,
            )
        return False

    def _maybe_reset_receive_recovery(
        self,
        *,
        thread: ThreadLike,
        recovery_attempts_before_start: int | None,
    ) -> None:
        """Reset recovery attempts after successful start when still applicable."""
        iface = self._iface
        if recovery_attempts_before_start is None:
            return
        with iface._state_lock:
            if (
                iface._receiveThread is thread
                and iface._receive_recovery_attempts == recovery_attempts_before_start
                and _thread_start_probe(thread)[1]
            ):
                iface._receive_recovery_attempts = 0

    def start_receive_thread(
        self,
        *,
        name: str,
        reset_recovery: bool = True,
        create_thread: Callable[..., ThreadLike] | None = None,
        start_thread: Callable[[ThreadLike], None] | None = None,
    ) -> None:
        """Create and start the background receive thread.

        Parameters
        ----------
        name : str
            Thread name used for diagnostics/logging.
        reset_recovery : bool
            Whether to reset ``_receive_recovery_attempts`` after successful
            startup.
        create_thread : Callable[..., ThreadLike] | None
            Optional thread factory override used by tests/compatibility flows.
        start_thread : Callable[[ThreadLike], None] | None
            Optional thread starter override used by tests/compatibility flows.
        """
        create_runtime_thread = create_thread or self._thread_access.create_thread
        start_runtime_thread = start_thread or self._thread_access.start_thread
        thread, recovery_attempts_before_start = self._check_receive_start_conditions(
            name=name,
            reset_recovery=reset_recovery,
            create_runtime_thread=create_runtime_thread,
        )
        if thread is None:
            return
        self._create_and_start_receive_thread(
            thread,
            start_runtime_thread=start_runtime_thread,
        )
        if not self._probe_receive_thread_start(thread, name=name):
            return
        self._maybe_reset_receive_recovery(
            thread=thread,
            recovery_attempts_before_start=recovery_attempts_before_start,
        )
