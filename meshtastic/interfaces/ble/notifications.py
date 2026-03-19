"""BLE notification management."""

import contextlib
import logging
import re
import struct
from collections.abc import Callable
from threading import RLock
from typing import TYPE_CHECKING, Any, cast

from bleak.exc import BleakDBusError, BleakError

from meshtastic.interfaces.ble.client import BLEClient
from meshtastic.interfaces.ble.constants import (
    BLECLIENT_ERROR_SUBSCRIPTION_TOKEN_EXHAUSTED,
    FROMNUM_UUID,
    LEGACY_LOGRADIO_UUID,
    LOGRADIO_UUID,
    MALFORMED_NOTIFICATION_THRESHOLD,
    NOTIFICATION_START_TIMEOUT,
    BLEConfig,
)
from meshtastic.interfaces.ble.errors import DecodeError
from meshtastic.interfaces.ble.utils import (
    _is_unconfigured_mock_callable,
    _is_unconfigured_mock_member,
    _is_unexpected_keyword_error,
    _sleep,
)
from meshtastic.protobuf import mesh_pb2

if TYPE_CHECKING:
    from meshtastic.interfaces.ble.interface import BLEInterface

logger = logging.getLogger("meshtastic.ble")
UNSUBSCRIBE_FAILURE_WARNING_THRESHOLD = 3
RESUBSCRIBE_FAILURE_WARNING_THRESHOLD = 3
_NOTIFY_ACQUIRED_FRAGMENT = "notify acquired"
_SAFE_EXECUTE_POSITIONAL_SIGNATURE_MISMATCH_RE = re.compile(
    r"positional.*argument|takes .* positional|missing required positional|were given|was given",
    re.IGNORECASE,
)


class SubscriptionTokenExhaustedError(RuntimeError):
    """Raised when notification subscription token space is exhausted."""


