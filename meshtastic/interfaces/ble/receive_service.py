"""Receive-loop and recovery helpers for BLE interface orchestration."""

import math
import time
from typing import TYPE_CHECKING

from bleak.exc import BleakDBusError, BleakError

from meshtastic.interfaces.ble.client import BLEClient
from meshtastic.interfaces.ble.constants import (
    ERROR_READING_BLE,
    FROMRADIO_UUID,
    GATT_IO_TIMEOUT,
    READ_TRIGGER_EVENT,
    RECEIVE_RECOVERY_MAX_BACKOFF_SEC,
    RECEIVE_RECOVERY_RAPID_FAILURE_THRESHOLD,
    RECEIVE_RECOVERY_STABILITY_RESET_SEC,
    RECONNECTED_EVENT,
    BLEConfig,
    logger,
)
from meshtastic.interfaces.ble.errors import DecodeError
from meshtastic.interfaces.ble.lifecycle_service import BLELifecycleService
from meshtastic.interfaces.ble.utils import (
    _is_unconfigured_mock_callable,
    _is_unconfigured_mock_member,
    _sleep,
)

if TYPE_CHECKING:
    from meshtastic.interfaces.ble.coordination import ThreadCoordinator
    from meshtastic.interfaces.ble.interface import BLEInterface

COORDINATOR_WAIT_FALLBACK_SLEEP_SEC = 0.001
RECEIVE_THREAD_FATAL_REASON = "receive_thread_fatal"


