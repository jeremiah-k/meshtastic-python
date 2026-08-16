"""Receive lifecycle coordinator runtime ownership for BLE."""

import threading
import time
from collections.abc import Callable
from typing import TYPE_CHECKING

from meshtastic.interfaces.ble.constants import logger
from meshtastic.interfaces.ble.coordination import ThreadLike
from meshtastic.interfaces.ble.lifecycle_decisions import (
    _decide_receive_start,
    _ReceiveStartDecision,
    _ReceiveStartDisposition,
    _ReceiveStartSnapshot,
    _ReceiveThreadProbe,
)
from meshtastic.interfaces.ble.lifecycle_primitives import _LifecycleThreadAccess
from meshtastic.interfaces.ble.ports import _BLESessionStatePort
from meshtastic.interfaces.ble.session_state import _session_state_for
from meshtastic.interfaces.ble.utils import _thread_start_probe

if TYPE_CHECKING:
    from meshtastic.interfaces.ble.interface import BLEInterface

RECEIVE_START_PENDING_TIMEOUT_SECONDS = 1.0
RECEIVE_START_SNAPSHOT_MAX_ATTEMPTS = 16


def _thread_display_name(thread: ThreadLike) -> str:
    """Return a defensive display name for a thread-like collaborator.

    Attribute access and ``repr`` may execute collaborator-owned code, so this
    helper is called only outside the shared session lock.
    """
    try:
        raw_name = thread.name
    except Exception:  # noqa: BLE001 - diagnostics must never disrupt lifecycle
        raw_name = None
    if isinstance(raw_name, str) and raw_name:
        return raw_name
    try:
        return repr(thread)
    except Exception:  # noqa: BLE001 - diagnostics must remain best effort
        return f"<{type(thread).__name__}>"


