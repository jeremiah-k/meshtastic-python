"""Request wait runtime for managing async response handling."""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable, Optional

from meshtastic import ResponseHandler
from meshtastic.util import Acknowledgment, Timeout

logger = logging.getLogger(__name__)

UNSCOPED_WAIT_REQUEST_ID: int = -1
WAIT_ATTR_POSITION: str = "receivedPosition"
WAIT_ATTR_TELEMETRY: str = "receivedTelemetry"
WAIT_ATTR_TRACEROUTE: str = "receivedTraceRoute"
WAIT_ATTR_WAYPOINT: str = "receivedWaypoint"
WAIT_ATTR_NAK: str = "receivedNak"

NO_RESPONSE_FIRMWARE_ERROR: str = (
    "No response from node. At least firmware 2.1.22 is required on the destination node."
)
RESPONSE_WAIT_REQID_ERROR: str = (
    "Internal error: response wait requires a positive packet id."
)

DECODE_ERROR_KEY: str = "error"
DECODE_FAILED_PREFIX: str = "decode-failed: "
LEGACY_UNSCOPED_WAIT_ATTR_BY_PORTNUM: dict[int, str] = {}
RETIRED_WAIT_REQUEST_ID_TTL_SECONDS: float = 60.0


class _RequestWaitRuntime:
    """Owns request/wait bookkeeping, scoped wait semantics, and response correlation."""

    def __init__(
        self,
        *,
        lock: threading.RLock,
        get_response_handlers: Callable[[], dict[int, "ResponseHandler"]],
        get_wait_errors: Callable[[], dict[tuple[str, int], str]],
        get_wait_acks: Callable[[], set[tuple[str, int]]],
        get_active_wait_request_ids: Callable[[], dict[str, set[int]]],
        get_retired_wait_request_ids: Callable[[], dict[str, dict[int, float]]],
        get_acknowledgment: Callable[[], Acknowledgment],
        get_timeout: Callable[[], Timeout],
        retired_wait_ttl_seconds: float,
    ) -> None:
        self._lock = lock
        self._get_response_handlers = get_response_handlers
        self._get_wait_errors = get_wait_errors
        self._get_wait_acks = get_wait_acks
        self._get_active_wait_request_ids = get_active_wait_request_ids
        self._get_retired_wait_request_ids = get_retired_wait_request_ids
        self._get_acknowledgment = get_acknowledgment
        self._get_timeout = get_timeout
        self._retired_wait_ttl_seconds = retired_wait_ttl_seconds

    def add_response_handler(
        self,
        request_id: int,
        callback: Callable[[dict[str, Any]], Any],
        *,
        ack_permitted: bool,
    ) -> None:
        """Register a response callback for a request id."""
        with self._lock:
            self._get_response_handlers()[request_id] = ResponseHandler(
                callback=callback,
                ackPermitted=ack_permitted,
            )

    def drop_response_handler(self, request_id: int) -> None:
        """Remove a response callback registration if present."""
        with self._lock:
            self._get_response_handlers().pop(request_id, None)

    def clear_wait_error(
        self,
        acknowledgment_attr: str,
        request_id: int | None = None,
        *,
        clear_scoped: bool = True,
    ) -> None:
        """Clear scoped/unscoped wait state."""
        with self._lock:
            wait_errors = self._get_wait_errors()
            wait_acks = self._get_wait_acks()
            active_wait_request_ids = self._get_active_wait_request_ids()
            if request_id is None:
                if clear_scoped:
                    for key in list(wait_errors):
                        if key[0] == acknowledgment_attr:
                            wait_errors.pop(key, None)
                    for key in list(wait_acks):
                        if key[0] == acknowledgment_attr:
                            wait_acks.discard(key)
                    active_wait_request_ids.pop(acknowledgment_attr, None)
                else:
                    wait_errors.pop(
                        (acknowledgment_attr, UNSCOPED_WAIT_REQUEST_ID), None
                    )
                    wait_acks.discard(
                        (acknowledgment_attr, UNSCOPED_WAIT_REQUEST_ID),
                    )
                self.prune_retired_wait_request_ids_locked(acknowledgment_attr)
            else:
                active_ids = active_wait_request_ids.setdefault(
                    acknowledgment_attr, set()
                )
                wait_errors.pop((acknowledgment_attr, UNSCOPED_WAIT_REQUEST_ID), None)
                wait_acks.discard((acknowledgment_attr, UNSCOPED_WAIT_REQUEST_ID))
                active_ids.add(request_id)
                retired_ids = self.prune_retired_wait_request_ids_locked(
                    acknowledgment_attr
                )
                retired_ids.pop(request_id, None)
                wait_errors.pop((acknowledgment_attr, request_id), None)
                wait_acks.discard((acknowledgment_attr, request_id))
        if request_id is None:
            setattr(self._get_acknowledgment(), acknowledgment_attr, False)

    def prune_retired_wait_request_ids_locked(
        self,
        acknowledgment_attr: str,
    ) -> dict[int, float]:
        """Prune expired retired ids. Caller must hold the response-handler lock."""
        retired_wait_request_ids = self._get_retired_wait_request_ids()
        retired_ids = retired_wait_request_ids.get(acknowledgment_attr)
        if not retired_ids:
            return {}
        now = time.monotonic()
        for retired_id, retired_at in list(retired_ids.items()):
            if now - retired_at > self._retired_wait_ttl_seconds:
                retired_ids.pop(retired_id, None)
        if not retired_ids:
            retired_wait_request_ids.pop(acknowledgment_attr, None)
            return {}
        return retired_ids

    def set_wait_error(
        self,
        acknowledgment_attr: str,
        message: str,
        *,
        request_id: int | None = None,
    ) -> None:
        """Record wait errors using scoped/unscoped compatibility rules."""
        set_legacy_ack_flag = False
        with self._lock:
            active_wait_request_ids = self._get_active_wait_request_ids()
            wait_errors = self._get_wait_errors()
            active_request_ids_for_attr = active_wait_request_ids.get(
                acknowledgment_attr
            )
            has_request_scope = active_request_ids_for_attr is not None
            active_request_ids = active_request_ids_for_attr or set()
            if request_id is not None:
                if request_id in active_request_ids:
                    resolved_request_id = request_id
                elif has_request_scope:
                    logger.debug(
                        "Ignoring stale wait error for %s request_id=%s (active=%s)",
                        acknowledgment_attr,
                        request_id,
                        sorted(active_request_ids),
                    )
                    return
                else:
                    retired_request_ids = self.prune_retired_wait_request_ids_locked(
                        acknowledgment_attr
                    )
                    if request_id in retired_request_ids:
                        logger.debug(
                            "Ignoring retired scoped wait error for %s request_id=%s",
                            acknowledgment_attr,
                            request_id,
                        )
                        return
                    resolved_request_id = UNSCOPED_WAIT_REQUEST_ID
                wait_errors[(acknowledgment_attr, resolved_request_id)] = message
            elif has_request_scope:
                logger.debug(
                    "Ignoring stale unscoped wait error for %s while scoped waits are active: %s",
                    acknowledgment_attr,
                    sorted(active_request_ids),
                )
                return
            else:
                resolved_request_id = UNSCOPED_WAIT_REQUEST_ID
                wait_errors[(acknowledgment_attr, resolved_request_id)] = message
                set_legacy_ack_flag = True
            if request_id is not None and not has_request_scope:
                set_legacy_ack_flag = True
        if set_legacy_ack_flag:
            setattr(self._get_acknowledgment(), acknowledgment_attr, True)

    def mark_wait_acknowledged(
        self,
        acknowledgment_attr: str,
        *,
        request_id: int | None = None,
    ) -> None:
        """Mark wait acknowledgments using scoped/unscoped compatibility rules."""
        set_legacy_ack_flag = False
        with self._lock:
            active_wait_request_ids = self._get_active_wait_request_ids()
            wait_acks = self._get_wait_acks()
            active_request_ids_for_attr = active_wait_request_ids.get(
                acknowledgment_attr
            )
            has_request_scope = active_request_ids_for_attr is not None
            active_request_ids = active_request_ids_for_attr or set()
            if request_id is not None:
                if request_id in active_request_ids:
                    resolved_request_id = request_id
                elif has_request_scope:
                    logger.debug(
                        "Ignoring stale acknowledgement for %s request_id=%s (active=%s)",
                        acknowledgment_attr,
                        request_id,
                        sorted(active_request_ids),
                    )
                    return
                else:
                    retired_request_ids = self.prune_retired_wait_request_ids_locked(
                        acknowledgment_attr
                    )
                    if request_id in retired_request_ids:
                        logger.debug(
                            "Ignoring retired scoped acknowledgement for %s request_id=%s",
                            acknowledgment_attr,
                            request_id,
                        )
                        return
                    resolved_request_id = UNSCOPED_WAIT_REQUEST_ID
                wait_acks.add((acknowledgment_attr, resolved_request_id))
            elif has_request_scope:
                logger.debug(
                    "Ignoring stale unscoped acknowledgement for %s while scoped waits are active: %s",
                    acknowledgment_attr,
                    sorted(active_request_ids),
                )
                return
            else:
                set_legacy_ack_flag = True
            if request_id is not None and not has_request_scope:
                set_legacy_ack_flag = True
        if set_legacy_ack_flag:
            setattr(self._get_acknowledgment(), acknowledgment_attr, True)

    def raise_wait_error_if_present(
        self,
        acknowledgment_attr: str,
        *,
        request_id: int | None,
        error_factory: Callable[[str], Exception],
    ) -> None:
        """Raise and consume the pending wait error for a wait scope."""
        with self._lock:
            wait_errors = self._get_wait_errors()
            active_wait_request_ids = self._get_active_wait_request_ids()
            resolved_request_id = (
                request_id if request_id is not None else UNSCOPED_WAIT_REQUEST_ID
            )
            error_message = wait_errors.pop(
                (acknowledgment_attr, resolved_request_id), None
            )
            if (
                error_message is None
                and request_id is not None
                and acknowledgment_attr not in active_wait_request_ids
            ):
                error_message = wait_errors.pop(
                    (acknowledgment_attr, UNSCOPED_WAIT_REQUEST_ID),
                    None,
                )
        if error_message is not None:
            raise error_factory(error_message)

    def retire_wait_request(
        self,
        acknowledgment_attr: str,
        *,
        request_id: int | None,
    ) -> None:
        """Retire response handlers and scoped wait state after completion/timeout."""
        with self._lock:
            response_handlers = self._get_response_handlers()
            wait_errors = self._get_wait_errors()
            wait_acks = self._get_wait_acks()
            active_wait_request_ids = self._get_active_wait_request_ids()
            retired_wait_request_ids = self._get_retired_wait_request_ids()

            active_request_ids = active_wait_request_ids.get(acknowledgment_attr, set())
            if request_id is not None:
                if request_id in active_request_ids:
                    active_request_ids.discard(request_id)
                    retired_request_ids = retired_wait_request_ids.setdefault(
                        acknowledgment_attr, {}
                    )
                    retired_request_ids[request_id] = time.monotonic()
                    if not active_request_ids:
                        active_wait_request_ids.pop(acknowledgment_attr, None)
                        wait_errors.pop(
                            (acknowledgment_attr, UNSCOPED_WAIT_REQUEST_ID), None
                        )
                        wait_acks.discard(
                            (acknowledgment_attr, UNSCOPED_WAIT_REQUEST_ID)
                        )
                    else:
                        active_wait_request_ids[acknowledgment_attr] = (
                            active_request_ids
                        )
                response_handlers.pop(request_id, None)
                wait_errors.pop((acknowledgment_attr, request_id), None)
                wait_acks.discard((acknowledgment_attr, request_id))
            else:
                if acknowledgment_attr not in active_wait_request_ids:
                    for active_request_id in active_request_ids:
                        response_handlers.pop(active_request_id, None)
                        wait_errors.pop((acknowledgment_attr, active_request_id), None)
                        wait_acks.discard((acknowledgment_attr, active_request_id))
                    active_wait_request_ids.pop(acknowledgment_attr, None)
                self.prune_retired_wait_request_ids_locked(acknowledgment_attr)
                wait_errors.pop((acknowledgment_attr, UNSCOPED_WAIT_REQUEST_ID), None)
                wait_acks.discard((acknowledgment_attr, UNSCOPED_WAIT_REQUEST_ID))
        if request_id is None:
            setattr(self._get_acknowledgment(), acknowledgment_attr, False)

    def wait_for_request_ack(
        self,
        acknowledgment_attr: str,
        request_id: int,
        *,
        timeout_seconds: float,
    ) -> bool:
        """Poll request-scoped wait state until ACK/error or timeout."""
        deadline = time.monotonic() + timeout_seconds
        timeout = self._get_timeout()
        sleep_interval = max(0.01, float(getattr(timeout, "sleepInterval", 0.1)))
        while time.monotonic() < deadline:
            with self._lock:
                wait_errors = self._get_wait_errors()
                wait_acks = self._get_wait_acks()
                key = (acknowledgment_attr, request_id)
                if key in wait_errors:
                    return True
                if key in wait_acks:
                    wait_acks.discard(key)
                    return True
            time.sleep(sleep_interval)
        return False

    def record_routing_wait_error(
        self,
        *,
        acknowledgment_attr: str,
        routing_error_reason: str | None,
        request_id: int | None = None,
    ) -> None:
        """Map routing errors into shared wait-error state."""
        if routing_error_reason is None or routing_error_reason == "NONE":
            return
        if routing_error_reason == "NO_RESPONSE":
            message = NO_RESPONSE_FIRMWARE_ERROR
        else:
            message = f"Routing error on response: {routing_error_reason}"
        self.set_wait_error(
            acknowledgment_attr,
            message,
            request_id=request_id,
        )

    def correlate_inbound_response(
        self,
        *,
        packet_dict: dict[str, Any],
        skip_response_callback_for_decode_failure: bool,
        extract_request_id: Callable[[dict[str, Any]], int | None],
    ) -> None:
        """Correlate inbound response packets with callbacks and wait-state updates."""
        request_id = extract_request_id(packet_dict)
        if request_id is None:
            return
        logger.debug("Got a response for requestId %s", request_id)

        decoded = packet_dict.get("decoded")
        routing = decoded.get("routing") if isinstance(decoded, dict) else None
        is_ack = routing is not None and (
            "errorReason" not in routing or routing["errorReason"] == "NONE"
        )
        response_handler, dropped_due_to_decode_failure = (
            self._select_response_handler_for_packet(
                request_id=request_id,
                is_ack=is_ack,
                skip_response_callback_for_decode_failure=(
                    skip_response_callback_for_decode_failure
                ),
            )
        )
        if dropped_due_to_decode_failure:
            self._apply_admin_decode_failure_wait_state(
                request_id=request_id,
                packet_dict=packet_dict,
            )
        self._invoke_response_callback(
            request_id=request_id,
            response_handler=response_handler,
            packet_dict=packet_dict,
        )

    def _select_response_handler_for_packet(
        self,
        *,
        request_id: int,
        is_ack: bool,
        skip_response_callback_for_decode_failure: bool,
    ) -> tuple[Optional[ResponseHandler], bool]:
        """Select/pop a response handler from shared state for one packet."""
        response_handler: Optional[ResponseHandler] = None
        dropped_due_to_decode_failure = False
        with self._lock:
            response_handlers = self._get_response_handlers()
            candidate = response_handlers.get(request_id, None)
            if candidate is not None:
                callback_name = getattr(candidate.callback, "__name__", "")
                if (
                    skip_response_callback_for_decode_failure
                    and callback_name != "onAckNak"
                ):
                    response_handlers.pop(request_id, None)
                    dropped_due_to_decode_failure = True
                elif (
                    (not is_ack)
                    or callback_name == "onAckNak"
                    or candidate.ackPermitted
                ):
                    response_handler = response_handlers.pop(request_id, None)
        return response_handler, dropped_due_to_decode_failure

    def _apply_admin_decode_failure_wait_state(
        self,
        *,
        request_id: int,
        packet_dict: dict[str, Any],
    ) -> None:
        """Convert admin decode failures into wait-error state and legacy NAK flag."""
        logger.warning(
            "Dropping response callback for requestId %s due to admin decode failure.",
            request_id,
        )
        admin_decoded_payload = packet_dict.get("decoded", {}).get("admin", {})
        if isinstance(admin_decoded_payload, dict):
            admin_decode_error = admin_decoded_payload.get(
                DECODE_ERROR_KEY,
                f"{DECODE_FAILED_PREFIX}unknown error",
            )
        else:
            admin_decode_error = f"{DECODE_FAILED_PREFIX}unknown error"
        self.set_wait_error(
            WAIT_ATTR_NAK,
            f"Failed to decode admin payload: {admin_decode_error}",
            request_id=request_id,
        )
        self._get_acknowledgment().receivedNak = True

    @staticmethod
    def _invoke_response_callback(
        *,
        request_id: int,
        response_handler: Optional[ResponseHandler],
        packet_dict: dict[str, Any],
    ) -> None:
        """Invoke one response callback with error isolation."""
        if response_handler is None:
            return
        logger.debug("Calling response handler for requestId %s", request_id)
        try:
            response_handler.callback(packet_dict)
        except Exception:
            logger.exception(
                "Error in response handler for requestId %s",
                request_id,
            )
