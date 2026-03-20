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
    """Own receive-loop intent and receive-thread lifecycle behavior.

    Parameters
    ----------
    iface : BLEInterface
        Interface instance whose receive-thread lifecycle is coordinated by
        this collaborator.
    """

    def __init__(self, iface: "BLEInterface") -> None:
        """Bind receive lifecycle ownership to a specific interface.

        Parameters
        ----------
        iface : BLEInterface
            Interface instance owning receive state and thread references.

        Returns
        -------
        None
            Initializes bound collaborator state.
        """
        self._iface = iface
        self._thread_access = _LifecycleThreadAccess(iface)
        self._deferred_restart_lock = threading.Lock()
        self._deferred_restart_inflight = False

    def _schedule_deferred_receive_restart(
        self,
        *,
        existing_thread: ThreadLike,
        name: str,
        reset_recovery: bool,
        clear_pending_if_alive: bool = False,
        enforce_pending_timeout: bool = False,
    ) -> None:
        """Schedule a best-effort receive restart after current-thread deferral.

        Parameters
        ----------
        existing_thread : ThreadLike
            Thread reference that should unwind before a restart is staged.
        name : str
            Thread name used for deferred helper and diagnostic messages.
        reset_recovery : bool
            Whether the eventual restart should reset recovery attempts.
        clear_pending_if_alive : bool, default False
            When ``True``, clear pending flags once ``existing_thread`` proves
            alive instead of scheduling a replacement.
        enforce_pending_timeout : bool, default False
            When ``True``, require a full pending-timeout window before
            evaluating restart progression.

        Returns
        -------
        None
            Scheduling is best effort.

        Notes
        -----
        This helper avoids replacing ``_receiveThread`` inline while the active
        receive thread is unwinding. A short-lived daemon task waits for the
        deferred thread to unwind (or for the wait window to elapse), then
        re-enters ``start_receive_thread``.
        """
        iface = self._iface
        with self._deferred_restart_lock:
            if self._deferred_restart_inflight:
                return
            self._deferred_restart_inflight = True

        def _deferred_restart() -> None:
            retry_requested = False
            try:
                wait_deadline = time.monotonic() + RECEIVE_START_PENDING_TIMEOUT_SECONDS
                while True:
                    with iface._state_lock:
                        if iface._closed or not iface._want_receive:
                            return
                        current = iface._receiveThread
                    if enforce_pending_timeout and time.monotonic() < wait_deadline:
                        time.sleep(0.01)
                        continue
                    if current is not existing_thread:
                        if enforce_pending_timeout:
                            return
                        break
                    _, current_is_alive = _thread_start_probe(existing_thread)
                    if clear_pending_if_alive:
                        if current_is_alive:
                            with iface._state_lock:
                                if iface._receiveThread is existing_thread:
                                    iface._receive_start_pending = False
                                    iface._receive_start_pending_since = None
                            return
                        if time.monotonic() >= wait_deadline:
                            break
                        time.sleep(0.01)
                        continue
                    if not current_is_alive:
                        break
                    if time.monotonic() >= wait_deadline:
                        logger.debug(
                            "Deferred receive restart (%s) still waiting for current thread unwind.",
                            name,
                        )
                        wait_deadline = (
                            time.monotonic() + RECEIVE_START_PENDING_TIMEOUT_SECONDS
                        )
                        time.sleep(0.01)
                        continue
                    time.sleep(0.01)
                self.start_receive_thread(name=name, reset_recovery=reset_recovery)
            except Exception:  # noqa: BLE001 - deferred restart must re-arm failures
                logger.error(
                    "Deferred receive restart (%s) failed.",
                    name,
                    exc_info=True,
                )
                retry_requested = True
            finally:
                with self._deferred_restart_lock:
                    self._deferred_restart_inflight = False
            if retry_requested:
                time.sleep(0.05)
                self._schedule_deferred_receive_restart(
                    existing_thread=existing_thread,
                    name=name,
                    reset_recovery=reset_recovery,
                    clear_pending_if_alive=clear_pending_if_alive,
                    enforce_pending_timeout=enforce_pending_timeout,
                )

        try:
            deferred_restart_thread = threading.Thread(
                target=_deferred_restart,
                name=f"{name}DeferredStart",
                daemon=True,
            )
            deferred_restart_thread.start()
        except Exception:  # noqa: BLE001 - helper launch remains best effort
            with self._deferred_restart_lock:
                self._deferred_restart_inflight = False
            logger.debug(
                "Failed to launch deferred receive restart helper (%s).",
                name,
                exc_info=True,
            )

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
                if not bool(is_started()):
                    return False
            except Exception:  # noqa: BLE001 - probe remains best effort
                return False
        return False

    @staticmethod
    def _is_current_receive_thread(thread: ThreadLike | None) -> bool:
        """Return whether ``thread`` is the current receive thread.

        Treats ThreadLike proxies with the same thread ident as the current
        receive thread as equivalent to the current thread.
        """
        if thread is threading.current_thread():
            return True
        if thread is None:
            return False
        thread_ident, _ = _thread_start_probe(thread)
        return isinstance(thread_ident, int) and thread_ident == threading.get_ident()

    def _check_receive_start_conditions(
        self,
        *,
        name: str,
        reset_recovery: bool,
        create_runtime_thread: Callable[..., ThreadLike],
    ) -> tuple[ThreadLike | None, int | None]:
        """Validate start preconditions and create a staged receive thread."""
        iface = self._iface
        expected_existing: ThreadLike | None = None
        recovery_attempts_before_start: int | None = None
        deferred_current_thread: ThreadLike | None = None
        deferred_current_thread_waiting = False
        schedule_deferred_restart_for: ThreadLike | None = None
        with iface._state_lock:
            if iface._closed or not iface._want_receive:
                logger.debug(
                    "Skipping receive thread start (%s): interface is closing/stopped.",
                    name,
                )
                return None, None
            existing = iface._receiveThread
            existing_start_pending = bool(
                getattr(iface, "_receive_start_pending", False)
            )
            existing_ident: int | None = None
            existing_is_alive = False
            if existing is not None:
                existing_ident, existing_is_alive = _thread_start_probe(existing)
            if self._is_current_receive_thread(existing):
                now = time.monotonic()
                pending_since = getattr(iface, "_receive_start_pending_since", None)
                if not existing_start_pending or not isinstance(
                    pending_since, (float, int)
                ):
                    pending_age = 0.0
                else:
                    pending_age = now - float(pending_since)
                    if pending_age >= RECEIVE_START_PENDING_TIMEOUT_SECONDS:
                        iface._receive_start_pending = False
                        iface._receive_start_pending_since = None
                        deferred_current_thread = existing
                        logger.debug(
                            "Receive-thread deferral timeout reached (%s): scheduling deferred restart.",
                            name,
                        )
                if deferred_current_thread is None:
                    deferred_current_thread_waiting = True
                    iface._receive_start_pending = True
                    if not isinstance(pending_since, (float, int)):
                        iface._receive_start_pending_since = now
                        schedule_deferred_restart_for = existing
                    elif not existing_start_pending:
                        schedule_deferred_restart_for = existing
                    if not reset_recovery:
                        logger.debug(
                            "Deferring replacement receive thread start (%s): current receive thread is still unwinding (pending %.3fs).",
                            name,
                            pending_age,
                        )
                    else:
                        logger.debug(
                            "Deferring receive thread start (%s): current receive thread is still unwinding (pending %.3fs).",
                            name,
                            pending_age,
                        )
                else:
                    logger.debug(
                        "Deferring receive thread start (%s) and queueing restart after current thread unwind.",
                        name,
                    )
            if existing is not None and not self._is_current_receive_thread(existing):
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
                    if not existing_is_alive and (
                        existing_ident is not None
                        or self._is_thread_start_failure_confirmed(existing)
                    ):
                        logger.debug(
                            "Replacing stale pending receive-thread start reference for %s: worker is no longer alive.",
                            getattr(existing, "name", repr(existing)),
                        )
                        iface._receive_start_pending = False
                        iface._receive_start_pending_since = None
                    else:
                        pending_since = getattr(
                            iface, "_receive_start_pending_since", None
                        )
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
                        self._schedule_deferred_receive_restart(
                            existing_thread=existing,
                            name=name,
                            reset_recovery=reset_recovery,
                            enforce_pending_timeout=True,
                        )
                        logger.debug(
                            "Skipping receive thread start (%s): %s liveness probe inconclusive.",
                            name,
                            existing.name,
                        )
                        return None, None
                    iface._receive_start_pending = False
                    iface._receive_start_pending_since = None
            if deferred_current_thread is None:
                expected_existing = existing
                recovery_attempts_before_start = (
                    iface._receive_recovery_attempts if reset_recovery else None
                )
        if deferred_current_thread_waiting:
            if schedule_deferred_restart_for is not None:
                self._schedule_deferred_receive_restart(
                    existing_thread=schedule_deferred_restart_for,
                    name=name,
                    reset_recovery=reset_recovery,
                )
            return None, None
        if deferred_current_thread is not None:
            self._schedule_deferred_receive_restart(
                existing_thread=deferred_current_thread,
                name=name,
                reset_recovery=reset_recovery,
            )
            return None, None
        thread = create_runtime_thread(
            target=iface._receive_from_radio_impl,
            name=name,
            daemon=True,
        )
        with iface._state_lock:
            if iface._closed or not iface._want_receive:
                return None, None
            if iface._receiveThread is not expected_existing:
                logger.debug(
                    "Skipping receive thread publish (%s): receive thread reference changed concurrently.",
                    name,
                )
                return None, None
            if (
                expected_existing is not None
                and _thread_start_probe(expected_existing)[1]
            ):
                iface._receive_start_pending = False
                iface._receive_start_pending_since = None
                logger.debug(
                    "Skipping receive thread start (%s): existing thread became active while staging replacement.",
                    name,
                )
                return None, None
            iface._receiveThread = thread
            iface._receive_start_pending = True
            iface._receive_start_pending_since = time.monotonic()
            return thread, recovery_attempts_before_start

    def _create_and_start_receive_thread(
        self,
        thread: ThreadLike,
        *,
        start_runtime_thread: Callable[[ThreadLike], None],
    ) -> bool:
        """Start staged receive thread and clear stale reference on failure."""
        iface = self._iface
        with iface._state_lock:
            if iface._receiveThread is not thread:
                return False
            if iface._closed or not iface._want_receive:
                return False
        try:
            start_runtime_thread(thread)
        except (SystemExit, KeyboardInterrupt):  # pylint: disable=W0706
            with iface._state_lock:
                if iface._receiveThread is thread:
                    iface._receiveThread = None
                    iface._receive_start_pending = False
                    iface._receive_start_pending_since = None
            raise
        except (
            Exception
        ):  # noqa: BLE001 - start failure must clear stale thread reference
            with iface._state_lock:
                if iface._receiveThread is thread:
                    iface._receiveThread = None
                    iface._receive_start_pending = False
                    iface._receive_start_pending_since = None
            raise
        return True

    def _probe_receive_thread_start(
        self,
        thread: ThreadLike,
        *,
        name: str,
        reset_recovery: bool,
    ) -> bool:
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
            self._schedule_deferred_receive_restart(
                existing_thread=thread,
                name=name,
                reset_recovery=reset_recovery,
                clear_pending_if_alive=True,
                enforce_pending_timeout=True,
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
        started = self._create_and_start_receive_thread(
            thread,
            start_runtime_thread=start_runtime_thread,
        )
        if not started:
            return
        if not self._probe_receive_thread_start(
            thread,
            name=name,
            reset_recovery=reset_recovery,
        ):
            return
        self._maybe_reset_receive_recovery(
            thread=thread,
            recovery_attempts_before_start=recovery_attempts_before_start,
        )
