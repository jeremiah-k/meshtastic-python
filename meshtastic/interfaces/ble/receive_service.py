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
        logger.debug("Device disconnected: %s", error_message)
        should_continue = iface._handle_disconnect(
            f"read_loop: {error_message}", client=previous_client
        )
        iface._read_retry_count = 0
        if not should_continue:
            iface._set_receive_wanted(want_receive=False)
        return should_continue

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
        event_signaled = BLEReceiveRecoveryService._coordinator_wait_for_event(
            coordinator,
            READ_TRIGGER_EVENT,
            timeout=wait_timeout,
        )
        poll_without_notify = False
        raw_ever_connected = getattr(iface, "_ever_connected", False)
        ever_connected = (
            False
            if _is_unconfigured_mock_member(raw_ever_connected)
            else raw_ever_connected if isinstance(raw_ever_connected, bool) else False
        )
        if not event_signaled:
            if (
                ever_connected
                and BLEReceiveRecoveryService._coordinator_check_and_clear_event(
                    coordinator,
                    RECONNECTED_EVENT,
                )
            ):
                logger.debug("Detected reconnection, resuming normal operation")
            poll_without_notify = BLEReceiveRecoveryService._should_poll_without_notify(
                iface
            )
            # Proceed only when fallback polling is viable without notify callbacks.
            return poll_without_notify, poll_without_notify

        BLEReceiveRecoveryService._coordinator_clear_event(
            coordinator, READ_TRIGGER_EVENT
        )
        return True, poll_without_notify

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
        from meshtastic.interfaces.ble.lifecycle_service import BLELifecycleService

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
            elif not _is_unconfigured_mock_member(
                state_is_connecting
            ) and isinstance(state_is_connecting, bool):
                is_connecting = state_is_connecting
            else:
                legacy_is_connecting = getattr(iface._state_manager, "_is_connecting", None)
                if callable(legacy_is_connecting) and not _is_unconfigured_mock_callable(
                    legacy_is_connecting
                ):
                    connecting_result = legacy_is_connecting()
                    is_connecting = (
                        connecting_result
                        if isinstance(connecting_result, bool)
                        else False
                    )
                elif not _is_unconfigured_mock_member(
                    legacy_is_connecting
                ) and isinstance(legacy_is_connecting, bool):
                    is_connecting = legacy_is_connecting
                else:
                    is_connecting = False
            publish_pending = iface._client_publish_pending
            state_is_closing = BLELifecycleService._state_manager_is_closing(iface)
            is_closing = state_is_closing or iface._closed
        return client, is_connecting, publish_pending, is_closing

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
        if client is None and is_closing:
            logger.debug("BLE client is None, shutting down")
            iface._set_receive_wanted(want_receive=False)
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
            wait_reason = (
                "connection establishment" if is_connecting else "auto-reconnect"
            )
            logger.debug("BLE client is None; waiting for %s", wait_reason)
            BLEReceiveRecoveryService._coordinator_wait_for_event(
                coordinator,
                RECONNECTED_EVENT,
                timeout=wait_timeout,
            )
            return True

        logger.debug("BLE client is None; re-checking connection state")
        return True

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
        if iface._is_connection_closing:
            return
        with iface._state_lock:
            current_client = iface.client
        should_continue = iface._handle_disconnect(
            disconnect_reason, client=current_client
        )
        if not should_continue:
            iface._set_receive_wanted(want_receive=False)
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
        should_restart = iface._should_run_receive_loop()
        logger.debug(
            "BLE receive recovery restart decision: attempts=%d should_restart=%s reset_recovery=%s",
            attempts,
            should_restart,
            False,
        )
        if should_restart:
            try:
                iface._start_receive_thread(
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
        logger.debug("Persistent BLE read error after retries", exc_info=True)
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