class BLEReceiveLifecycleCoordinator:
    """Own receive-loop intent and receive-thread lifecycle behavior.

    Parameters
    ----------
    iface : BLEInterface
        Interface instance whose receive-thread lifecycle is coordinated by
        this collaborator.
    """

    def __init__(
        self,
        iface: "BLEInterface",
        *,
        session_state: _BLESessionStatePort | None = None,
    ) -> None:
        """Bind receive lifecycle ownership to a specific interface.

        Parameters
        ----------
        iface : BLEInterface
            Interface instance owning receive state and thread references.
        session_state : _BLESessionStatePort | None
            Optional shared lifecycle state. When ``None``, resolve the
            interface-owned state or use the legacy adapter.

        Returns
        -------
        None
            Initializes bound collaborator state.
        """
        self._iface = iface
        self._session = _session_state_for(iface, session_state)
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
        with self._deferred_restart_lock:
            if self._deferred_restart_inflight:
                return
            self._deferred_restart_inflight = True

        def _deferred_restart() -> None:
            retry_requested = False
            try:
                wait_deadline = time.monotonic() + RECEIVE_START_PENDING_TIMEOUT_SECONDS
                while True:
                    with self._session.lock:
                        if self._session.closed or not self._session.want_receive:
                            return
                        current = self._session.receive_thread
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
                            with self._session.lock:
                                if self._session.receive_thread is existing_thread:
                                    self._session.receive_start_pending = False
                                    self._session.receive_start_pending_since = None
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
        with self._session.lock:
            self._session.want_receive = want_receive

    def should_run_receive_loop(self) -> bool:
        """Return whether receive loop should continue running."""
        with self._session.lock:
            return self._session.want_receive and not self._session.closed

    @staticmethod
    def _is_thread_start_failure_confirmed(thread: ThreadLike) -> bool:
        """Return whether startup probes confirm that ``thread`` failed to start."""
        started_event = getattr(thread, "_started", None)
        is_started = getattr(started_event, "is_set", None)
        if callable(is_started):
            try:
                if bool(is_started()):
                    return False
                thread_ident, _ = _thread_start_probe(thread)
                return thread_ident is not None
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

    @staticmethod
    def _probe_receive_thread(existing: ThreadLike | None) -> _ReceiveThreadProbe:
        """Probe one receive-thread reference without holding lifecycle locks."""
        if existing is None:
            return _ReceiveThreadProbe(
                ident=None,
                is_alive=False,
                is_current=False,
                start_failure_confirmed=False,
                display_name="<no receive thread>",
            )
        ident, is_alive = _thread_start_probe(existing)
        is_current = existing is threading.current_thread() or (
            isinstance(ident, int) and ident == threading.get_ident()
        )
        start_failure_confirmed = False
        if not is_alive and not is_current and ident is None:
            start_failure_confirmed = (
                BLEReceiveLifecycleCoordinator._is_thread_start_failure_confirmed(
                    existing
                )
            )
        return _ReceiveThreadProbe(
            ident=ident,
            is_alive=is_alive,
            is_current=is_current,
            start_failure_confirmed=start_failure_confirmed,
            display_name=_thread_display_name(existing),
        )

    def _apply_receive_start_decision(
        self,
        *,
        decision: _ReceiveStartDecision,
        snapshot: _ReceiveStartSnapshot,
        probe: _ReceiveThreadProbe,
        name: str,
        reset_recovery: bool,
    ) -> tuple[ThreadLike | None, ThreadLike | None, bool]:
        """Apply one stable receive-start decision under the session lock.

        Returns
        -------
        tuple[ThreadLike | None, ThreadLike | None, bool]
            Deferred-current thread, inconclusive-probe thread, and whether the
            caller should return without staging a new thread.
        """
        existing = snapshot.existing
        disposition = decision.disposition
        deferred: ThreadLike | None = None
        inconclusive: ThreadLike | None = None
        should_return = False

        if disposition is _ReceiveStartDisposition.DEFER_CURRENT_TIMEOUT:
            self._session.receive_start_pending = False
            self._session.receive_start_pending_since = None
            logger.debug(
                "Receive-thread deferral timeout reached (%s): scheduling deferred restart.",
                name,
            )
            deferred = existing
            should_return = True
        elif disposition is _ReceiveStartDisposition.DEFER_CURRENT:
            self._session.receive_start_pending = True
            if decision.initialize_pending_since:
                self._session.receive_start_pending_since = time.monotonic()
            message = (
                "Deferring replacement receive thread start (%s): current receive "
                "thread is still unwinding (pending %.3fs)."
                if not reset_recovery
                else "Deferring receive thread start (%s): current receive thread "
                "is still unwinding (pending %.3fs)."
            )
            logger.debug(message, name, decision.pending_age)
            should_return = True
        elif disposition is _ReceiveStartDisposition.SKIP_RUNNING:
            self._session.receive_start_pending = False
            self._session.receive_start_pending_since = None
            logger.debug(
                "Skipping receive thread start (%s): %s is already running.",
                name,
                probe.display_name,
            )
            should_return = True
        elif disposition is _ReceiveStartDisposition.REPLACE_STALE_PENDING:
            logger.debug(
                "Replacing stale pending receive-thread start reference for %s: "
                "worker is no longer alive.",
                probe.display_name,
            )
            self._session.receive_thread = None
            self._session.receive_start_pending = False
            self._session.receive_start_pending_since = None
        elif disposition is _ReceiveStartDisposition.REPLACE_STALE_PENDING_TIMEOUT:
            logger.debug(
                "Receive thread start pending timed out for %s after %.3fs; "
                "replacing stale pending reference.",
                probe.display_name,
                decision.pending_age,
            )
            self._session.receive_start_pending = False
            self._session.receive_start_pending_since = None
        elif disposition is _ReceiveStartDisposition.WAIT_PENDING:
            if decision.initialize_pending_since:
                self._session.receive_start_pending_since = time.monotonic()
            logger.debug(
                "Skipping receive thread start (%s): %s start still pending.",
                name,
                probe.display_name,
            )
            should_return = True
        elif disposition is _ReceiveStartDisposition.REPLACE_DEAD:
            logger.debug(
                "Replacing dead receive thread reference for %s before restart.",
                probe.display_name,
            )
            self._session.receive_thread = None
            self._session.receive_start_pending = False
            self._session.receive_start_pending_since = None
        elif disposition is _ReceiveStartDisposition.WAIT_INCONCLUSIVE:
            if decision.initialize_pending_since:
                self._session.receive_start_pending_since = time.monotonic()
            self._session.receive_start_pending = True
            logger.debug(
                "Skipping receive thread start (%s): %s liveness probe inconclusive.",
                name,
                probe.display_name,
            )
            inconclusive = existing
            should_return = True
        elif disposition is _ReceiveStartDisposition.CLEAR_FAILED_START:
            self._session.receive_start_pending = False
            self._session.receive_start_pending_since = None

        return deferred, inconclusive, should_return

    def _check_receive_start_conditions(  # pylint: disable=too-many-return-statements
        self,
        *,
        name: str,
        reset_recovery: bool,
        create_runtime_thread: Callable[..., ThreadLike],
    ) -> tuple[ThreadLike | None, int | None]:
        """Validate start preconditions and create a staged receive thread.

        Thread probes run outside the shared session lock. A stable snapshot is
        revalidated before the pure decision is applied so collaborator code can
        never extend the critical section or overwrite newer lifecycle state.
        """
        iface = self._iface
        expected_existing: ThreadLike | None = None
        recovery_attempts_before_start: int | None = None
        deferred_current_thread: ThreadLike | None = None
        schedule_deferred_restart_for: ThreadLike | None = None
        inconclusive_probe_thread: ThreadLike | None = None
        should_return_after_decision = False

        for _snapshot_attempt in range(RECEIVE_START_SNAPSHOT_MAX_ATTEMPTS):
            with self._session.lock:
                if self._session.closed or not self._session.want_receive:
                    logger.debug(
                        "Skipping receive thread start (%s): interface is closing/stopped.",
                        name,
                    )
                    return None, None
                snapshot = _ReceiveStartSnapshot(
                    existing=self._session.receive_thread,
                    start_pending=self._session.receive_start_pending,
                    pending_since=self._session.receive_start_pending_since,
                )

            probe = self._probe_receive_thread(snapshot.existing)
            decision = _decide_receive_start(
                snapshot,
                probe,
                now=time.monotonic(),
                pending_timeout=RECEIVE_START_PENDING_TIMEOUT_SECONDS,
            )

            with self._session.lock:
                if self._session.closed or not self._session.want_receive:
                    continue
                if (
                    self._session.receive_thread is not snapshot.existing
                    or self._session.receive_start_pending != snapshot.start_pending
                    or self._session.receive_start_pending_since
                    != snapshot.pending_since
                ):
                    continue

                deferred, inconclusive, should_return = (
                    self._apply_receive_start_decision(
                        decision=decision,
                        snapshot=snapshot,
                        probe=probe,
                        name=name,
                        reset_recovery=reset_recovery,
                    )
                )
                if deferred is not None:
                    deferred_current_thread = deferred
                if inconclusive is not None:
                    inconclusive_probe_thread = inconclusive
                should_return_after_decision = should_return
                if (
                    decision.disposition is _ReceiveStartDisposition.DEFER_CURRENT
                    and decision.schedule_deferred_restart
                ):
                    schedule_deferred_restart_for = snapshot.existing

                if not should_return:
                    expected_existing = self._session.receive_thread
                    recovery_attempts_before_start = (
                        self._session.receive_recovery_attempts
                        if reset_recovery
                        else None
                    )
                break
        else:
            logger.debug(
                "Skipping receive thread start (%s): lifecycle state changed during %d consecutive liveness snapshots.",
                name,
                RECEIVE_START_SNAPSHOT_MAX_ATTEMPTS,
            )
            return None, None

        if inconclusive_probe_thread is not None:
            self._schedule_deferred_receive_restart(
                existing_thread=inconclusive_probe_thread,
                name=name,
                reset_recovery=reset_recovery,
                clear_pending_if_alive=True,
                enforce_pending_timeout=True,
            )
            return None, None
        if should_return_after_decision:
            deferred_target = (
                schedule_deferred_restart_for
                if schedule_deferred_restart_for is not None
                else deferred_current_thread
            )
            if deferred_target is not None:
                self._schedule_deferred_receive_restart(
                    existing_thread=deferred_target,
                    name=name,
                    reset_recovery=reset_recovery,
                )
            return None, None

        thread = create_runtime_thread(
            target=iface._receive_from_radio_impl,
            name=name,
            daemon=True,
        )
        expected_existing_is_alive = (
            _thread_start_probe(expected_existing)[1]
            if expected_existing is not None
            else False
        )
        with self._session.lock:
            if self._session.closed or not self._session.want_receive:
                return None, None
            if self._session.receive_thread is not expected_existing:
                logger.debug(
                    "Skipping receive thread publish (%s): receive thread reference changed concurrently.",
                    name,
                )
                return None, None
            if expected_existing is not None and expected_existing_is_alive:
                self._session.receive_start_pending = False
                self._session.receive_start_pending_since = None
                logger.debug(
                    "Skipping receive thread start (%s): existing thread became active while staging replacement.",
                    name,
                )
                return None, None
            self._session.receive_thread = thread
            self._session.receive_start_pending = True
            self._session.receive_start_pending_since = time.monotonic()
            return thread, recovery_attempts_before_start

    def _create_and_start_receive_thread(
        self,
        thread: ThreadLike,
        *,
        start_runtime_thread: Callable[[ThreadLike], None],
    ) -> bool:
        """Start staged receive thread and clear stale reference on failure."""
        with self._session.lock:
            if self._session.receive_thread is not thread:
                return False
            if self._session.closed or not self._session.want_receive:
                return False
        try:
            start_runtime_thread(thread)
        except (SystemExit, KeyboardInterrupt):  # pylint: disable=W0706
            with self._session.lock:
                if self._session.receive_thread is thread:
                    self._session.receive_thread = None
                    self._session.receive_start_pending = False
                    self._session.receive_start_pending_since = None
            raise
        except (
            Exception
        ):  # noqa: BLE001 - start failure must clear stale thread reference
            with self._session.lock:
                if self._session.receive_thread is thread:
                    self._session.receive_thread = None
                    self._session.receive_start_pending = False
                    self._session.receive_start_pending_since = None
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
        _, thread_is_alive = _thread_start_probe(thread)
        if thread_is_alive:
            with self._session.lock:
                if self._session.receive_thread is thread:
                    self._session.receive_start_pending = False
                    self._session.receive_start_pending_since = None
            return True
        start_failure_confirmed = self._is_thread_start_failure_confirmed(thread)

        if start_failure_confirmed:
            with self._session.lock:
                if self._session.receive_thread is thread:
                    self._session.receive_thread = None
                    self._session.receive_start_pending = False
                    self._session.receive_start_pending_since = None
            logger.debug(
                "Receive thread %s did not start; cleared stale thread reference.",
                name,
            )
        else:
            with self._session.lock:
                if self._session.receive_thread is thread:
                    self._session.receive_start_pending = True
                    pending_since = self._session.receive_start_pending_since
                    if not isinstance(pending_since, (float, int)):
                        self._session.receive_start_pending_since = time.monotonic()
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
        if recovery_attempts_before_start is None:
            return
        thread_is_alive = _thread_start_probe(thread)[1]
        if not thread_is_alive:
            return
        with self._session.lock:
            if (
                self._session.receive_thread is thread
                and self._session.receive_recovery_attempts
                == recovery_attempts_before_start
            ):
                self._session.receive_recovery_attempts = 0

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