class BLEReceiveRecoveryController:
    """Instance-bound collaborator for BLE receive-loop and recovery paths."""

    def __init__(self, iface: "BLEInterface") -> None:
        """Bind receive/recovery helpers to a specific interface instance."""
        self._iface = iface

    def _should_run_receive_loop(self) -> bool:
        """Return whether the lifecycle currently wants receive-loop execution."""
        iface = self._iface
        override_should_run_receive_loop = iface.__dict__.get("_should_run_receive_loop")
        if callable(override_should_run_receive_loop):
            return bool(override_should_run_receive_loop())
        get_lifecycle_controller = getattr(iface, "_get_lifecycle_controller", None)
        if callable(get_lifecycle_controller):
            return bool(get_lifecycle_controller().should_run_receive_loop())
        should_run_receive_loop = getattr(iface, "_should_run_receive_loop", None)
        if callable(should_run_receive_loop):
            return bool(should_run_receive_loop())
        return False

    def _set_receive_wanted(self, *, want_receive: bool) -> None:
        """Set receive-loop intent through lifecycle controller or legacy iface hook."""
        iface = self._iface
        override_set_receive_wanted = iface.__dict__.get("_set_receive_wanted")
        if callable(override_set_receive_wanted):
            override_set_receive_wanted(want_receive=want_receive)
            return
        get_lifecycle_controller = getattr(iface, "_get_lifecycle_controller", None)
        if callable(get_lifecycle_controller):
            get_lifecycle_controller().set_receive_wanted(want_receive=want_receive)
            return
        set_receive_wanted = getattr(iface, "_set_receive_wanted", None)
        if callable(set_receive_wanted):
            set_receive_wanted(want_receive=want_receive)

    def _handle_disconnect(
        self,
        disconnect_reason: str,
        *,
        client: BLEClient | None,
    ) -> bool:
        """Invoke disconnect handling through lifecycle controller or legacy hook."""
        iface = self._iface
        override_handle_disconnect = iface.__dict__.get("_handle_disconnect")
        if callable(override_handle_disconnect):
            return bool(override_handle_disconnect(disconnect_reason, client=client))
        get_lifecycle_controller = getattr(iface, "_get_lifecycle_controller", None)
        if callable(get_lifecycle_controller):
            return bool(
                get_lifecycle_controller().handle_disconnect(
                    disconnect_reason,
                    client=client,
                )
            )
        handle_disconnect = getattr(iface, "_handle_disconnect", None)
        if callable(handle_disconnect):
            return bool(handle_disconnect(disconnect_reason, client=client))
        return False

    def _start_receive_thread(self, *, name: str, reset_recovery: bool) -> None:
        """Start receive thread through lifecycle controller or legacy hook."""
        iface = self._iface
        override_start_receive_thread = iface.__dict__.get("_start_receive_thread")
        if callable(override_start_receive_thread):
            override_start_receive_thread(name=name, reset_recovery=reset_recovery)
            return
        get_lifecycle_controller = getattr(iface, "_get_lifecycle_controller", None)
        if callable(get_lifecycle_controller):
            get_lifecycle_controller().start_receive_thread(
                name=name,
                reset_recovery=reset_recovery,
            )
            return
        start_receive_thread = getattr(iface, "_start_receive_thread", None)
        if callable(start_receive_thread):
            start_receive_thread(name=name, reset_recovery=reset_recovery)

    def _close_after_fatal_read(self) -> None:
        """Close interface via lifecycle collaborator after fatal read failures."""
        iface = self._iface
        override_close = iface.__dict__.get("close")
        if callable(override_close):
            override_close()
            return
        get_lifecycle_controller = getattr(iface, "_get_lifecycle_controller", None)
        if callable(get_lifecycle_controller):
            from meshtastic.interfaces.ble import interface as interface_mod

            get_lifecycle_controller().close(
                management_shutdown_wait_timeout=(
                    interface_mod._MANAGEMENT_SHUTDOWN_WAIT_TIMEOUT_SECONDS
                ),
                management_wait_poll_seconds=interface_mod._MANAGEMENT_CONNECT_WAIT_POLL_SECONDS,
            )
            return
        close = getattr(iface, "close", None)
        if callable(close):
            close()

    def handle_read_loop_disconnect(
        self, error_message: str, previous_client: BLEClient
    ) -> bool:
        """Handle read-loop disconnect logic for the bound interface."""
        iface = self._iface
        override_handle_read_loop_disconnect = iface.__dict__.get(
            "_handle_read_loop_disconnect"
        )
        if callable(override_handle_read_loop_disconnect):
            return bool(
                override_handle_read_loop_disconnect(error_message, previous_client)
            )
        logger.debug("Device disconnected: %s", error_message)
        should_continue = self._handle_disconnect(
            f"read_loop: {error_message}",
            client=previous_client,
        )
        iface._read_retry_count = 0
        if not should_continue:
            self._set_receive_wanted(want_receive=False)
        return should_continue

    def _should_poll_without_notify(self) -> bool:
        """Return whether fallback polling is allowed without notify callbacks."""
        with self._iface._state_lock:
            return not self._iface._fromnum_notify_enabled

    def _wait_for_read_trigger(
        self,
        *,
        coordinator: "ThreadCoordinator",
        wait_timeout: float,
    ) -> tuple[bool, bool]:
        """Wait for read trigger and compute fallback poll mode."""
        event_signaled = BLEReceiveRecoveryService._coordinator_wait_for_event(
            coordinator,
            READ_TRIGGER_EVENT,
            timeout=wait_timeout,
        )
        poll_without_notify = False
        ever_connected = BLELifecycleService._ever_connected_flag(self._iface)
        if not event_signaled:
            if (
                ever_connected
                and BLEReceiveRecoveryService._coordinator_check_and_clear_event(
                    coordinator,
                    RECONNECTED_EVENT,
                )
            ):
                logger.debug("Detected reconnection, resuming normal operation")
            poll_without_notify = self._should_poll_without_notify()
            return poll_without_notify, poll_without_notify

        BLEReceiveRecoveryService._coordinator_clear_event(
            coordinator, READ_TRIGGER_EVENT
        )
        return True, poll_without_notify

    def _snapshot_client_state(self) -> tuple[BLEClient | None, bool, bool, bool]:
        """Snapshot client and gating flags needed by the read loop."""
        iface = self._iface
        with iface._state_lock:
            client = iface.client
            state_is_connecting = getattr(iface._state_manager, "is_connecting", None)
            if callable(state_is_connecting) and not _is_unconfigured_mock_callable(
                state_is_connecting
            ):
                connecting_result = state_is_connecting()
                is_connecting = (
                    connecting_result if isinstance(connecting_result, bool) else False
                )
            elif not _is_unconfigured_mock_member(state_is_connecting) and isinstance(
                state_is_connecting, bool
            ):
                is_connecting = state_is_connecting
            else:
                legacy_is_connecting = getattr(iface._state_manager, "_is_connecting", None)
                if callable(legacy_is_connecting) and not _is_unconfigured_mock_callable(
                    legacy_is_connecting
                ):
                    connecting_result = legacy_is_connecting()
                    is_connecting = (
                        connecting_result if isinstance(connecting_result, bool) else False
                    )
                elif not _is_unconfigured_mock_member(legacy_is_connecting) and isinstance(
                    legacy_is_connecting, bool
                ):
                    is_connecting = legacy_is_connecting
                else:
                    is_connecting = False
            publish_pending = iface._client_publish_pending
            state_is_closing = BLELifecycleService._state_manager_is_closing(iface)
            is_closing = state_is_closing or iface._closed
        return client, is_connecting, publish_pending, is_closing

    def _process_client_state(
        self,
        *,
        coordinator: "ThreadCoordinator",
        wait_timeout: float,
        client: BLEClient | None,
        is_connecting: bool,
        publish_pending: bool,
        is_closing: bool,
    ) -> bool:
        """Process current client state and decide whether to break loop."""
        iface = self._iface
        if client is None and is_closing:
            logger.debug("BLE client is None, shutting down")
            self._set_receive_wanted(want_receive=False)
            return True

        if publish_pending:
            logger.debug(
                "Skipping BLE read while connect publication is pending verification."
            )
            BLEReceiveRecoveryService._coordinator_wait_for_event(
                coordinator,
                RECONNECTED_EVENT,
                timeout=wait_timeout,
            )
            return True

        if client is not None:
            return False

        if iface.auto_reconnect or is_connecting:
            wait_reason = "connection establishment" if is_connecting else "auto-reconnect"
            logger.debug("BLE client is None; waiting for %s", wait_reason)
            BLEReceiveRecoveryService._coordinator_wait_for_event(
                coordinator,
                RECONNECTED_EVENT,
                timeout=wait_timeout,
            )
            return True

        logger.debug("BLE client is None; re-checking connection state")
        return True

    def _reset_recovery_after_stability(self) -> None:
        """Reset recovery-attempt counter after sustained stable reads."""
        iface = self._iface
        now = time.monotonic()
        with iface._state_lock:
            if (
                iface._receive_recovery_attempts > 0
                and now - iface._last_recovery_time
                >= RECEIVE_RECOVERY_STABILITY_RESET_SEC
            ):
                logger.debug(
                    "Resetting receive recovery attempts after %.1fs of stability.",
                    now - iface._last_recovery_time,
                )
                iface._receive_recovery_attempts = 0

    def _read_and_handle_payload(
        self,
        client: BLEClient,
        *,
        poll_without_notify: bool,
    ) -> bool:
        """Read a payload iteration and dispatch packet handling."""
        iface = self._iface
        payload = self.read_from_radio_with_retries(
            client,
            retry_on_empty=not poll_without_notify,
        )
        if not payload:
            iface._read_retry_count = 0
            return False
        logger.debug("FROMRADIO read: %s", payload.hex())
        try:
            iface._handle_from_radio(payload)
        except DecodeError as exc:
            logger.warning("Failed to parse FromRadio packet, discarding: %s", exc)
            iface._read_retry_count = 0
            return True
        self._reset_recovery_after_stability()
        iface._read_retry_count = 0
        return True

    def _handle_payload_read(
        self,
        client: BLEClient,
        *,
        poll_without_notify: bool,
    ) -> tuple[bool, bool]:
        """Handle one payload-read attempt and classify loop control flow."""
        iface = self._iface
        try:
            if not self._read_and_handle_payload(
                client,
                poll_without_notify=poll_without_notify,
            ):
                return True, False
            return False, False
        except BleakDBusError as exc:
            if self.handle_read_loop_disconnect(repr(exc), client):
                return True, False
            return True, True
        except (SystemExit, KeyboardInterrupt):  # pylint: disable=W0706
            raise
        except (BleakError, BLEClient.BLEError) as exc:
            try:
                self.handle_transient_read_error(exc)
                return False, False
            except iface.BLEError:
                logger.error("Fatal BLE read error after retries: %s", exc)
                if not iface._is_connection_closing:
                    self._close_after_fatal_read()
                return True, True
        except (RuntimeError, OSError) as exc:
            logger.error("Fatal error in BLE receive thread: %s", exc)
            if not iface._is_connection_closing:
                self._close_after_fatal_read()
            return True, True
        except Exception as exc:  # noqa: BLE001  # pragma: no cover
            logger.exception("Unexpected error in BLE read loop")
            if self.handle_read_loop_disconnect(repr(exc), client):
                return True, False
            return True, True

    def _run_receive_cycle(
        self,
        *,
        coordinator: "ThreadCoordinator",
        wait_timeout: float,
    ) -> bool:
        """Run receive-loop orchestration until shutdown or fatal stop."""
        while self._should_run_receive_loop():
            proceed, poll_without_notify = self._wait_for_read_trigger(
                coordinator=coordinator,
                wait_timeout=wait_timeout,
            )
            if not proceed:
                continue

            while self._should_run_receive_loop():
                client, is_connecting, publish_pending, is_closing = (
                    self._snapshot_client_state()
                )
                if self._process_client_state(
                    coordinator=coordinator,
                    wait_timeout=wait_timeout,
                    client=client,
                    is_connecting=is_connecting,
                    publish_pending=publish_pending,
                    is_closing=is_closing,
                ):
                    break

                if client is None:  # pragma: no cover
                    break

                break_inner_loop, stop_receive_loop = self._handle_payload_read(
                    client,
                    poll_without_notify=poll_without_notify,
                )
                if stop_receive_loop:
                    return False
                if break_inner_loop:
                    break
        return True

    def receive_from_radio_impl(self) -> None:
        """Run the receive loop for the bound interface."""
        iface = self._iface
        coordinator = iface.thread_coordinator
        wait_timeout = BLEConfig.RECEIVE_WAIT_TIMEOUT
        try:
            should_continue = self._run_receive_cycle(
                coordinator=coordinator,
                wait_timeout=wait_timeout,
            )
            if not should_continue:
                return
        except (SystemExit, KeyboardInterrupt):  # pylint: disable=W0706
            raise
        except (
            BleakDBusError,
            BLEClient.BLEError,
            BleakError,
            DecodeError,
            RuntimeError,
            OSError,
        ):
            logger.exception("Fatal error in BLE receive thread")
            self.recover_receive_thread(RECEIVE_THREAD_FATAL_REASON)
        except Exception:  # noqa: BLE001
            logger.exception("Unexpected fatal error in BLE receive thread")
            self.recover_receive_thread(RECEIVE_THREAD_FATAL_REASON)

    def recover_receive_thread(self, disconnect_reason: str) -> None:
        """Recover the receive thread for the bound interface."""
        iface = self._iface
        if iface._is_connection_closing:
            return
        with iface._state_lock:
            current_client = iface.client
        should_continue = self._handle_disconnect(
            disconnect_reason,
            client=current_client,
        )
        if not should_continue:
            self._set_receive_wanted(want_receive=False)
            return
        now = time.monotonic()
        with iface._state_lock:
            iface._receive_recovery_attempts += 1
            attempts = iface._receive_recovery_attempts
            last_recovery = iface._last_recovery_time
        logger.debug(
            "BLE receive recovery attempt scheduled: attempts=%d last_recovery_time=%.3f",
            attempts,
            last_recovery,
        )
        if attempts > RECEIVE_RECOVERY_RAPID_FAILURE_THRESHOLD:
            max_exponent = max(
                math.ceil(math.log2(max(RECEIVE_RECOVERY_MAX_BACKOFF_SEC, 1.0))),
                0,
            )
            exponent = min(
                attempts - RECEIVE_RECOVERY_RAPID_FAILURE_THRESHOLD,
                max_exponent,
            )
            backoff = min(2.0**exponent, RECEIVE_RECOVERY_MAX_BACKOFF_SEC)
            remaining_wait = backoff - (now - last_recovery)
            if remaining_wait > 0:
                logger.warning(
                    "Throttling BLE receive recovery: waiting %.1fs before retry (attempt %d)",
                    remaining_wait,
                    attempts,
                )
                logger.debug(
                    "BLE receive recovery backoff: attempts=%d backoff=%.3f remaining_wait=%.3f",
                    attempts,
                    backoff,
                    remaining_wait,
                )
                if iface._shutdown_event.wait(timeout=remaining_wait):
                    logger.debug(
                        "BLE receive recovery aborted by shutdown during backoff wait (attempt %d)",
                        attempts,
                    )
                    return
                logger.debug(
                    "BLE receive recovery backoff wait elapsed; retrying restart (attempt %d)",
                    attempts,
                )
        with iface._state_lock:
            iface._last_recovery_time = time.monotonic()
            updated_last_recovery_time = iface._last_recovery_time
        iface._read_retry_count = 0
        logger.debug(
            "BLE receive recovery timestamp updated: attempts=%d last_recovery_time=%.3f",
            attempts,
            updated_last_recovery_time,
        )
        should_restart = self._should_run_receive_loop()
        logger.debug(
            "BLE receive recovery restart decision: attempts=%d should_restart=%s reset_recovery=%s",
            attempts,
            should_restart,
            False,
        )
        if should_restart:
            try:
                self._start_receive_thread(
                    name="BLEReceiveRecovery", reset_recovery=False
                )
            except Exception:  # noqa: BLE001 - preserve existing failure behavior
                logger.warning(
                    "BLE receive recovery restart failed (attempt %d)",
                    attempts,
                    exc_info=True,
                )
                raise
            logger.debug(
                "BLE receive recovery restart started (attempt %d)",
                attempts,
            )

    def read_from_radio_with_retries(
        self, client: BLEClient, *, retry_on_empty: bool = True
    ) -> bytes | None:
        """Read FROMRADIO payload for the bound interface with retry policy."""
        iface = self._iface
        override_read_from_radio_with_retries = iface.__dict__.get(
            "_read_from_radio_with_retries"
        )
        if callable(override_read_from_radio_with_retries):
            return override_read_from_radio_with_retries(
                client,
                retry_on_empty=retry_on_empty,
            )
        max_retries = BLEConfig.EMPTY_READ_MAX_RETRIES if retry_on_empty else 0
        read_timeout = GATT_IO_TIMEOUT if retry_on_empty else BLEConfig.RECEIVE_WAIT_TIMEOUT
        for attempt in range(max_retries + 1):
            payload = client.read_gatt_char(FROMRADIO_UUID, timeout=read_timeout)
            if payload:
                iface._suppressed_empty_read_warnings = 0
                return payload
            if attempt < max_retries:
                _sleep(iface._retry_policy_get_delay(iface._empty_read_policy, attempt))
        if retry_on_empty:
            self.log_empty_read_warning()
        return None

    def handle_transient_read_error(self, error: BleakError | BLEClient.BLEError) -> None:
        """Apply transient-read retry policy for the bound interface."""
        iface = self._iface
        override_handle_transient_read_error = iface.__dict__.get(
            "_handle_transient_read_error"
        )
        if callable(override_handle_transient_read_error):
            override_handle_transient_read_error(error)
            return
        transient_policy = iface._transient_read_policy
        if iface._retry_policy_should_retry(transient_policy, iface._read_retry_count):
            attempt_index = iface._read_retry_count
            iface._read_retry_count += 1
            logger.debug(
                "Transient BLE read error, retrying (%d/%d)",
                iface._read_retry_count,
                BLEConfig.TRANSIENT_READ_MAX_RETRIES,
            )
            _sleep(iface._retry_policy_get_delay(transient_policy, attempt_index))
            return
        iface._read_retry_count = 0
        raise iface.BLEError(ERROR_READING_BLE) from error

    def log_empty_read_warning(self) -> None:
        """Emit empty-read warning for the bound interface."""
        iface = self._iface
        override_log_empty_read_warning = iface.__dict__.get("_log_empty_read_warning")
        if callable(override_log_empty_read_warning):
            override_log_empty_read_warning()
            return
        now = time.monotonic()
        cooldown = BLEConfig.EMPTY_READ_WARNING_COOLDOWN
        if now - iface._last_empty_read_warning >= cooldown:
            suppressed = iface._suppressed_empty_read_warnings
            message = f"Exceeded max retries for empty BLE read from {FROMRADIO_UUID}"
            if suppressed:
                message = (
                    f"{message} (suppressed {suppressed} repeats in the last "
                    f"{cooldown:.0f}s)"
                )
            logger.warning(message)
            iface._last_empty_read_warning = now
            iface._suppressed_empty_read_warnings = 0
            return

        iface._suppressed_empty_read_warnings += 1
        logger.debug(
            "Suppressed repeated empty BLE read warning (%d within %.0fs window)",
            iface._suppressed_empty_read_warnings,
            cooldown,
        )