class NotificationManager:
    """Manage BLE notification subscriptions so we can resubscribe cleanly after reconnects."""

    _MAX_SUBSCRIPTION_TOKEN = 0x7FFFFFFF

    def __init__(self) -> None:
        """Initialize a NotificationManager and its thread-safe subscription state.

        Initializes the following internal attributes used for tracking subscriptions:
        - _active_subscriptions: maps a unique subscription token to a (characteristic, callback) pair.
        - _characteristic_to_callback: maps a characteristic UUID to the most recently registered callback.
        - _subscription_counter: monotonic counter used to allocate unique subscription tokens.
        - _lock: re-entrant lock protecting access to subscription state.

        Returns
        -------
        None
        """
        self._active_subscriptions: dict[
            int, tuple[str, Callable[[Any, Any], None]]
        ] = {}
        self._characteristic_to_callback: dict[str, Callable[[Any, Any], None]] = {}
        self._resubscribe_failures: dict[str, int] = {}
        self._subscription_counter = 0
        self._resubscribe_epoch = 0
        self._lock = RLock()

    def _subscribe(
        self, characteristic: str, callback: Callable[[Any, Any], None]
    ) -> int:
        """Track a notification callback for a BLE characteristic so it can be cleaned up or re-registered later.

        Parameters
        ----------
        characteristic : str
            Identifier of the BLE characteristic (for example, a UUID string or handle).
        callback : Callable[[Any, Any], None]
            Function invoked when a notification arrives; typically called as (sender, data).

        Returns
        -------
        token : int
            Unique opaque token that identifies the tracked subscription.

        Notes
        -----
        Multiple subscriptions to the same characteristic are allowed; the most recently
        registered callback for a characteristic is returned by `_get_callback()`, while
        all tracked subscriptions are retained for cleanup or resubscription attempts.
        """
        with self._lock:
            token = self._subscription_counter
            # Wrap at 2 billion to prevent unbounded growth in long-running processes
            self._subscription_counter = (
                self._subscription_counter + 1
            ) & self._MAX_SUBSCRIPTION_TOKEN
            # Detect a full cycle through the token space instead of using a fixed
            # iteration cap, which can raise prematurely under wraparound collisions.
            start_token = token
            while token in self._active_subscriptions:
                token = self._subscription_counter
                self._subscription_counter = (
                    self._subscription_counter + 1
                ) & self._MAX_SUBSCRIPTION_TOKEN
                if token == start_token:
                    raise SubscriptionTokenExhaustedError(
                        BLECLIENT_ERROR_SUBSCRIPTION_TOKEN_EXHAUSTED
                    )
            self._active_subscriptions[token] = (characteristic, callback)
            self._characteristic_to_callback[characteristic] = callback
            return token

    def _cleanup_all(self) -> None:
        """Clear all tracked BLE notification subscriptions and per-characteristic callbacks.

        Removes every active subscription entry, clears the characteristic-to-callback mapping, and resets the subscription token counter to zero. This operation is performed while holding the manager's internal lock.

        Returns
        -------
        None
        """
        with self._lock:
            self._resubscribe_epoch += 1
            self._active_subscriptions.clear()
            self._characteristic_to_callback.clear()
            self._resubscribe_failures.clear()
            self._subscription_counter = 0

    def _unsubscribe_all(self, client: "BLEClient", *, timeout: float | None) -> None:
        """Stop notifications for every characteristic currently tracked and ignore any errors.

        Parameters
        ----------
        client : 'BLEClient'
            BLE client used to stop notifications.
        timeout : float | None
            Per-unsubscribe timeout passed to the client's `stop_notify` method; may be None.

        Returns
        -------
        None
        """
        with self._lock:
            characteristics = list(self._characteristic_to_callback.keys())

        failure_count = 0
        for characteristic in characteristics:
            try:
                client.stop_notify(characteristic, timeout=timeout)
            except Exception as e:  # noqa: BLE001
                failure_count += 1
                logger.debug(
                    "Failed to unsubscribe %s during shutdown: %s",
                    characteristic,
                    e,
                )
        if failure_count >= UNSUBSCRIBE_FAILURE_WARNING_THRESHOLD:
            logger.warning(
                "Failed to unsubscribe %d/%d BLE characteristic notifications during shutdown.",
                failure_count,
                len(characteristics),
            )

    def _resubscribe_all(self, client: "BLEClient", *, timeout: float | None) -> None:
        """Resubscribe all tracked BLE notification callbacks on the given client.

        Uses the per-characteristic latest callback to avoid duplicate resubscription attempts when
        multiple subscriptions were registered for the same characteristic.

        Parameters
        ----------
        client : 'BLEClient'
            BLE client on which to call `start_notify` for each characteristic.
        timeout : float | None
            Per-subscription timeout to pass to the client's `start_notify` method.

        Returns
        -------
        None
        """
        with self._lock:
            # Use _characteristic_to_callback to deduplicate - it holds only the latest
            # callback per characteristic, avoiding redundant resubscription attempts
            subscriptions = list(self._characteristic_to_callback.items())
            failures = self._resubscribe_failures
            epoch = self._resubscribe_epoch
        max_failure_threshold = RESUBSCRIBE_FAILURE_WARNING_THRESHOLD

        for characteristic, callback in subscriptions:
            with self._lock:
                if epoch != self._resubscribe_epoch:
                    return
            if characteristic == FROMNUM_UUID:
                # FROMNUM startup requires dispatcher-owned retry/fallback logic
                # and fromnum_notify_enabled synchronization.
                continue
            try:
                client.start_notify(
                    characteristic,
                    callback,
                    timeout=timeout,
                )
            except Exception as e:  # noqa: BLE001
                with self._lock:
                    if epoch != self._resubscribe_epoch:
                        return
                    failure_count = failures.get(characteristic, 0) + 1
                    failures[characteristic] = failure_count
                logger.debug(
                    "Failed to resubscribe %s during reconnect: %s",
                    characteristic,
                    e,
                )
                if failure_count >= max_failure_threshold:
                    logger.warning(
                        "Repeated BLE resubscribe failures for %s (%d attempts): %s",
                        characteristic,
                        failure_count,
                        e,
                    )
            else:
                with self._lock:
                    if epoch != self._resubscribe_epoch:
                        return
                    failures.pop(characteristic, None)

    def __len__(self) -> int:
        """Report the number of active BLE notification subscriptions being tracked.

        Returns
        -------
        int
            Number of active subscriptions currently tracked.
        """
        with self._lock:
            return len(self._active_subscriptions)

    def _get_callback(self, characteristic: str) -> Callable[[Any, Any], None] | None:
        """Retrieve the most recently registered callback for a BLE characteristic.

        Parameters
        ----------
        characteristic : str
            BLE characteristic identifier (e.g., UUID or handle) to look up.

        Returns
        -------
        Callable[[Any, Any], None] | None
            The most recently registered callback for the characteristic, or `None` if no callback is registered.
        """
        with self._lock:
            return self._characteristic_to_callback.get(characteristic)


