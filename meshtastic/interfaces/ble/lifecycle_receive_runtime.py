"""Receive lifecycle coordinator runtime ownership for BLE."""

import threading
from collections.abc import Callable
from typing import TYPE_CHECKING

from meshtastic.interfaces.ble.constants import logger
from meshtastic.interfaces.ble.coordination import ThreadLike
from meshtastic.interfaces.ble.lifecycle_primitives import _LifecycleThreadAccess
from meshtastic.interfaces.ble.utils import _thread_start_probe

if TYPE_CHECKING:
    from meshtastic.interfaces.ble.interface import BLEInterface


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

    def start_receive_thread(
        self,
        *,
        name: str,
        reset_recovery: bool = True,
        create_thread: Callable[..., ThreadLike] | None = None,
        start_thread: Callable[[object], None] | None = None,
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
        start_thread : Callable[[object], None] | None
            Optional thread starter override used by tests/compatibility flows.
        """
        iface = self._iface
        create_runtime_thread = create_thread or self._thread_access.create_thread
        start_runtime_thread = start_thread or self._thread_access.start_thread
        recovery_attempts_before_start: int | None = None
        with iface._state_lock:
            if iface._closed or not iface._want_receive:
                logger.debug(
                    "Skipping receive thread start (%s): interface is closing/stopped.",
                    name,
                )
                return
            existing = iface._receiveThread
            existing_ident, existing_is_alive = (
                _thread_start_probe(existing) if existing is not None else (None, False)
            )
            if (
                existing is not None
                and existing is not threading.current_thread()
                and (existing_is_alive or existing_ident is None)
            ):
                logger.debug(
                    "Skipping receive thread start (%s): %s is already running or pending start.",
                    name,
                    existing.name,
                )
                return
            thread = create_runtime_thread(
                target=iface._receive_from_radio_impl,
                name=name,
                daemon=True,
            )
            iface._receiveThread = thread
            if reset_recovery:
                recovery_attempts_before_start = iface._receive_recovery_attempts
        try:
            start_runtime_thread(thread)
        except (SystemExit, KeyboardInterrupt):  # pylint: disable=W0706
            with iface._state_lock:
                if iface._receiveThread is thread:
                    iface._receiveThread = None
            raise
        except (
            Exception
        ):  # noqa: BLE001 - start failure must clear stale thread reference
            with iface._state_lock:
                if iface._receiveThread is thread:
                    iface._receiveThread = None
            raise
        thread_ident, thread_is_alive = _thread_start_probe(thread)
        if thread_ident is None and not thread_is_alive:
            started_event = getattr(thread, "_started", None)
            is_started = getattr(started_event, "is_set", None)
            start_failure_confirmed = False
            if callable(is_started):
                try:
                    start_failure_confirmed = not bool(is_started())
                except Exception:  # noqa: BLE001 - probe remains best effort
                    start_failure_confirmed = False
            elif isinstance(thread, threading.Thread):
                # Native thread with no ident and not alive indicates startup never happened.
                start_failure_confirmed = True

            if start_failure_confirmed:
                with iface._state_lock:
                    if iface._receiveThread is thread:
                        iface._receiveThread = None
                logger.debug(
                    "Receive thread %s did not start; cleared stale thread reference.",
                    name,
                )
            else:
                logger.debug(
                    "Receive thread %s start probe inconclusive; keeping thread reference.",
                    name,
                )
            return
        if reset_recovery:
            with iface._state_lock:
                if (
                    recovery_attempts_before_start is not None
                    and iface._receive_recovery_attempts
                    == recovery_attempts_before_start
                ):
                    iface._receive_recovery_attempts = 0
