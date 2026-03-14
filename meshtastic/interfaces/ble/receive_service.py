"""Receive-loop and recovery helpers for BLE interface orchestration."""

import time
from typing import TYPE_CHECKING

from bleak.exc import BleakDBusError, BleakError

from meshtastic.interfaces.ble.client import BLEClient
from meshtastic.interfaces.ble.constants import (
    ERROR_READING_BLE,
    FROMRADIO_UUID,
    GATT_IO_TIMEOUT,
    RECEIVE_RECOVERY_MAX_BACKOFF_SEC,
    RECEIVE_RECOVERY_RAPID_FAILURE_THRESHOLD,
    RECEIVE_RECOVERY_STABILITY_RESET_SEC,
    BLEConfig,
    logger,
)
from meshtastic.interfaces.ble.errors import DecodeError
from meshtastic.interfaces.ble.state import ConnectionState
from meshtastic.interfaces.ble.utils import _sleep

if TYPE_CHECKING:
    from meshtastic.interfaces.ble.interface import BLEInterface


class BLEReceiveRecoveryService:
    """Service helpers for BLE receive-loop and recovery behavior."""

    @staticmethod
    def handle_read_loop_disconnect(
        iface: "BLEInterface", error_message: str, previous_client: BLEClient
    ) -> bool:
        """Return whether receive loop should continue after a disconnect."""
        logger.debug("Device disconnected: %s", error_message)
        should_continue = iface._handle_disconnect(
            f"read_loop: {error_message}", client=previous_client
        )
        if not should_continue:
            iface._set_receive_wanted(False)
        return should_continue

    @staticmethod
    def receive_from_radio_impl(iface: "BLEInterface") -> None:
        """Run receive loop and dispatch radio packets."""
        coordinator = iface.thread_coordinator
        wait_timeout = BLEConfig.RECEIVE_WAIT_TIMEOUT
        try:
            while iface._should_run_receive_loop():
                event_signaled = coordinator.wait_for_event(
                    "read_trigger", timeout=wait_timeout
                )
                poll_without_notify = False
                if not event_signaled:
                    if iface._ever_connected and coordinator.check_and_clear_event(
                        "reconnected_event"
                    ):
                        logger.debug("Detected reconnection, resuming normal operation")
                    with iface._state_lock:
                        poll_without_notify = not iface._fromnum_notify_enabled
                    if not poll_without_notify:
                        continue
                else:
                    coordinator.clear_event("read_trigger")

                while iface._should_run_receive_loop():
                    with iface._state_lock:
                        client = iface.client
                        is_connecting = (
                            iface._state_manager.current_state
                            == ConnectionState.CONNECTING
                        )
                        publish_pending = iface._client_publish_pending
                        is_closing = iface._state_manager.is_closing or iface._closed
                    if publish_pending:
                        logger.debug(
                            "Skipping BLE read while connect publication is pending verification."
                        )
                        coordinator.wait_for_event(
                            "reconnected_event",
                            timeout=wait_timeout,
                        )
                        break
                    if client is None:
                        if iface.auto_reconnect or is_connecting:
                            wait_reason = (
                                "connection establishment"
                                if is_connecting
                                else "auto-reconnect"
                            )
                            logger.debug(
                                "BLE client is None; waiting for %s",
                                wait_reason,
                            )
                            coordinator.wait_for_event(
                                "reconnected_event",
                                timeout=wait_timeout,
                            )
                            break
                        if is_closing:
                            logger.debug("BLE client is None, shutting down")
                            iface._set_receive_wanted(False)
                        else:
                            logger.debug(
                                "BLE client is None; re-checking connection state"
                            )
                        break
                    try:
                        payload = iface._read_from_radio_with_retries(
                            client,
                            retry_on_empty=not poll_without_notify,
                        )
                        if not payload:
                            break
                        logger.debug("FROMRADIO read: %s", payload.hex())
                        try:
                            iface._handle_from_radio(payload)
                        except DecodeError as exc:
                            logger.warning(
                                "Failed to parse FromRadio packet, discarding: %s", exc
                            )
                            iface._read_retry_count = 0
                            continue
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
                        iface._read_retry_count = 0
                    except (BleakDBusError, BLEClient.BLEError) as exc:
                        if iface._handle_read_loop_disconnect(repr(exc), client):
                            break
                        return
                    except (SystemExit, KeyboardInterrupt):  # pylint: disable=W0706
                        raise
                    except BleakError as exc:
                        try:
                            iface._handle_transient_read_error(exc)
                            continue
                        except iface.BLEError:
                            logger.error(
                                "Fatal BLE read error after retries: %s", exc
                            )
                            if not iface._is_connection_closing:
                                iface.close()
                            return
                    except (RuntimeError, OSError) as exc:
                        logger.error("Fatal error in BLE receive thread: %s", exc)
                        if not iface._is_connection_closing:
                            iface.close()
                        return
                    except Exception as exc:  # noqa: BLE001  # pragma: no cover
                        logger.exception("Unexpected error in BLE read loop")
                        if iface._handle_read_loop_disconnect(repr(exc), client):
                            break
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
            iface._recover_receive_thread("receive_thread_fatal")
        except Exception:  # noqa: BLE001
            logger.exception("Unexpected fatal error in BLE receive thread")
            iface._recover_receive_thread("receive_thread_fatal")

    @staticmethod
    def recover_receive_thread(iface: "BLEInterface", disconnect_reason: str) -> None:
        """Handle receive-thread crash and guarded recovery."""
        if iface._is_connection_closing:
            return
        with iface._state_lock:
            current_client = iface.client
        should_continue = iface._handle_disconnect(
            disconnect_reason, client=current_client
        )
        if not should_continue:
            iface._set_receive_wanted(False)
            return
        now = time.monotonic()
        with iface._state_lock:
            iface._receive_recovery_attempts += 1
            attempts = iface._receive_recovery_attempts
            last_recovery = iface._last_recovery_time
        if attempts > RECEIVE_RECOVERY_RAPID_FAILURE_THRESHOLD:
            backoff = min(
                2 ** (attempts - RECEIVE_RECOVERY_RAPID_FAILURE_THRESHOLD),
                RECEIVE_RECOVERY_MAX_BACKOFF_SEC,
            )
            if now - last_recovery < backoff:
                logger.warning(
                    "Throttling BLE receive recovery: waiting %.1fs before retry (attempt %d)",
                    backoff,
                    attempts,
                )
                iface._shutdown_event.wait(timeout=backoff)
        with iface._state_lock:
            iface._last_recovery_time = time.monotonic()
        if iface._should_run_receive_loop():
            iface._start_receive_thread(name="BLEReceiveRecovery", reset_recovery=False)

    @staticmethod
    def read_from_radio_with_retries(
        iface: "BLEInterface",
        client: BLEClient,
        *,
        retry_on_empty: bool = True,
    ) -> bytes | None:
        """Read non-empty payload from FROMRADIO characteristic with retry policy."""
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
    def handle_transient_read_error(iface: "BLEInterface", error: BleakError) -> None:
        """Apply transient read retry policy and raise on exhaustion."""
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
    def log_empty_read_warning(iface: "BLEInterface") -> None:
        """Emit throttled warning on repeated empty FROMRADIO reads."""
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