class BLENotificationDispatcher:
    """Own notification callback safety, FROMNUM parsing, and registration flow."""

    def __init__(
        self,
        *,
        notification_manager: NotificationManager,
        error_handler_provider: Callable[[], object],
        trigger_read_event: Callable[[], None],
    ) -> None:
        """Create a notification-dispatch collaborator.

        Parameters
        ----------
        notification_manager : NotificationManager
            Subscription manager used to deduplicate callback registrations.
        error_handler_provider : Callable[[], object]
            Function returning the current interface error-handler object.
        trigger_read_event : Callable[[], None]
            Callback that wakes the receive loop after FROMNUM handling.
        """
        self._notification_manager = notification_manager
        self._error_handler_provider = error_handler_provider
        self._trigger_read_event = trigger_read_event
        self._fromnum_notify_enabled = False
        self._malformed_notification_count = 0
        self._malformed_notification_lock = RLock()
        self._current_legacy_log_handler: Callable[[Any, Any], None] | None = None
        self._current_log_handler: Callable[[Any, Any], None] | None = None
        self._current_from_num_handler: Callable[[Any, Any], None] | None = None
        self._registered_notification_session_epoch: int | None = None
        self._started_notify_characteristics: set[str] = set()

    @property
    def fromnum_notify_enabled(self) -> bool:
        """Return whether FROMNUM notifications are currently active."""
        return self._fromnum_notify_enabled

    @fromnum_notify_enabled.setter
    def fromnum_notify_enabled(self, enabled: bool) -> None:
        """Set FROMNUM notification-active flag."""
        self._fromnum_notify_enabled = enabled

    @property
    def malformed_notification_count(self) -> int:
        """Return current malformed FROMNUM notification counter."""
        with self._malformed_notification_lock:
            return self._malformed_notification_count

    @malformed_notification_count.setter
    def malformed_notification_count(self, value: int) -> None:
        """Set malformed FROMNUM notification counter."""
        with self._malformed_notification_lock:
            self._malformed_notification_count = value

    @property
    def malformed_notification_lock(self) -> RLock:
        """Expose malformed-notification lock for compatibility callers."""
        return self._malformed_notification_lock

    @malformed_notification_lock.setter
    def malformed_notification_lock(self, lock: RLock) -> None:
        """Set malformed-notification lock for compatibility callers."""
        self._malformed_notification_lock = lock

    def handle_malformed_fromnum(self, reason: str, exc_info: bool = False) -> None:
        """Track malformed FROMNUM notifications and emit threshold warnings."""
        with self._malformed_notification_lock:
            self._malformed_notification_count += 1
            logger.debug("%s", reason, exc_info=exc_info)
            if self._malformed_notification_count >= MALFORMED_NOTIFICATION_THRESHOLD:
                logger.warning(
                    "Received %d malformed FROMNUM notifications. Check BLE connection stability.",
                    self._malformed_notification_count,
                )
                self._malformed_notification_count = 0

    def _resolve_error_handler(self) -> object | None:
        """Resolve current error handler, suppressing provider failures.

        Returns
        -------
        object | None
            Resolved error-handler object, or ``None`` when provider lookup
            fails or returns an unconfigured mock placeholder.
        """
        try:
            error_handler = self._error_handler_provider()
        except Exception:  # noqa: BLE001 - notification paths must remain best effort
            logger.debug(
                "Error resolving notification error-handler provider.",
                exc_info=True,
            )
            return None
        if _is_unconfigured_mock_member(error_handler):
            return None
        return error_handler

    def report_notification_handler_error(self, error_msg: str) -> None:
        """Report notification-handler failures through configured hooks."""
        error_handler = self._resolve_error_handler()
        if error_handler is None:
            logger.debug(error_msg)
            return
        report_exception: Callable[[str], Any] | None = None
        for hook_name in ("safe_execute", "_safe_execute"):
            safe_execute_hook = getattr(error_handler, hook_name, None)
            if callable(safe_execute_hook) and not _is_unconfigured_mock_callable(
                safe_execute_hook
            ):
                safe_execute_callable = cast(Callable[..., Any], safe_execute_hook)

                def _report_via_safe_execute(message: str) -> None:
                    def _raise_handler_error() -> None:
                        raise RuntimeError(message)

                    BLENotificationDispatcher.invoke_safe_execute_compat(
                        safe_execute_callable,
                        _raise_handler_error,
                        error_msg=message,
                        fallback=lambda: logger.debug(message),
                    )

                report_exception = _report_via_safe_execute
                break
        for hook_name in (
            "handle_unhandled_exception",
            "_handle_unhandled_exception",
        ):
            if report_exception is not None:
                break
            hook = getattr(error_handler, hook_name, None)
            if callable(hook) and not _is_unconfigured_mock_callable(hook):
                report_exception = hook
                break
        if report_exception is not None:
            try:
                report_exception(error_msg)
            except Exception:  # noqa: BLE001 - callback error reporting is best effort
                logger.debug(error_msg, exc_info=True)
            return
        logger.debug(error_msg)

    @staticmethod
    def invoke_safe_execute_compat(
        safe_execute: Callable[..., Any],
        handler_thunk: Callable[[], None],
        *,
        error_msg: str,
        fallback: Callable[[], None],
        report_handler_error: Callable[[BaseException], None] | None = None,
    ) -> None:
        """Invoke ``safe_execute`` with compatibility signature fallbacks."""
        executed = False
        handler_failed = False
        handler_exception: BaseException | None = None

        def _tracked_handler_thunk() -> None:
            nonlocal executed, handler_failed, handler_exception
            executed = True
            try:
                handler_thunk()
            except (
                Exception
            ) as exc:  # noqa: BLE001 - callback errors are reported below
                handler_failed = True
                handler_exception = exc
                raise

        def _report_handler_failure() -> None:
            if (
                not handler_failed
                or handler_exception is None
                or report_handler_error is None
            ):
                return
            try:
                report_handler_error(handler_exception)
            except Exception:  # noqa: BLE001 - error reporting remains best effort
                logger.debug(
                    "Failed to report notification handler error (%s).",
                    error_msg,
                    exc_info=True,
                )

        def _fallback_if_not_executed() -> None:
            if not executed:
                fallback()

        try:
            safe_execute(_tracked_handler_thunk, error_msg=error_msg)
            if executed:
                _report_handler_failure()
                return
            _fallback_if_not_executed()
            return
        except TypeError as exc:
            if not _is_unexpected_keyword_error(exc, "error_msg"):
                logger.debug(
                    "safe_execute keyword probe raised TypeError for notification handler (%s): %s",
                    error_msg,
                    exc,
                    exc_info=True,
                )
                if executed:
                    _report_handler_failure()
                    return
                _fallback_if_not_executed()
                return
            if executed:
                logger.debug(
                    "safe_execute keyword compatibility probe raised TypeError after handler execution (%s): %s",
                    error_msg,
                    exc,
                    exc_info=True,
                )
                _report_handler_failure()
        except (
            Exception
        ) as exc:  # noqa: BLE001 - notification callbacks must stay best effort
            logger.debug(
                "safe_execute keyword probe failed for notification handler (%s): %s",
                error_msg,
                exc,
                exc_info=True,
            )
            if executed:
                _report_handler_failure()
                return
            _fallback_if_not_executed()
            return

        if executed:
            _report_handler_failure()
            return

        try:
            safe_execute(_tracked_handler_thunk, error_msg)
            if executed:
                _report_handler_failure()
                return
            _fallback_if_not_executed()
            return
        except TypeError as exc:
            if _SAFE_EXECUTE_POSITIONAL_SIGNATURE_MISMATCH_RE.search(str(exc)):
                if executed:
                    logger.debug(
                        "safe_execute positional compatibility probe raised TypeError after handler execution (%s): %s",
                        error_msg,
                        exc,
                        exc_info=True,
                    )
                    _report_handler_failure()
            else:
                logger.debug(
                    "safe_execute positional probe raised TypeError for notification handler (%s); skipping callable-only probe to avoid duplicate handler execution.",
                    error_msg,
                    exc_info=True,
                )
                if executed:
                    _report_handler_failure()
                    return
                _fallback_if_not_executed()
                return
        except (
            Exception
        ) as exc:  # noqa: BLE001 - notification callbacks must stay best effort
            logger.debug(
                "safe_execute positional probe failed for notification handler (%s): %s; skipping callable-only probe to avoid duplicate handler execution.",
                error_msg,
                exc,
                exc_info=True,
            )
            if executed:
                _report_handler_failure()
                return
            _fallback_if_not_executed()
            return

        if executed:
            _report_handler_failure()
            return

        try:
            safe_execute(_tracked_handler_thunk)
            if executed:
                _report_handler_failure()
                return
            _fallback_if_not_executed()
            return
        except (
            Exception
        ) as exc:  # noqa: BLE001 - notification callbacks must stay best effort
            logger.debug(
                "safe_execute callable-only probe failed for notification handler (%s): %s.",
                error_msg,
                exc,
                exc_info=True,
            )
            if executed:
                _report_handler_failure()
                return
            _fallback_if_not_executed()

    def from_num_handler(self, _: Any, b: bytes | bytearray) -> None:
        """Parse FROMNUM payload, reset malformed counter, and wake read loop."""
        try:
            if len(b) != 4:
                self.handle_malformed_fromnum(
                    f"FROMNUM notify has unexpected length {len(b)}; ignoring"
                )
                return
            from_num = struct.unpack("<I", b)[0]
            logger.debug("FROMNUM notify: %d", from_num)
            with self._malformed_notification_lock:
                self._malformed_notification_count = 0
        except (struct.error, ValueError):
            self.handle_malformed_fromnum(
                "Malformed FROMNUM notify; ignoring", exc_info=True
            )
            return
        finally:
            self._trigger_read_event()

    def register_notifications(
        self,
        iface: "BLEInterface",
        client: BLEClient,
        *,
        legacy_log_handler: Callable[[Any, bytes | bytearray], None],
        log_handler: Callable[[Any, bytes | bytearray], None],
        from_num_handler: Callable[[Any, bytes], None],
    ) -> None:
        """Register BLE characteristic notification handlers on ``client``."""
        self._current_legacy_log_handler = legacy_log_handler
        self._current_log_handler = log_handler
        self._current_from_num_handler = from_num_handler
        current_session_epoch = int(getattr(iface, "_connection_session_epoch", 0))
        if self._registered_notification_session_epoch != current_session_epoch:
            self._registered_notification_session_epoch = current_session_epoch
            self._started_notify_characteristics.clear()
            self.fromnum_notify_enabled = False

        def _safe_call(
            handler: Callable[[Any, Any], None],
            sender: Any,
            data: Any,
            error_msg: str,
        ) -> None:
            def _report_notification_error() -> None:
                def _try_report(hook: object | None) -> bool:
                    if not callable(hook) or _is_unconfigured_mock_callable(hook):
                        return False
                    try:
                        hook(error_msg)
                    except Exception:  # noqa: BLE001 - reporting must stay best effort
                        logger.debug(
                            "Notification error reporter failed; trying fallback.",
                            exc_info=True,
                        )
                        return False
                    return True

                report_notification_error = getattr(
                    iface,
                    "report_notification_handler_error",
                    None,
                )
                if _try_report(report_notification_error):
                    return
                legacy_report_notification_error = getattr(
                    iface,
                    "_report_notification_handler_error",
                    None,
                )
                if _try_report(legacy_report_notification_error):
                    return
                self.report_notification_handler_error(error_msg)

            def _invoke_handler() -> None:
                handler(sender, data)

            def _fallback_invoke_handler() -> None:
                try:
                    _invoke_handler()
                except (
                    Exception
                ):  # noqa: BLE001 - notification callbacks must stay best effort
                    _report_notification_error()

            error_handler = self._resolve_error_handler()
            safe_execute = getattr(error_handler, "safe_execute", None)
            if not callable(safe_execute) or _is_unconfigured_mock_callable(
                safe_execute
            ):
                safe_execute = getattr(error_handler, "_safe_execute", None)
            if not callable(safe_execute) or _is_unconfigured_mock_callable(
                safe_execute
            ):
                try:
                    _invoke_handler()
                except (
                    Exception
                ):  # noqa: BLE001 - notification callbacks must stay best effort
                    _report_notification_error()
                return
            self.invoke_safe_execute_compat(
                safe_execute,
                _invoke_handler,
                error_msg=error_msg,
                fallback=_fallback_invoke_handler,
                report_handler_error=lambda _error: _report_notification_error(),
            )

        def _safe_legacy_handler(sender: Any, data: bytes | bytearray) -> None:
            current_legacy_handler = cast(
                Callable[[Any, Any], None],
                getattr(
                    self,
                    "_current_legacy_log_handler",
                    legacy_log_handler,
                ),
            )
            _safe_call(
                current_legacy_handler,
                sender,
                data,
                "Error in legacy log notification handler",
            )

        def _safe_log_handler(sender: Any, data: bytes | bytearray) -> None:
            current_log_handler = cast(
                Callable[[Any, Any], None],
                getattr(
                    self,
                    "_current_log_handler",
                    log_handler,
                ),
            )
            _safe_call(
                current_log_handler,
                sender,
                data,
                "Error in log notification handler",
            )

        def _safe_from_num_handler(sender: Any, data: bytes) -> None:
            current_from_num_handler = cast(
                Callable[[Any, Any], None],
                getattr(
                    self,
                    "_current_from_num_handler",
                    from_num_handler,
                ),
            )
            _safe_call(
                current_from_num_handler,
                sender,
                data,
                "Error in FROMNUM notification handler",
            )

        def _get_or_create_handler(
            uuid: str, factory: Callable[[], Callable[[Any, Any], None]]
        ) -> Callable[[Any, Any], None]:
            handler = self._notification_manager._get_callback(uuid)
            if handler is None:
                handler = factory()
                self._notification_manager._subscribe(uuid, handler)
            return handler

        def _is_notify_acquired_error(err: BaseException) -> bool:
            return _NOTIFY_ACQUIRED_FRAGMENT in str(err).casefold()

        optional_errors = (
            BleakError,
            BleakDBusError,
            RuntimeError,
            BLEClient.BLEError,
            iface.BLEError,
        )

        try:
            has_legacy_logradio = client.has_characteristic(LEGACY_LOGRADIO_UUID)
        except optional_errors as err:
            logger.debug(
                "Failed to start optional legacy log notifications for %s: %s",
                LEGACY_LOGRADIO_UUID,
                err,
            )
        else:
            if has_legacy_logradio:
                if LEGACY_LOGRADIO_UUID in self._started_notify_characteristics:
                    has_legacy_logradio = False
            if has_legacy_logradio:
                legacy_handler = _get_or_create_handler(
                    LEGACY_LOGRADIO_UUID, lambda: _safe_legacy_handler
                )
                try:
                    client.start_notify(
                        LEGACY_LOGRADIO_UUID,
                        legacy_handler,
                        timeout=NOTIFICATION_START_TIMEOUT,
                    )
                except optional_errors as err:
                    logger.debug(
                        "Failed to start optional legacy log notifications for %s: %s",
                        LEGACY_LOGRADIO_UUID,
                        err,
                    )
                else:
                    self._started_notify_characteristics.add(LEGACY_LOGRADIO_UUID)

        try:
            has_logradio = client.has_characteristic(LOGRADIO_UUID)
        except optional_errors as err:
            logger.debug(
                "Failed to start optional log notifications for %s: %s",
                LOGRADIO_UUID,
                err,
            )
        else:
            if has_logradio:
                if LOGRADIO_UUID in self._started_notify_characteristics:
                    has_logradio = False
            if has_logradio:
                log_callback = _get_or_create_handler(
                    LOGRADIO_UUID, lambda: _safe_log_handler
                )
                try:
                    client.start_notify(
                        LOGRADIO_UUID,
                        log_callback,
                        timeout=NOTIFICATION_START_TIMEOUT,
                    )
                except optional_errors as err:
                    logger.debug(
                        "Failed to start optional log notifications for %s: %s",
                        LOGRADIO_UUID,
                        err,
                    )
                else:
                    self._started_notify_characteristics.add(LOGRADIO_UUID)

        ingress_handler = self._notification_manager._get_callback(FROMNUM_UUID)
        if ingress_handler is None:
            ingress_handler = _safe_from_num_handler
        if FROMNUM_UUID in self._started_notify_characteristics:
            self.fromnum_notify_enabled = True
            return
        self.fromnum_notify_enabled = False
        max_attempts = BLEConfig.SERVICE_CHARACTERISTIC_RETRY_COUNT + 1
        for attempt in range(max_attempts):
            try:
                client.start_notify(
                    FROMNUM_UUID,
                    ingress_handler,
                    timeout=NOTIFICATION_START_TIMEOUT,
                )
            except BleakDBusError as err:
                if not _is_notify_acquired_error(err):
                    logger.warning(
                        "Unable to start FROMNUM notifications for %s: %s; falling back to polling reads.",
                        FROMNUM_UUID,
                        err,
                    )
                    return
                logger.debug(
                    "FROMNUM notify already acquired for %s; retrying after best-effort stop_notify (attempt %d/%d)",
                    FROMNUM_UUID,
                    attempt + 1,
                    max_attempts,
                )
                with contextlib.suppress(*optional_errors):
                    client.stop_notify(
                        FROMNUM_UUID,
                        timeout=NOTIFICATION_START_TIMEOUT,
                    )
                if attempt + 1 < max_attempts:
                    _sleep(BLEConfig.SERVICE_CHARACTERISTIC_RETRY_DELAY * (attempt + 1))
                    continue
                logger.warning(
                    "Unable to start FROMNUM notifications for %s after %d attempts due to BlueZ 'Notify acquired'; falling back to polling reads.",
                    FROMNUM_UUID,
                    max_attempts,
                )
                return
            except optional_errors as err:
                logger.warning(
                    "Unable to start FROMNUM notifications for %s: %s; falling back to polling reads.",
                    FROMNUM_UUID,
                    err,
                )
                return
            else:
                if self._notification_manager._get_callback(FROMNUM_UUID) is None:
                    self._notification_manager._subscribe(FROMNUM_UUID, ingress_handler)
                self._started_notify_characteristics.add(FROMNUM_UUID)
                self.fromnum_notify_enabled = True
                return

    def log_radio_handler(self, _: Any, b: bytes | bytearray) -> str | None:
        """Decode protobuf log payload and return formatted message."""
        log_record = mesh_pb2.LogRecord()
        try:
            log_record.ParseFromString(bytes(b))
            if log_record.source:
                return f"[{log_record.source}] {log_record.message}"
            return log_record.message
        except DecodeError:
            logger.warning("Malformed LogRecord received. Skipping.")
            return None

    # COMPAT_STABLE_SHIM (2.7.7): historical underscore callback entrypoint.
    def _log_radio_handler(self, _: Any, b: bytes | bytearray) -> str | None:
        """Decode protobuf log payload and return formatted message."""
        return self.log_radio_handler(_, b)

    @staticmethod
    def legacy_log_radio_handler(_: Any, b: bytes | bytearray) -> str | None:
        """Decode legacy UTF-8 log payload and return normalized message."""
        try:
            return b.decode("utf-8").replace("\n", "")
        except UnicodeDecodeError:
            logger.warning(
                "Malformed legacy LogRecord received (not valid utf-8). Skipping."
            )
            return None

    # COMPAT_STABLE_SHIM (2.7.7): historical underscore callback entrypoint.
    def _legacy_log_radio_handler(
        self, sender: Any, b: bytes | bytearray
    ) -> str | None:
        """Decode legacy UTF-8 log payload and return normalized message."""
        return self.legacy_log_radio_handler(sender, b)