class BLEReceiveRecoveryService:
    """Service helpers for BLE receive-loop and recovery behavior."""

    @staticmethod
    def _handle_read_loop_disconnect(
        iface: "BLEInterface", error_message: str, previous_client: BLEClient
    ) -> bool:
        """Handle a read-loop disconnect and return continue/stop decision.

        Parameters
        ----------
        iface : BLEInterface
            Interface whose disconnect handling should be invoked.
        error_message : str
            Disconnect context string included in the source label.
        previous_client : BLEClient
            Client instance that triggered the disconnect path.

        Returns
        -------
        bool
            ``True`` when receive-loop processing should continue, otherwise
            ``False``.
        """
        return BLEReceiveRecoveryController(iface).handle_read_loop_disconnect(
            error_message,
            previous_client,
        )

    @staticmethod
    def _should_poll_without_notify(iface: "BLEInterface") -> bool:
        """Return whether fallback polling is allowed without notify callbacks.

        Parameters
        ----------
        iface : BLEInterface
            Interface providing notification-configuration state.

        Returns
        -------
        bool
            ``True`` when polling without notify callbacks should proceed,
            otherwise ``False``.
        """
        with iface._state_lock:
            return not iface._fromnum_notify_enabled

    @staticmethod
    def _coordinator_wait_for_event(
        coordinator: "ThreadCoordinator", event_name: str, *, timeout: float | None
    ) -> bool:
        """Wait for a coordinator event using compatibility dispatch.

        Parameters
        ----------
        coordinator : ThreadCoordinator
            Coordinator instance exposing public or underscore wait helpers.
        event_name : str
            Event name to wait on.
        timeout : float | None
            Maximum wait time in seconds. ``None`` waits indefinitely when the
            coordinator exposes ``wait_for_event``/``_wait_for_event``; when no
            wait hook is available, fallback behavior sleeps briefly using
            ``_sleep(COORDINATOR_WAIT_FALLBACK_SLEEP_SEC)`` and returns.

        Returns
        -------
        bool
            ``True`` when the event was signaled, otherwise ``False``.
        """
        wait_for_event = getattr(coordinator, "wait_for_event", None)
        if callable(wait_for_event) and not _is_unconfigured_mock_callable(
            wait_for_event
        ):
            return bool(wait_for_event(event_name, timeout=timeout))
        legacy_wait_for_event = getattr(coordinator, "_wait_for_event", None)
        if callable(legacy_wait_for_event) and not _is_unconfigured_mock_callable(
            legacy_wait_for_event
        ):
            return bool(legacy_wait_for_event(event_name, timeout=timeout))
        if timeout is None:
            _sleep(COORDINATOR_WAIT_FALLBACK_SLEEP_SEC)
        elif timeout > 0:
            _sleep(min(timeout, COORDINATOR_WAIT_FALLBACK_SLEEP_SEC))
        return False

    @staticmethod
    def _coordinator_check_and_clear_event(
        coordinator: "ThreadCoordinator", event_name: str
    ) -> bool:
        """Check and clear a coordinator event using compatibility dispatch.

        Parameters
        ----------
        coordinator : ThreadCoordinator
            Coordinator instance exposing public or underscore clear helpers.
        event_name : str
            Event name to check and clear.

        Returns
        -------
        bool
            ``True`` when the event was set before clearing, otherwise
            ``False``.
        """
        check_and_clear_event = getattr(coordinator, "check_and_clear_event", None)
        if callable(check_and_clear_event) and not _is_unconfigured_mock_callable(
            check_and_clear_event
        ):
            return bool(check_and_clear_event(event_name))
        legacy_check_and_clear_event = getattr(
            coordinator, "_check_and_clear_event", None
        )
        if callable(
            legacy_check_and_clear_event
        ) and not _is_unconfigured_mock_callable(legacy_check_and_clear_event):
            return bool(legacy_check_and_clear_event(event_name))
        return False

    @staticmethod
    def _coordinator_clear_event(
        coordinator: "ThreadCoordinator", event_name: str
    ) -> None:
        """Clear a coordinator event using compatibility dispatch.

        Parameters
        ----------
        coordinator : ThreadCoordinator
            Coordinator instance exposing public or underscore clear helpers.
        event_name : str
            Event name to clear.

        Returns
        -------
        None
            Returns ``None`` after best-effort event clearing.
        """
        clear_events = getattr(coordinator, "clear_events", None)
        if callable(clear_events) and not _is_unconfigured_mock_callable(clear_events):
            clear_events(event_name)
            return
        legacy_clear_events = getattr(coordinator, "_clear_events", None)
        if callable(legacy_clear_events) and not _is_unconfigured_mock_callable(
            legacy_clear_events
        ):
            legacy_clear_events(event_name)
            return
        clear_event = getattr(coordinator, "clear_event", None)
        if callable(clear_event) and not _is_unconfigured_mock_callable(clear_event):
            clear_event(event_name)
            return
        legacy_clear_event = getattr(coordinator, "_clear_event", None)
        if callable(legacy_clear_event) and not _is_unconfigured_mock_callable(
            legacy_clear_event
        ):
            legacy_clear_event(event_name)

    @staticmethod
    def _wait_for_read_trigger(
        iface: "BLEInterface",
        *,
        coordinator: "ThreadCoordinator",
        wait_timeout: float,
    ) -> tuple[bool, bool]:
        """Wait for read trigger and compute fallback poll mode.

        Parameters
        ----------
        iface : BLEInterface
            Interface providing reconnect and notification state.
        coordinator : ThreadCoordinator
            Coordinator used for event waits and clear operations.
        wait_timeout : float
            Maximum wait time for read-trigger and reconnect events.

        Returns
        -------
        tuple[bool, bool]
            ``(proceed, poll_without_notify)`` where ``proceed`` indicates
            whether the caller should continue the loop iteration.
        """
        return BLEReceiveRecoveryController(iface)._wait_for_read_trigger(
            coordinator=coordinator,
            wait_timeout=wait_timeout,
        )

    @staticmethod
    def _snapshot_client_state(
        iface: "BLEInterface",
    ) -> tuple[BLEClient | None, bool, bool, bool]:
        """Snapshot client and gating flags needed by the read loop.

        Parameters
        ----------
        iface : BLEInterface
            Interface providing state guarded by ``_state_lock``.

        Returns
        -------
        tuple[BLEClient | None, bool, bool, bool]
            ``(client, is_connecting, publish_pending, is_closing)``.
        """
        return BLEReceiveRecoveryController(iface)._snapshot_client_state()

    @staticmethod
    def _process_client_state(
        iface: "BLEInterface",
        *,
        coordinator: "ThreadCoordinator",
        wait_timeout: float,
        client: BLEClient | None,
        is_connecting: bool,
        publish_pending: bool,
        is_closing: bool,
    ) -> bool:
        """Process current client state and decide whether to break loop.

        Parameters
        ----------
        iface : BLEInterface
            Interface providing reconnect and lifecycle helpers.
        coordinator : ThreadCoordinator
            Coordinator used for reconnect wait signaling.
        wait_timeout : float
            Wait timeout passed to coordinator event waits.
        client : BLEClient | None
            Current active BLE client snapshot.
        is_connecting : bool
            Whether connection establishment is currently in progress.
        publish_pending : bool
            Whether connect publication is pending verification.
        is_closing : bool
            Whether the interface is closing.

        Returns
        -------
        bool
            ``True`` when the caller should break out of the inner receive
            loop, otherwise ``False``.
        """
        return BLEReceiveRecoveryController(iface)._process_client_state(
            coordinator=coordinator,
            wait_timeout=wait_timeout,
            client=client,
            is_connecting=is_connecting,
            publish_pending=publish_pending,
            is_closing=is_closing,
        )

    @staticmethod
    def _reset_recovery_after_stability(iface: "BLEInterface") -> None:
        """Reset recovery-attempt counter after sustained stable reads.

        Parameters
        ----------
        iface : BLEInterface
            Interface maintaining receive-recovery counters.

        Returns
        -------
        None
            Returns ``None`` after best-effort counter reset.
        """
        BLEReceiveRecoveryController(iface)._reset_recovery_after_stability()

    @staticmethod
    def _read_and_handle_payload(
        iface: "BLEInterface",
        client: BLEClient,
        *,
        poll_without_notify: bool,
    ) -> bool:
        """Read a payload iteration and dispatch packet handling.

        Parameters
        ----------
        iface : BLEInterface
            Interface providing read/retry helpers and packet handlers.
        client : BLEClient
            Active BLE client used for characteristic reads.
        poll_without_notify : bool
            Whether the current cycle is using fallback polling mode.

        Returns
        -------
        bool
            ``True`` to continue the inner loop, ``False`` to stop.
        """
        payload = iface._read_from_radio_with_retries(
            client,
            retry_on_empty=not poll_without_notify,
        )
        if not payload:
            iface._read_retry_count = 0
            return False
        logger.debug("FROMRADIO read: %s", payload.hex())
        try:
            iface._handle_from_radio(payload)
        except DecodeError as exc:
            logger.warning("Failed to parse FromRadio packet, discarding: %s", exc)
            iface._read_retry_count = 0
            return True
        BLEReceiveRecoveryService._reset_recovery_after_stability(iface)
        iface._read_retry_count = 0
        return True

    @staticmethod
    def _handle_payload_read(
        iface: "BLEInterface",
        client: BLEClient,
        *,
        poll_without_notify: bool,
    ) -> tuple[bool, bool]:
        """Handle one payload-read attempt and classify loop control flow.

        Parameters
        ----------
        iface : BLEInterface
            Interface providing payload handlers and retry/disconnect helpers.
        client : BLEClient
            Active BLE client used for current read iteration.
        poll_without_notify : bool
            Whether the current cycle is running in polling fallback mode.

        Returns
        -------
        tuple[bool, bool]
            ``(break_inner_loop, stop_receive_loop)``.

        Raises
        ------
        SystemExit
            Propagated when process termination is requested.
        KeyboardInterrupt
            Propagated when process termination is requested.
        """
        try:
            if not BLEReceiveRecoveryService._read_and_handle_payload(
                iface,
                client,
                poll_without_notify=poll_without_notify,
            ):
                return True, False
            return False, False
        except BleakDBusError as exc:
            if iface._handle_read_loop_disconnect(repr(exc), client):
                return True, False
            return True, True
        except (SystemExit, KeyboardInterrupt):  # pylint: disable=W0706
            raise
        except (BleakError, BLEClient.BLEError) as exc:
            try:
                iface._handle_transient_read_error(exc)
                return False, False
            except iface.BLEError:
                logger.error("Fatal BLE read error after retries: %s", exc)
                if not iface._is_connection_closing:
                    iface.close()
                return True, True
        except (RuntimeError, OSError) as exc:
            logger.error("Fatal error in BLE receive thread: %s", exc)
            if not iface._is_connection_closing:
                iface.close()
            return True, True
        except Exception as exc:  # noqa: BLE001  # pragma: no cover
            logger.exception("Unexpected error in BLE read loop")
            if iface._handle_read_loop_disconnect(repr(exc), client):
                return True, False
            return True, True

    @staticmethod
    def _run_receive_cycle(
        iface: "BLEInterface",
        *,
        coordinator: "ThreadCoordinator",
        wait_timeout: float,
    ) -> bool:
        """Run receive-loop orchestration until shutdown or fatal stop.

        Parameters
        ----------
        iface : BLEInterface
            Interface whose receive loop is being executed.
        coordinator : ThreadCoordinator
            Coordinator used for trigger/reconnect event waits.
        wait_timeout : float
            Wait timeout passed to coordinator waits for each cycle.

        Returns
        -------
        bool
            ``True`` when the loop exited naturally, ``False`` when a fatal
            read path requested immediate receive-loop stop.
        """
        while iface._should_run_receive_loop():
            proceed, poll_without_notify = (
                BLEReceiveRecoveryService._wait_for_read_trigger(
                    iface,
                    coordinator=coordinator,
                    wait_timeout=wait_timeout,
                )
            )
            if not proceed:
                continue

            while iface._should_run_receive_loop():
                client, is_connecting, publish_pending, is_closing = (
                    BLEReceiveRecoveryService._snapshot_client_state(iface)
                )
                if BLEReceiveRecoveryService._process_client_state(
                    iface,
                    coordinator=coordinator,
                    wait_timeout=wait_timeout,
                    client=client,
                    is_connecting=is_connecting,
                    publish_pending=publish_pending,
                    is_closing=is_closing,
                ):
                    break

                if client is None:  # pragma: no cover
                    break

                break_inner_loop, stop_receive_loop = (
                    BLEReceiveRecoveryService._handle_payload_read(
                        iface,
                        client,
                        poll_without_notify=poll_without_notify,
                    )
                )
                if stop_receive_loop:
                    return False
                if break_inner_loop:
                    break
        return True

    @staticmethod
    def _receive_from_radio_impl(iface: "BLEInterface") -> None:
        """Run the BLE receive loop and dispatch decoded radio payloads.

        Parameters
        ----------
        iface : BLEInterface
            Interface providing receive-loop state and handlers.

        Returns
        -------
        None
            Returns ``None``. Fatal receive-loop failures trigger recovery or
            close side effects before exiting.
        """
        coordinator = iface.thread_coordinator
        wait_timeout = BLEConfig.RECEIVE_WAIT_TIMEOUT
        try:
            should_continue = BLEReceiveRecoveryService._run_receive_cycle(
                iface,
                coordinator=coordinator,
                wait_timeout=wait_timeout,
            )
            if not should_continue:
                return
        except (SystemExit, KeyboardInterrupt):  # pylint: disable=W0706
            raise
        except (
            BleakDBusError,
            BLEClient.BLEError,
            BleakError,
            DecodeError,
            RuntimeError,
            OSError,
        ):
            logger.exception("Fatal error in BLE receive thread")
            iface._recover_receive_thread(RECEIVE_THREAD_FATAL_REASON)
        except Exception:  # noqa: BLE001
            logger.exception("Unexpected fatal error in BLE receive thread")
            iface._recover_receive_thread(RECEIVE_THREAD_FATAL_REASON)

    @staticmethod
    def _recover_receive_thread(iface: "BLEInterface", disconnect_reason: str) -> None:
        """Handle receive-thread crash and guarded restart behavior.

        Parameters
        ----------
        iface : BLEInterface
            Interface providing disconnect and receive-thread lifecycle helpers.
        disconnect_reason : str
            Reason label passed into disconnect handling.

        Returns
        -------
        None
            Returns ``None`` after scheduling recovery or stopping receive.
        """
        BLEReceiveRecoveryController(iface).recover_receive_thread(disconnect_reason)

    @staticmethod
    def _read_from_radio_with_retries(
        iface: "BLEInterface",
        client: BLEClient,
        *,
        retry_on_empty: bool = True,
    ) -> bytes | None:
        """Read non-empty payload from FROMRADIO with retry policy.

        Parameters
        ----------
        iface : BLEInterface
            Interface providing retry policies and warning state.
        client : BLEClient
            Active BLE client used for characteristic reads.
        retry_on_empty : bool
            When ``True``, apply empty-read retry/backoff behavior.

        Returns
        -------
        bytes | None
            Non-empty payload bytes when available, otherwise ``None``.
        """
        max_retries = BLEConfig.EMPTY_READ_MAX_RETRIES if retry_on_empty else 0
        read_timeout = (
            GATT_IO_TIMEOUT if retry_on_empty else BLEConfig.RECEIVE_WAIT_TIMEOUT
        )
        for attempt in range(max_retries + 1):
            payload = client.read_gatt_char(FROMRADIO_UUID, timeout=read_timeout)
            if payload:
                iface._suppressed_empty_read_warnings = 0
                return payload
            if attempt < max_retries:
                _sleep(iface._retry_policy_get_delay(iface._empty_read_policy, attempt))
        if retry_on_empty:
            iface._log_empty_read_warning()
        return None

    @staticmethod
    def _handle_transient_read_error(
        iface: "BLEInterface", error: BleakError | BLEClient.BLEError
    ) -> None:
        """Apply transient-read retry policy and raise on exhaustion.

        Parameters
        ----------
        iface : BLEInterface
            Interface providing transient retry counters and policy helpers.
        error : BleakError | BLEClient.BLEError
            Error that triggered retry-policy evaluation.

        Returns
        -------
        None
            Returns ``None`` when retry is scheduled.

        Raises
        ------
        BLEError
            If transient retry attempts are exhausted.
        """
        transient_policy = iface._transient_read_policy
        if iface._retry_policy_should_retry(transient_policy, iface._read_retry_count):
            attempt_index = iface._read_retry_count
            iface._read_retry_count += 1
            logger.debug(
                "Transient BLE read error, retrying (%d/%d)",
                iface._read_retry_count,
                BLEConfig.TRANSIENT_READ_MAX_RETRIES,
            )
            _sleep(iface._retry_policy_get_delay(transient_policy, attempt_index))
            return
        iface._read_retry_count = 0
        raise iface.BLEError(ERROR_READING_BLE) from error

    @staticmethod
    def _log_empty_read_warning(iface: "BLEInterface") -> None:
        """Emit throttled warning on repeated empty FROMRADIO reads.

        Parameters
        ----------
        iface : BLEInterface
            Interface storing empty-read warning cooldown state.

        Returns
        -------
        None
            Returns ``None`` after logging or suppressing warning output.
        """
        now = time.monotonic()
        cooldown = BLEConfig.EMPTY_READ_WARNING_COOLDOWN
        if now - iface._last_empty_read_warning >= cooldown:
            suppressed = iface._suppressed_empty_read_warnings
            message = f"Exceeded max retries for empty BLE read from {FROMRADIO_UUID}"
            if suppressed:
                message = (
                    f"{message} (suppressed {suppressed} repeats in the last "
                    f"{cooldown:.0f}s)"
                )
            logger.warning(message)
            iface._last_empty_read_warning = now
            iface._suppressed_empty_read_warnings = 0
            return

        iface._suppressed_empty_read_warnings += 1
        logger.debug(
            "Suppressed repeated empty BLE read warning (%d within %.0fs window)",
            iface._suppressed_empty_read_warnings,
            cooldown,
        )
