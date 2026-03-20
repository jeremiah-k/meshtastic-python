"""Mesh Interface class."""

# pylint: disable=R0917,C0302

import base64
import collections
import copy
import json
import logging
import math
import random
import secrets
import sys
import threading
import time
import traceback
from dataclasses import dataclass, field
from datetime import datetime
from types import TracebackType
from typing import IO, Any, Callable, Literal, Protocol, TypeAlias, cast

import google.protobuf.json_format
from google.protobuf import message as protobuf_message

try:
    import print_color  # type: ignore[import-untyped]
except ImportError:
    print_color = None

from pubsub import pub
from tabulate import tabulate

import meshtastic.node
from meshtastic import (
    BROADCAST_ADDR,
    BROADCAST_NUM,
    DECODE_ERROR_KEY,
    LOCAL_ADDR,
    NODELESS_WANT_CONFIG_ID,
    ResponseHandler,
    protocols,
    publishingThread,
)
from meshtastic.protobuf import (
    channel_pb2,
    config_pb2,
    mesh_pb2,
    module_config_pb2,
    portnums_pb2,
    telemetry_pb2,
)
from meshtastic.util import (
    Acknowledgment,
    Timeout,
    convert_mac_addr,
    messageToJson,
    remove_keys_from_dict,
    stripnl,
)

logger = logging.getLogger(__name__)

# Heartbeat interval for keeping the interface alive (5 minutes)
HEARTBEAT_INTERVAL_SECONDS = 300

# Packet-id generation constants
PACKET_ID_MASK = 0xFFFFFFFF
PACKET_ID_COUNTER_MASK = 0x3FF
PACKET_ID_RANDOM_MAX = 0x3FFFFF
PACKET_ID_RANDOM_SHIFT_BITS = 10

# Queue backoff while waiting for TX slots to free
QUEUE_WAIT_DELAY_SECONDS = 0.5

# Accepted sendTelemetry payload kinds.
TelemetryType: TypeAlias = Literal[
    "environment_metrics",
    "air_quality_metrics",
    "power_metrics",
    "local_stats",
    "device_metrics",
]
DEFAULT_TELEMETRY_TYPE: TelemetryType = "device_metrics"
VALID_TELEMETRY_TYPES: tuple[TelemetryType, ...] = (
    "environment_metrics",
    "air_quality_metrics",
    "power_metrics",
    "local_stats",
    "device_metrics",
)
VALID_TELEMETRY_TYPE_SET: frozenset[str] = frozenset(VALID_TELEMETRY_TYPES)
UNKNOWN_SNR_QUARTER_DB = -128
MISSING_NODE_NUM_ERROR_TEMPLATE = "NodeId {destination_id} has no numeric 'num' in DB"
NODE_NOT_FOUND_IN_DB_ERROR_TEMPLATE = "NodeId {destination_id} not found in DB"
NODE_NOT_FOUND_DB_UNAVAILABLE_ERROR_TEMPLATE = (
    "NodeId {destination_id} not found and node DB is unavailable"
)
HEX_NODE_ID_TAIL_CHARS = frozenset("0123456789abcdefABCDEF")
DECODE_FAILED_PREFIX = "decode-failed: "
NO_RESPONSE_FIRMWARE_ERROR: str = "No response from node. At least firmware 2.1.22 is required on the destination node."
JSONValue: TypeAlias = (
    None | bool | int | float | str | list["JSONValue"] | dict[str, "JSONValue"]
)
UNSCOPED_WAIT_REQUEST_ID: int = -1
WAIT_ATTR_POSITION: str = "receivedPosition"
WAIT_ATTR_TELEMETRY: str = "receivedTelemetry"
WAIT_ATTR_TRACEROUTE: str = "receivedTraceRoute"
WAIT_ATTR_WAYPOINT: str = "receivedWaypoint"
WAIT_ATTR_NAK: str = "receivedNak"
LEGACY_UNSCOPED_WAIT_ATTR_BY_PORTNUM: dict[int, str] = {
    portnums_pb2.PortNum.POSITION_APP: WAIT_ATTR_POSITION,
    portnums_pb2.PortNum.TRACEROUTE_APP: WAIT_ATTR_TRACEROUTE,
    portnums_pb2.PortNum.TELEMETRY_APP: WAIT_ATTR_TELEMETRY,
    portnums_pb2.PortNum.WAYPOINT_APP: WAIT_ATTR_WAYPOINT,
}
RETIRED_WAIT_REQUEST_ID_TTL_SECONDS: float = 60.0
RESPONSE_WAIT_REQID_ERROR: str = (
    "Internal error: response wait requires a positive packet id."
)
LOCAL_CONFIG_FROM_RADIO_FIELDS: tuple[str, ...] = (
    "device",
    "position",
    "power",
    "network",
    "display",
    "lora",
    "bluetooth",
    "security",
)
MODULE_CONFIG_FROM_RADIO_FIELDS: tuple[str, ...] = (
    "mqtt",
    "serial",
    "external_notification",
    "store_forward",
    "range_test",
    "telemetry",
    "canned_message",
    "audio",
    "remote_hardware",
    "neighbor_info",
    "detection_sensor",
    "ambient_lighting",
    "paxcounter",
    "traffic_management",
)


@dataclass(frozen=True)
class _PublicationIntent:
    """Represents one pubsub emission requested by inbound runtime handlers."""

    topic: str
    payload: dict[str, Any]


@dataclass
class _LazyMessageDict:
    """Lazily materializes protobuf JSON dict form on first access."""

    message: protobuf_message.Message
    _value: dict[str, Any] | None = field(default=None, init=False, repr=False)

    def get(self) -> dict[str, Any]:
        """Return cached MessageToDict payload, computing it only once."""
        if self._value is None:
            self._value = google.protobuf.json_format.MessageToDict(self.message)
        return self._value


@dataclass(frozen=True)
class _FromRadioContext:
    """Normalized FromRadio context passed from parse/normalize to dispatch handlers."""

    message: mesh_pb2.FromRadio
    message_dict: _LazyMessageDict
    config_id: int | None


@dataclass
class _PacketRuntimeContext:
    """Mutable packet runtime state across packet handling phases."""

    packet_dict: dict[str, Any]
    topic: str = "meshtastic.receive"
    decoded: dict[str, Any] | None = None
    skip_response_callback_for_decode_failure: bool = False


class _RequestWaitRuntime:
    """Owns request/wait bookkeeping, scoped wait semantics, and response correlation."""

    def __init__(
        self,
        *,
        lock: threading.RLock,
        get_response_handlers: Callable[[], dict[int, ResponseHandler]],
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
    ) -> tuple[ResponseHandler | None, bool]:
        """Select/pop a response handler from shared state for one packet."""
        response_handler: ResponseHandler | None = None
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
        response_handler: ResponseHandler | None,
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


class _QueueSendRuntime:
    """Owns queue state mutation, resend orchestration, and queue-status correlation."""

    def __init__(
        self,
        *,
        lock: threading.RLock,
        get_queue: Callable[[], collections.OrderedDict[int, mesh_pb2.ToRadio | bool]],
        get_queue_status: Callable[[], mesh_pb2.QueueStatus | None],
        set_queue_status: Callable[[mesh_pb2.QueueStatus], None],
        queue_wait_delay_seconds: float,
    ) -> None:
        self._lock = lock
        self._get_queue = get_queue
        self._get_queue_status = get_queue_status
        self._set_queue_status = set_queue_status
        self._queue_wait_delay_seconds = queue_wait_delay_seconds

    def has_free_space(self) -> bool:
        """Return whether queue status indicates free TX slots."""
        with self._lock:
            queue_status = self._get_queue_status()
            if queue_status is None:
                return True
            return queue_status.free > 0

    def claim(self) -> None:
        """Claim one queue slot when queue status is available."""
        with self._lock:
            queue_status = self._get_queue_status()
            if queue_status is None:
                return
            queue_status.free -= 1

    def pop_for_send(self) -> tuple[int, mesh_pb2.ToRadio | bool] | None:
        """Pop the next sendable queue entry while honoring queue free-space state."""
        with self._lock:
            queue = self._get_queue()
            if not queue:
                return None
            queue_status = self._get_queue_status()
            if queue_status is not None and queue_status.free <= 0:
                return None
            to_resend = queue.popitem(last=False)
            if queue_status is not None:
                queue_status.free -= 1
            return to_resend

    def send_to_radio(
        self,
        to_radio: mesh_pb2.ToRadio,
        *,
        send_impl: Callable[[mesh_pb2.ToRadio], None],
        pop_for_send: Callable[[], tuple[int, mesh_pb2.ToRadio | bool] | None],
        sleep_fn: Callable[[float], None],
    ) -> None:
        """Run outbound send/resend loop using queue ownership semantics."""
        if not to_radio.HasField("packet"):
            # not a meshpacket -- send immediately, give queue a chance,
            # this makes heartbeat trigger queue
            send_impl(to_radio)
        else:
            # meshpacket -- queue
            with self._lock:
                self._get_queue()[to_radio.packet.id] = to_radio

        resent_queue: collections.OrderedDict[int, mesh_pb2.ToRadio | bool] = (
            collections.OrderedDict()
        )
        while True:
            to_resend = pop_for_send()
            if to_resend is None:
                with self._lock:
                    queue_has_items = bool(self._get_queue())
                if not queue_has_items:
                    break
                logger.debug("Waiting for free space in TX Queue")
                sleep_fn(self._queue_wait_delay_seconds)
                continue

            packet_id, packet = to_resend
            resent_queue[packet_id] = packet
            if not isinstance(packet, mesh_pb2.ToRadio):
                continue
            if packet != to_radio:
                logger.debug("Resending packet ID %08x %s", packet_id, packet)
            send_impl(packet)

        self.reconcile_resent_queue(resent_queue=resent_queue)

    def reconcile_resent_queue(
        self,
        *,
        resent_queue: collections.OrderedDict[int, mesh_pb2.ToRadio | bool],
    ) -> None:
        """Reconcile resent packets against ACK-under-us and requeue semantics."""
        for packet_id, packet in resent_queue.items():
            with self._lock:
                # Returns False if packetId is absent (default) or if a
                # False marker was stored for an earlier unexpected ACK.
                # Both cases indicate "already ACKed" for resend purposes.
                acked = self._get_queue().pop(packet_id, False) is False
            if acked:  # Packet got acked under us
                logger.debug("packet %08x got acked under us", packet_id)
                continue
            if packet:
                with self._lock:
                    self._get_queue()[packet_id] = packet

    def record_queue_status(self, queue_status: mesh_pb2.QueueStatus) -> None:
        """Persist latest queue status update."""
        with self._lock:
            self._set_queue_status(queue_status)
        logger.debug(
            "TX QUEUE free %s of %s, res = %s, id = %08x ",
            queue_status.free,
            queue_status.maxlen,
            queue_status.res,
            queue_status.mesh_packet_id,
        )

    def correlate_queue_status_reply(self, queue_status: mesh_pb2.QueueStatus) -> None:
        """Correlate queue status mesh_packet_id replies to pending entries."""
        debug_enabled = logger.isEnabledFor(logging.DEBUG)
        with self._lock:
            queue = self._get_queue()
            queue_snapshot = tuple(queue.keys()) if debug_enabled else ()
            just_queued = queue.pop(queue_status.mesh_packet_id, None)
        if debug_enabled:
            logger.debug(
                "queue: %s",
                " ".join(f"{key:08x}" for key in queue_snapshot),
            )
        if just_queued is None and queue_status.mesh_packet_id != 0:
            with self._lock:
                self._get_queue()[queue_status.mesh_packet_id] = False
            logger.debug(
                "Reply for unexpected packet ID %08x",
                queue_status.mesh_packet_id,
            )

    def handle_queue_status_from_radio(
        self, queue_status: mesh_pb2.QueueStatus
    ) -> None:
        """Apply queue status updates and queue reply correlation."""
        self.record_queue_status(queue_status)
        if queue_status.res:
            return
        self.correlate_queue_status_reply(queue_status)


class _SerializablePayload(Protocol):
    """Protocol for payloads that can serialize to bytes."""

    def SerializeToString(self) -> bytes:
        """Return serialized payload bytes."""
        ...  # pylint: disable=unnecessary-ellipsis


PayloadData: TypeAlias = bytes | bytearray | memoryview | _SerializablePayload


def _format_missing_node_num_error(destination_id: int | str) -> str:
    """Return a consistent error message for nodes missing numeric IDs."""
    return MISSING_NODE_NUM_ERROR_TEMPLATE.format(destination_id=destination_id)


def _format_node_not_found_in_db_error(destination_id: int | str) -> str:
    """Return a consistent error for node IDs missing from an available node DB."""
    return NODE_NOT_FOUND_IN_DB_ERROR_TEMPLATE.format(destination_id=destination_id)


def _format_node_db_unavailable_error(destination_id: int | str) -> str:
    """Return a consistent error for node IDs when node DB is unavailable."""
    return NODE_NOT_FOUND_DB_UNAVAILABLE_ERROR_TEMPLATE.format(
        destination_id=destination_id
    )


def _extract_hex_node_id_body(destination_id: str) -> str | None:
    """Return a compact 8-hex node-id body when ``destination_id`` matches supported forms.

    Parameters
    ----------
    destination_id : str
        Candidate node identifier. Supported prefixed forms are ``!12345678``,
        ``0x12345678``, and ``0X12345678``; bare ``12345678`` is also supported.

    Returns
    -------
    str | None
        The 8-character hexadecimal body if the full input is a supported compact
        node-id format; otherwise ``None``.
    """
    candidate = destination_id
    if destination_id.startswith("!"):
        candidate = destination_id[1:]
    elif destination_id.startswith(("0x", "0X")):
        candidate = destination_id[2:]
    if len(candidate) != 8:
        return None
    if not all(ch in HEX_NODE_ID_TAIL_CHARS for ch in candidate):
        return None
    return candidate


def _logger_has_visible_info_handler(target_logger: logging.Logger) -> bool:
    """Return whether INFO logs from `target_logger` are visible on stdout streams."""
    if target_logger.disabled or target_logger.getEffectiveLevel() > logging.INFO:
        return False

    visible_console_streams = {
        sys.stdout,
        sys.__stdout__,
    }
    current_logger: logging.Logger | None = target_logger
    while current_logger is not None:
        for handler in current_logger.handlers:
            handler_stream = getattr(handler, "stream", None)
            console = getattr(handler, "console", None)
            console_stream = getattr(console, "file", None)
            if handler.level <= logging.INFO and (
                handler_stream in visible_console_streams
                or console_stream in visible_console_streams
            ):
                return True
        if not current_logger.propagate:
            break
        current_logger = current_logger.parent
    return False


def _emit_response_summary(message: str) -> None:
    """Emit a short response summary without hiding legacy stdout behavior."""
    logger.info("%s", message)
    if not _logger_has_visible_info_handler(logger):
        print(message)


def _normalize_json_serializable(value: object) -> JSONValue:
    """Recursively normalize common non-JSON-native values into JSON-safe forms."""
    if isinstance(value, (bytes, bytearray, memoryview)):
        return "base64:" + base64.b64encode(bytes(value)).decode("ascii")
    if isinstance(value, dict):
        return {
            str(key): _normalize_json_serializable(inner_value)
            for key, inner_value in value.items()
        }
    if isinstance(value, (list, tuple, set)):
        return [_normalize_json_serializable(item) for item in value]
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    return str(value)


def _timeago(delta_secs: int) -> str:
    """Produce a short human-readable relative time string for a past interval.

    Parameters
    ----------
    delta_secs : int
        Number of seconds elapsed in the past; zero or negative values are treated as "now".

    Returns
    -------
    str
        A compact relative time string such as "now", "30 sec ago", "1 hour ago", or "2 days ago".
    """
    intervals = (
        ("year", 60 * 60 * 24 * 365),
        ("month", 60 * 60 * 24 * 30),
        ("day", 60 * 60 * 24),
        ("hour", 60 * 60),
        ("min", 60),
        ("sec", 1),
    )
    for name, interval_duration in intervals:
        if delta_secs < interval_duration:
            continue
        x = delta_secs // interval_duration
        plur = "s" if x > 1 else ""
        return f"{x} {name}{plur} ago"

    return "now"


class MeshInterface:  # pylint: disable=R0902
    """Interface class for meshtastic devices.

    Properties:

    isConnected
    nodes
    debugOut
    """

    class MeshInterfaceError(Exception):
        """An exception class for general mesh interface errors."""

        def __init__(self, message: str) -> None:
            """Create a MeshInterfaceError with a human-readable message.

            Parameters
            ----------
            message : str
                The error message describing the failure.
            """
            self.message = message
            super().__init__(self.message)

    def __init__(
        self,
        debugOut: IO[str] | Callable[[str], Any] | None = None,
        noProto: bool = False,
        noNodes: bool = False,
        timeout: float = 300.0,
    ) -> None:
        """Initialize the MeshInterface and configure runtime options.

        Parameters
        ----------
        debugOut : IO[str] | Callable[[str], Any] | None
            Destination for human-readable log lines; if provided the
            interface will publish device logs to this output. (Default value = None)
        noProto : bool
            If True, disable running the meshtastic protocol layer over the link
            (operate as a dumb serial client). (Default value = False)
        noNodes : bool
            If True, instruct the device not to send its node database on startup;
            only other configuration will be requested. (Default value = False)
        timeout : float
            Default timeout in seconds for operations that wait for replies.
        """
        self.debugOut = debugOut
        self.nodes: dict[str, dict[str, Any]] | None = None
        self.isConnected: threading.Event = threading.Event()
        self.noProto: bool = noProto
        self.localNode: meshtastic.node.Node = meshtastic.node.Node(
            self, -1, timeout=timeout
        )  # We fixup nodenum later
        self.myInfo: mesh_pb2.MyNodeInfo | None = None  # We don't have device info yet
        self.metadata: mesh_pb2.DeviceMetadata | None = (
            None  # We don't have device metadata yet
        )
        # ------------------------------------------------------------------
        # Locking contract for MeshInterface shared state.
        #
        # Shared mutable state is intentionally split by concern:
        # - _response_handlers_lock: responseHandlers map + response wait errors
        #   + _response_wait_acks + _active_wait_request_ids
        #   + _retired_wait_request_ids
        # - _heartbeat_lock: _closing, heartbeatTimer, _heartbeat_inflight,
        #   isConnected
        # - _packet_id_lock: currentPacketId generation
        # - _queue_lock: queue + queueStatus
        # - _node_db_lock: nodes/nodesByNum/_localChannels plus myInfo/metadata
        #   and local config/module config copies that are updated from RX thread.
        #
        # Deadlock-avoidance rule:
        # - Do not hold more than one MeshInterface lock at a time.
        # - If a future change must nest locks, establish and document a single
        #   global order in this block before introducing nested acquisition.
        #
        # Current implementation follows the no-nesting rule by acquiring one
        # lock, copying/snapshotting needed state, and doing I/O/callbacks after
        # releasing the lock.
        # ------------------------------------------------------------------
        # responseHandlers is shared by _add_response_handler (sendData path) and
        # _handle_packet_from_radio (receive thread). Use this lock to serialize
        # responseHandlers access across those call sites.
        self._response_handlers_lock = threading.RLock()
        self.responseHandlers: dict[
            int, ResponseHandler
        ] = {}  # A map from request ID to the handler
        self._response_wait_errors: dict[tuple[str, int], str] = {}
        self._response_wait_acks: set[tuple[str, int]] = set()
        self._active_wait_request_ids: dict[str, set[int]] = {}
        self._retired_wait_request_ids: dict[str, dict[int, float]] = {}
        self.failure: BaseException | None = (
            None  # If we've encountered a fatal exception it will be kept here
        )
        self._timeout: Timeout = Timeout(maxSecs=timeout)
        self._acknowledgment: Acknowledgment = Acknowledgment()
        self.heartbeatTimer: threading.Timer | None = None
        self._heartbeat_lock = threading.RLock()
        # Track heartbeat sends that have passed the _closing gate but have not
        # finished I/O yet. close() waits on this to avoid post-close sends.
        self._heartbeat_inflight = 0
        self._heartbeat_idle_condition = threading.Condition(self._heartbeat_lock)
        self._packet_id_lock = threading.Lock()
        self._queue_lock = threading.RLock()
        # Guard node DB plus configuration state updated by the receive thread:
        # nodes/nodesByNum/_localChannels and myInfo/metadata/configId/localNode config copies.
        self._node_db_lock = threading.RLock()
        self._closing = False
        self.currentPacketId: int = random.randint(0, PACKET_ID_MASK)
        self.nodesByNum: dict[int, dict[str, Any]] | None = None
        self.noNodes: bool = noNodes
        self.configId: int | None = NODELESS_WANT_CONFIG_ID if noNodes else None
        self.gotResponse: bool = False  # used in gpio read
        self.mask: int | None = None  # legacy GPIO mask fallback (remote_hardware)
        self.queueStatus: mesh_pb2.QueueStatus | None = None
        self.queue: collections.OrderedDict[int, mesh_pb2.ToRadio | bool] = (
            collections.OrderedDict()
        )
        self._localChannels: list[channel_pb2.Channel] = []
        self._request_wait_runtime = _RequestWaitRuntime(
            lock=self._response_handlers_lock,
            get_response_handlers=lambda: self.responseHandlers,
            get_wait_errors=lambda: self._response_wait_errors,
            get_wait_acks=lambda: self._response_wait_acks,
            get_active_wait_request_ids=lambda: self._active_wait_request_ids,
            get_retired_wait_request_ids=lambda: self._retired_wait_request_ids,
            get_acknowledgment=lambda: self._acknowledgment,
            get_timeout=lambda: self._timeout,
            retired_wait_ttl_seconds=RETIRED_WAIT_REQUEST_ID_TTL_SECONDS,
        )
        self._queue_send_runtime = _QueueSendRuntime(
            lock=self._queue_lock,
            get_queue=lambda: self.queue,
            get_queue_status=lambda: self.queueStatus,
            set_queue_status=lambda queue_status: setattr(
                self, "queueStatus", queue_status
            ),
            queue_wait_delay_seconds=QUEUE_WAIT_DELAY_SECONDS,
        )
        self._from_radio_dispatch_map_cache: dict[
            str, Callable[[_FromRadioContext], list[_PublicationIntent]]
        ] | None = None

        # We could have just not passed in debugOut to MeshInterface, and instead told consumers to subscribe to
        # the meshtastic.log.line publish instead.  Alas though changing that now would be a breaking API change
        # for any external consumers of the library.
        if debugOut:
            pub.subscribe(MeshInterface._print_log_line, "meshtastic.log.line")

    def close(self) -> None:
        """Shut down the interface and send a disconnect to the radio.

        Marks the interface as closing, cancels any scheduled heartbeat timer,
        waits for any in-flight heartbeat send to finish, then emits a
        disconnect message to the radio transport.
        """
        # Handle case where __init__ returned early before parent initialization
        heartbeat_lock = getattr(self, "_heartbeat_lock", None)
        heartbeat_idle_condition = getattr(self, "_heartbeat_idle_condition", None)
        if heartbeat_lock is not None:
            with heartbeat_lock:
                if self._closing:
                    return
                self._closing = True
                timer = self.heartbeatTimer
                self.heartbeatTimer = None
            if timer:
                timer.cancel()
            # Extra complexity is intentional: a callback can pass the _closing
            # check and begin sendHeartbeat() just before close() starts. Wait
            # until any such in-flight send completes so close() provides a
            # strong "no heartbeat after close returns" guarantee.
            if isinstance(heartbeat_idle_condition, threading.Condition):
                with heartbeat_lock:
                    while self._heartbeat_inflight > 0:
                        heartbeat_idle_condition.wait()
            try:
                self._send_disconnect()
            except (OSError, MeshInterface.MeshInterfaceError):
                logger.debug(
                    "Failed to send disconnect during close(); continuing shutdown.",
                    exc_info=True,
                )
            except TypeError:
                is_finalizing = getattr(sys, "is_finalizing", None)
                if not (callable(is_finalizing) and is_finalizing()):
                    raise
                logger.debug(
                    "Failed to send disconnect during interpreter finalization; continuing shutdown.",
                    exc_info=True,
                )
        # debugOut is caller-owned (often shared via outer context managers);
        # do not close it here. Only clear our reference on shutdown.
        if hasattr(self, "debugOut"):
            self.debugOut = None

    def __enter__(self) -> "MeshInterface":
        """Enter a context for use with the with statement and return this MeshInterface instance.

        Returns
        -------
        'MeshInterface'
            This MeshInterface instance.
        """
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        trace: TracebackType | None,
    ) -> None:
        """Handle context-manager exit: log any exception information and close the interface.

        If an exception occurred within the with-block (exc_type is not None),
        any exception raised by close() is logged and suppressed so the original
        exception propagates. If no with-block exception occurred, close() exceptions
        are allowed to propagate normally.

        Parameters
        ----------
        exc_type : type[BaseException] | None
            The exception class if an exception was raised, otherwise None.
        exc_value : BaseException | None
            The exception instance if one was raised, otherwise None.
        trace : TracebackType | None
            The traceback object for the exception if present, otherwise None.
        """
        if exc_type is not None and exc_value is not None:
            logger.error(
                "An exception of type %s with value %s has occurred",
                exc_type,
                exc_value,
            )
            if trace is not None:
                logger.error("Traceback:\n%s", "".join(traceback.format_tb(trace)))
        try:
            self.close()
        except Exception:
            if exc_type is not None:
                logger.warning(
                    "close() failed while unwinding an existing exception.",
                    exc_info=True,
                )
            else:
                raise

    @staticmethod
    def _print_log_line(line: str, interface: Any) -> None:
        """Format and emit a single log line to the configured debug output.

        If a color printer is available and the interface is using stdout, apply a color
        based on the log level text (DEBUG→cyan, INFO→white, WARN→yellow, ERR→red).
        If interface.debugOut is a callable, invoke it with the line.
        If interface.debugOut exposes a write() method, write the line followed by a newline.

        Parameters
        ----------
        line : str
            The log line text to emit.
        interface : Any
            The MeshInterface instance (or similar) providing a `debugOut`
            attribute which may be stdout, a callable that accepts a string, or a
            file-like object with a `write()` method.
        """
        if print_color is not None and interface.debugOut == sys.stdout:
            # this isn't quite correct (could cause false positives), but currently our formatting differs between different log representations
            if "DEBUG" in line:
                print_color.print(line, color=cast(Any, "cyan"))
            elif "INFO" in line:
                print_color.print(line, color="white")
            elif "WARN" in line:
                print_color.print(line, color="yellow")
            elif "ERR" in line:
                print_color.print(line, color="red")
            else:
                print_color.print(line)
        elif callable(interface.debugOut):
            interface.debugOut(line)
        elif hasattr(interface.debugOut, "write"):
            interface.debugOut.write(line + "\n")

    def _handle_log_line(self, line: str) -> None:
        """Publish a device log line to the "meshtastic.log.line" topic, normalizing any trailing newline.

        Parameters
        ----------
        line : str
            Log text received from the device; a trailing newline (if present) is removed before publishing.
        """

        # Devices should _not_ be including a newline at the end of each log-line str (especially when
        # encapsulated as a LogRecord).  But to cope with old device loads, we check for that and fix it here:
        if line.endswith("\n"):
            line = line[:-1]

        pub.sendMessage("meshtastic.log.line", line=line, interface=self)

    def _handle_log_record(self, record: mesh_pb2.LogRecord) -> None:
        """Process a protobuf LogRecord by extracting its message text and handling it as a device log line.

        Parameters
        ----------
        record : mesh_pb2.LogRecord
            Protobuf log record containing the `message` field to be processed.
        """
        # For now we just try to format the line as if it had come in over the serial port
        self._handle_log_line(record.message)

    def showInfo(self, file: IO[str] | None = None) -> str:
        """Return a human-readable JSON summary of the mesh interface including owner, local node info, metadata, and known nodes.

        The summary omits internal node fields (`raw`, `decoded`, `payload`) and normalizes stored MAC addresses
        to a human-readable form before formatting.

        Parameters
        ----------
        file : IO[str] | None
            File-like object to which the summary is written; defaults to sys.stdout.

        Returns
        -------
        summary : str
            The formatted summary text that was written to `file`.
        """
        owner = f"Owner: {self.getLongName()} ({self.getShortName()})"
        with self._node_db_lock:
            my_info = self.myInfo
            metadata_info = self.metadata
        myinfo = ""
        if my_info:
            myinfo = f"\nMy info: {messageToJson(my_info)}"
        metadata = ""
        if metadata_info:
            metadata = f"\nMetadata: {messageToJson(metadata_info)}"
        mesh = "\n\nNodes in mesh: "
        nodes: dict[str, JSONValue] = {}
        with self._node_db_lock:
            nodes_snapshot = (
                [copy.deepcopy(node) for node in self.nodes.values()]
                if self.nodes
                else []
            )
        for n in nodes_snapshot:
            # when the TBeam is first booted, it sometimes shows the raw data
            # so, we will just remove any raw keys
            keys_to_remove = ("raw", "decoded", "payload")
            n2 = remove_keys_from_dict(keys_to_remove, n)

            user = n2.get("user")
            if not isinstance(user, dict):
                continue

            # if we have 'macaddr', re-format it
            val = user.get("macaddr")
            if isinstance(val, str):
                # decode the base64 value
                try:
                    user["macaddr"] = convert_mac_addr(val)
                except (TypeError, ValueError):
                    logger.debug(
                        "Skipping malformed macaddr for node %s",
                        user.get("id"),
                    )

            # use id as dictionary key for correct json format in list of nodes
            node_id = user.get("id")
            if node_id is not None:
                nodes[str(node_id)] = _normalize_json_serializable(n2)
        infos = owner + myinfo + metadata + mesh + json.dumps(nodes, indent=2)
        if file is None:
            file = sys.stdout
        print(infos, file=file)
        return infos

    def showNodes(
        self, includeSelf: bool = True, showFields: list[str] | None = None
    ) -> str:  # pylint: disable=W0613
        """Produce a formatted table summarizing known mesh nodes.

        Parameters
        ----------
        includeSelf : bool
            If False, omit the local node from the output. (Default value = True)
        showFields : list[str] | None
            Ordered list of node fields to
            include (dotted paths for nested fields). If omitted or empty,
            a sensible default set of fields is used; the row-number column
            "N" is always included.

        Returns
        -------
        table : str
            The rendered table string (also printed to stdout)
            containing one row per node and columns mapped to human-readable
            headings.
        """

        def _get_human_readable(name: str) -> str:
            """Map an internal dotted field path to a human-readable column label.

            Parameters
            ----------
            name : str
                Dotted field path or key to convert (e.g., "user.longName", "position.latitude").

            Returns
            -------
            str
                A human-readable label for the given field when known (e.g., "User", "Latitude"); otherwise returns the original `name`.
            """
            name_map = {
                "user.longName": "User",
                "user.id": "ID",
                "user.shortName": "AKA",
                "user.hwModel": "Hardware",
                "user.publicKey": "Pubkey",
                "user.role": "Role",
                "position.latitude": "Latitude",
                "position.longitude": "Longitude",
                "position.altitude": "Altitude",
                "deviceMetrics.batteryLevel": "Battery",
                "deviceMetrics.channelUtilization": "Channel util.",
                "deviceMetrics.airUtilTx": "Tx air util.",
                "snr": "SNR",
                "hopsAway": "Hops",
                "channel": "Channel",
                "lastHeard": "LastHeard",
                "since": "Since",
                "isFavorite": "Fav",
            }

            return name_map.get(name, name)

        def _format_float(
            value: int | float | None,
            precision: int = 2,
            unit: str = "",
        ) -> str | None:
            """Format a numeric value as a string with fixed decimal precision and an optional unit suffix.

            Parameters
            ----------
            value : int | float | None
                Value to format; returns `None` when this is `None`.
            precision : int
                Number of digits after the decimal point. (Default value = 2)
            unit : str
                Suffix appended directly after the number (for example, "V" or " dB"). (Default value = '')

            Returns
            -------
            str | None
                `None` if `value` is `None`, otherwise the formatted string using the given precision and unit (e.g., "3.14V").
            """
            return f"{value:.{precision}f}{unit}" if value is not None else None

        def _get_lh(ts: int | float | None) -> str | None:
            """Format a Unix timestamp as "YYYY-MM-DD HH:MM:SS" or return None when no timestamp is available.

            Parameters
            ----------
            ts : int | float | None
                Seconds since the Unix epoch. If `ts` is `None` or `0`, no timestamp is available.

            Returns
            -------
            str | None
                Formatted timestamp string in `YYYY-MM-DD HH:MM:SS` form, or `None` if `ts` is `None` or `0`.
            """
            return (
                datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")
                if ts is not None and ts != 0
                else None
            )

        def _get_time_ago(ts: int | float | None) -> str | None:
            """Return a short human-readable relative time string for a past Unix epoch timestamp.

            Parameters
            ----------
            ts : int | float | None
                Unix timestamp in seconds since the epoch. If `None` or `0`, no computation is performed.

            Returns
            -------
            str | None
                A concise relative time string such as "now", "5 sec ago", or "2 min ago",
                or `None` if `ts` is `None`, `0`, or represents a time in the future.
            """
            if ts is None or ts == 0:
                return None
            delta = datetime.now() - datetime.fromtimestamp(ts)
            delta_secs = int(delta.total_seconds())
            if delta_secs < 0:
                return None  # not handling a timestamp from the future
            return _timeago(delta_secs)

        def _get_nested_value(node_dict: dict[str, Any], key_path: str) -> Any:
            """Retrieve a nested value from a dictionary using a dotted key path.

            Parameters
            ----------
            node_dict : dict[str, Any]
                Dictionary to traverse.
            key_path : str
                Dotted path (e.g., "a.b.c"). Non-dotted paths are treated
                as a single-level lookup on node_dict.

            Returns
            -------
            Any
                The value found at the given path, or `None` if any intermediate
                key is missing or an intermediate value is not a dictionary.
            """
            # Guard against None or non-dict input
            if not isinstance(node_dict, dict):
                return None
            if "." not in key_path:
                # Treat non-dotted path as a single-level lookup
                return node_dict.get(key_path)
            keys = key_path.split(".")
            value: Any = node_dict
            for key in keys:
                if isinstance(value, dict):
                    value = value.get(key)
                else:
                    return None
            return value

        if showFields is None or len(showFields) == 0:
            # The default set of fields to show (e.g., the status quo)
            showFields = [
                "N",
                "user.longName",
                "user.id",
                "user.shortName",
                "user.hwModel",
                "user.publicKey",
                "user.role",
                "position.latitude",
                "position.longitude",
                "position.altitude",
                "deviceMetrics.batteryLevel",
                "deviceMetrics.channelUtilization",
                "deviceMetrics.airUtilTx",
                "snr",
                "hopsAway",
                "channel",
                "isFavorite",
                "lastHeard",
                "since",
            ]
        else:
            # Always at least include the row number, but avoid duplicates.
            showFields = (
                ["N", *showFields] if "N" not in showFields else list(showFields)
            )

        rows: list[dict[str, Any]] = []
        with self._node_db_lock:
            nodes_snapshot = list(self.nodesByNum.values()) if self.nodesByNum else []
            nodes_log_snapshot = dict(self.nodes) if self.nodes else {}
            local_node_num = self.localNode.nodeNum
        if nodes_snapshot:
            logger.debug("self.nodes:%s", nodes_log_snapshot)
            for node in nodes_snapshot:
                if not includeSelf and node["num"] == local_node_num:
                    continue

                presumptive_id = f"!{node['num']:08x}"

                # This allows the user to specify fields that wouldn't otherwise be included.
                fields = {}
                for field in showFields:
                    if "." in field:
                        raw_value = _get_nested_value(node, field)
                    else:
                        # The "since" column is synthesized, it's not retrieved from the device. Get the
                        # lastHeard value here, and then we'll format it properly below.
                        if field == "since":
                            raw_value = node.get("lastHeard")
                        else:
                            raw_value = node.get(field)

                    formatted_value: str | None = ""

                    # Some of these need special formatting or processing.
                    if field == "channel":
                        if raw_value is None:
                            formatted_value = "0"
                    elif field == "deviceMetrics.channelUtilization":
                        formatted_value = _format_float(raw_value, 2, "%")
                    elif field == "deviceMetrics.airUtilTx":
                        formatted_value = _format_float(raw_value, 2, "%")
                    elif field == "deviceMetrics.batteryLevel":
                        if raw_value in (0, 101):
                            formatted_value = "Powered"
                        else:
                            formatted_value = _format_float(raw_value, 0, "%")
                    elif field == "isFavorite":
                        formatted_value = "*" if raw_value else ""
                    elif field == "lastHeard":
                        formatted_value = _get_lh(raw_value)
                    elif field == "position.latitude":
                        formatted_value = _format_float(raw_value, 4, "°")
                    elif field == "position.longitude":
                        formatted_value = _format_float(raw_value, 4, "°")
                    elif field == "position.altitude":
                        formatted_value = _format_float(raw_value, 0, "m")
                    elif field == "since":
                        formatted_value = _get_time_ago(raw_value) or "N/A"
                    elif field == "snr":
                        formatted_value = _format_float(raw_value, 0, " dB")
                    elif field == "user.shortName":
                        formatted_value = (
                            raw_value
                            if raw_value is not None
                            else f"Meshtastic {presumptive_id[-4:]}"
                        )
                    elif field == "user.id":
                        formatted_value = (
                            raw_value if raw_value is not None else presumptive_id
                        )
                    else:
                        formatted_value = raw_value  # No special formatting

                    fields[field] = formatted_value

                # Filter out any field in the data set that was not specified.
                filtered_data = {
                    _get_human_readable(k): v
                    for k, v in fields.items()
                    if k in showFields
                }
                rows.append(filtered_data)

        rows.sort(key=lambda r: r.get("LastHeard") or "0000", reverse=True)
        for i, row in enumerate(rows):
            row["N"] = i + 1

        table = str(
            tabulate(rows, headers="keys", missingval="N/A", tablefmt="fancy_grid")
        )
        print(table)
        return table

    def getNode(
        self,
        nodeId: str,
        requestChannels: bool = True,
        requestChannelAttempts: int = 3,
        timeout: float = 300.0,
    ) -> meshtastic.node.Node:
        """Get the Node object for the given node identifier.

        If nodeId is the local or broadcast address, return the already-initialized local node.
        If requestChannels is True, request channel information from the remote node and retry up to
        requestChannelAttempts until channel data is received or the operation times out.
        Raises MeshInterfaceError if channel retrieval repeatedly fails.

        Parameters
        ----------
        nodeId : str
            Node identifier (hex/node-id string, or LOCAL_ADDR/BROADCAST_ADDR).
        requestChannels : bool
            If True, request channel settings from the remote node. (Default value = True)
        requestChannelAttempts : int
            Number of attempts to retrieve channel info before giving up. (Default value = 3)
        timeout : float
            Timeout in seconds passed to the Node constructor and used while waiting for responses. (Default value = 300.0)

        Returns
        -------
        meshtastic.node.Node
            The Node object corresponding to nodeId.

        Raises
        ------
        MeshInterfaceError
            If channel retrieval repeatedly fails or times out.
        """
        if nodeId in (LOCAL_ADDR, BROADCAST_ADDR):
            return self.localNode
        else:
            n = meshtastic.node.Node(self, nodeId, timeout=timeout)
            # Only request device settings and channel info when necessary
            if requestChannels:
                logger.debug("About to requestChannels")
                n.requestChannels()
                retries_left = requestChannelAttempts
                last_index: int = 0
                while retries_left > 0:
                    retries_left -= 1
                    if not n.waitForConfig():
                        new_index: int = (
                            len(n.partialChannels) if n.partialChannels else 0
                        )
                        # each time we get a new channel, reset the counter
                        if new_index != last_index:
                            retries_left = requestChannelAttempts - 1
                        if retries_left <= 0:
                            raise self.MeshInterfaceError(
                                "Error: Timed out waiting for channels, giving up"
                            )
                        logger.warning(
                            "Timed out trying to retrieve channel info, retrying"
                        )
                        n.requestChannels(startingIndex=new_index)
                        last_index = new_index
                    else:
                        break
            return n

    def sendText(
        self,
        text: str,
        destinationId: int | str = BROADCAST_ADDR,
        wantAck: bool = False,
        wantResponse: bool = False,
        onResponse: Callable[[dict[str, Any]], Any] | None = None,
        channelIndex: int = 0,
        portNum: portnums_pb2.PortNum.ValueType = portnums_pb2.PortNum.TEXT_MESSAGE_APP,
        replyId: int | None = None,
        hopLimit: int | None = None,
    ) -> mesh_pb2.MeshPacket:
        """Send UTF-8 text to a node (or broadcast) and return the transmitted MeshPacket.

        Parameters
        ----------
        text : str
            Message text to send; encoded as UTF-8 for transport.
        destinationId : int | str
            Target node as numeric node number or node ID string; use BROADCAST_ADDR
            to send to all nodes. (Default value = BROADCAST_ADDR)
        replyId : int | None
            If provided, marks this packet as a response to the given message ID. (Default value = None)
        wantAck : bool
            If True, request transport-level acknowledgment. (Default value = False)
        wantResponse : bool
            If True, request an application-level response. (Default value = False)
        onResponse : Callable[[dict[str, Any]], Any] | None
            Optional callback for the response. (Default value = None)
        channelIndex : int
            Channel index to send on. (Default value = 0)
        portNum : portnums_pb2.PortNum.ValueType
            Application port number for the text message. (Default value = portnums_pb2.PortNum.TEXT_MESSAGE_APP)
        hopLimit : int | None
            Optional hop limit override for the outgoing packet. (Default value = None)

        Returns
        -------
        mesh_pb2.MeshPacket
            The packet that was sent; its `id` field will be populated and can be used to track acknowledgments or naks.
        """

        return self.sendData(
            text.encode("utf-8"),
            destinationId,
            portNum=portNum,
            wantAck=wantAck,
            wantResponse=wantResponse,
            onResponse=onResponse,
            channelIndex=channelIndex,
            replyId=replyId,
            hopLimit=hopLimit,
        )

    def sendAlert(
        self,
        text: str,
        destinationId: int | str = BROADCAST_ADDR,
        onResponse: Callable[[dict[str, Any]], Any] | None = None,
        channelIndex: int = 0,
        hopLimit: int | None = None,
    ) -> mesh_pb2.MeshPacket:
        """Send a high-priority alert text to a node, which may trigger special notifications on clients.

        Parameters
        ----------
        text : str
            Alert text to send.
        destinationId : int | str
            Node ID or node number to receive the alert (defaults to broadcast).
        onResponse : Callable[[dict[str, Any]], Any] | None
            Optional callback invoked if a response is received for this message. (Default value = None)
        channelIndex : int
            Channel index to use when sending. (Default value = 0)
        hopLimit : int | None
            Optional hop limit override for the outgoing packet. (Default value = None)

        Returns
        -------
        mesh_pb2.MeshPacket
            The sent mesh packet with its `id` populated.
        """

        return self.sendData(
            text.encode("utf-8"),
            destinationId,
            portNum=portnums_pb2.PortNum.ALERT_APP,
            wantAck=False,
            wantResponse=False,
            onResponse=onResponse,
            channelIndex=channelIndex,
            priority=mesh_pb2.MeshPacket.Priority.ALERT,
            hopLimit=hopLimit,
        )

    def sendMqttClientProxyMessage(self, topic: str, data: bytes) -> None:
        """Send an MQTT client-proxy message through the radio.

        Parameters
        ----------
        topic : str
            MQTT topic to forward.
        data : bytes
            MQTT payload to forward.
        """
        prox = mesh_pb2.MqttClientProxyMessage()
        prox.topic = topic
        prox.data = data
        toRadio = mesh_pb2.ToRadio()
        toRadio.mqttClientProxyMessage.CopyFrom(prox)
        self._send_to_radio(toRadio)

    def sendData(  # pylint: disable=R0913
        self,
        data: PayloadData,
        destinationId: int | str = BROADCAST_ADDR,
        portNum: portnums_pb2.PortNum.ValueType = portnums_pb2.PortNum.PRIVATE_APP,
        wantAck: bool = False,
        wantResponse: bool = False,
        onResponse: Callable[[dict[str, Any]], Any] | None = None,
        onResponseAckPermitted: bool = False,
        channelIndex: int = 0,
        hopLimit: int | None = None,
        pkiEncrypted: bool = False,
        publicKey: bytes | None = None,
        priority: mesh_pb2.MeshPacket.Priority.ValueType = mesh_pb2.MeshPacket.Priority.RELIABLE,
        replyId: int | None = None,
    ) -> mesh_pb2.MeshPacket:
        """Send a payload to a mesh node.

        Parameters
        ----------
        data : Any
            Payload to send; protobuf messages are serialized to bytes.
        destinationId : int | str
            Destination node identifier (node id string, numeric node number,
            `BROADCAST_ADDR`, or `LOCAL_ADDR`).
        portNum : portnums_pb2.PortNum.ValueType
            Application port number for the payload.
        wantAck : bool
            Request link-layer ACK for this packet.
        wantResponse : bool
            Register a response handler for this packet.
        onResponse : Callable[[dict[str, Any]], Any] | None
            Optional callback invoked for matching responses.
        onResponseAckPermitted : bool
            Whether ACK-only responses should trigger `onResponse`.
        channelIndex : int
            Channel index used to transmit the packet.
        hopLimit : int | None
            Optional hop-limit override for the packet.
        pkiEncrypted : bool
            Whether to request PKI encryption for this payload.
        publicKey : bytes | None
            Optional destination public key used for PKI encryption.
        priority : mesh_pb2.MeshPacket.Priority.ValueType
            Mesh packet priority for retransmission behavior.
        replyId : int | None
            Optional mesh packet id to set in `reply_id`.

        Returns
        -------
        mesh_pb2.MeshPacket
            The packet object that was enqueued for transmission.

        Notes
        -----
        Request-scoped wait/error bookkeeping is intentionally implemented in
        `_send_data_with_wait()` and is not part of the public `sendData()`
        contract.
        """
        legacy_wait_attr = LEGACY_UNSCOPED_WAIT_ATTR_BY_PORTNUM.get(portNum)
        if legacy_wait_attr is not None:
            self._clear_wait_error(
                legacy_wait_attr,
                request_id=None,
                clear_scoped=False,
            )
        return self._send_data_with_wait(
            data,
            destinationId=destinationId,
            portNum=portNum,
            wantAck=wantAck,
            wantResponse=wantResponse,
            onResponse=onResponse,
            onResponseAckPermitted=onResponseAckPermitted,
            channelIndex=channelIndex,
            hopLimit=hopLimit,
            pkiEncrypted=pkiEncrypted,
            publicKey=publicKey,
            priority=priority,
            replyId=replyId,
            response_wait_attr=None,
        )

    def _send_data_with_wait(  # pylint: disable=R0913
        self,
        data: PayloadData,
        destinationId: int | str = BROADCAST_ADDR,
        portNum: portnums_pb2.PortNum.ValueType = portnums_pb2.PortNum.PRIVATE_APP,
        *,
        wantAck: bool = False,
        wantResponse: bool = False,
        onResponse: Callable[[dict[str, Any]], Any] | None = None,
        onResponseAckPermitted: bool = False,
        channelIndex: int = 0,
        hopLimit: int | None = None,
        pkiEncrypted: bool = False,
        publicKey: bytes | None = None,
        priority: mesh_pb2.MeshPacket.Priority.ValueType = mesh_pb2.MeshPacket.Priority.RELIABLE,
        replyId: int | None = None,
        response_wait_attr: str | None = None,
    ) -> mesh_pb2.MeshPacket:
        """Send payload data while optionally pre-registering request-scoped wait bookkeeping.

        Serializes protobuf payloads when provided, validates payload size and port number,
        assigns a packet id, registers an optional response handler, and transmits the packet.

        Parameters
        ----------
        data : Any
            Payload to send; protobuf messages will be serialized to bytes.
        destinationId : int | str
            Destination node identifier (node id string, numeric node number, BROADCAST_ADDR, or LOCAL_ADDR). (Default value = BROADCAST_ADDR)
        portNum : portnums_pb2.PortNum.ValueType
            Application port number for the payload; must not be UNKNOWN_APP. (Default value = portnums_pb2.PortNum.PRIVATE_APP)
        wantAck : bool
            Request transport-level acknowledgement/retry behavior. (Default value = False)
        wantResponse : bool
            Request an application-layer response from the recipient. (Default value = False)
        onResponse : Callable[[dict[str, Any]], Any] | None
            Optional callback invoked with the response
            packet dict when a response (or permitted ACK/NAK) arrives. (Default value = None)
        onResponseAckPermitted : bool
            If True, invoke onResponse for regular ACKs as well as data responses and NAKs. (Default value = False)
        channelIndex : int
            Channel index to send on. (Default value = 0)
        hopLimit : int | None
            Optional maximum hop count for the packet; None uses the default.
        pkiEncrypted : bool
            If True, request PKI encryption for the packet. (Default value = False)
        publicKey : bytes | None
            Public key to use for PKI encryption when pkiEncrypted is True. (Default value = None)
        priority : mesh_pb2.MeshPacket.Priority.ValueType
            Packet priority enum value to assign to the packet. (Default value = mesh_pb2.MeshPacket.Priority.RELIABLE)
        replyId : int | None
            If provided, marks this packet as a reply to the given message id. (Default value = None)
        response_wait_attr : str | None
            Optional acknowledgment attribute name used to scope wait/error
            state to this packet id before send (for example
            "receivedTelemetry"). (Default value = None)

        Returns
        -------
        mesh_pb2.MeshPacket
            The transmitted MeshPacket with its `id` field populated.

        Raises
        ------
        MeshInterface.MeshInterfaceError
            If the payload exceeds the maximum allowed size, a valid
            packet id cannot be generated, or if an invalid port number is supplied.
        """
        serializer = getattr(data, "SerializeToString", None)
        payload: bytes | bytearray | memoryview
        if callable(serializer):
            logger.debug("Serializing protobuf as data: %s", stripnl(data))
            payload = cast(bytes, serializer())
        else:
            payload = cast(bytes | bytearray | memoryview, data)
        if isinstance(payload, memoryview):
            payload = payload.tobytes()
        elif isinstance(payload, bytearray):
            payload = bytes(payload)

        logger.debug("len(data): %s", len(payload))
        logger.debug(
            "mesh_pb2.Constants.DATA_PAYLOAD_LEN: %s",
            mesh_pb2.Constants.DATA_PAYLOAD_LEN,
        )
        if len(payload) > mesh_pb2.Constants.DATA_PAYLOAD_LEN:
            raise MeshInterface.MeshInterfaceError("Data payload too big")

        if (
            portNum == portnums_pb2.PortNum.UNKNOWN_APP
        ):  # we are now more strict wrt port numbers
            raise MeshInterface.MeshInterfaceError(
                "A non-zero port number must be specified"
            )

        meshPacket = mesh_pb2.MeshPacket()
        meshPacket.channel = channelIndex
        meshPacket.decoded.payload = payload
        meshPacket.decoded.portnum = portNum
        meshPacket.decoded.want_response = wantResponse
        meshPacket.id = self._generate_packet_id()
        # Packet id 0 is reserved as an unset/unknown sentinel in wait paths.
        while meshPacket.id == 0:
            meshPacket.id = self._generate_packet_id()
        if replyId is not None:
            meshPacket.decoded.reply_id = replyId
        if priority is not None:
            meshPacket.priority = priority

        if response_wait_attr is not None:
            self._clear_wait_error(response_wait_attr, request_id=meshPacket.id)

        if onResponse is not None:
            logger.debug("Setting a response handler for requestId %s", meshPacket.id)
            self._add_response_handler(
                meshPacket.id, onResponse, ackPermitted=onResponseAckPermitted
            )
        try:
            return self._send_packet(
                meshPacket,
                destinationId,
                wantAck=wantAck,
                hopLimit=hopLimit,
                pkiEncrypted=pkiEncrypted,
                publicKey=publicKey,
            )
        except Exception:
            if response_wait_attr is not None:
                self._retire_wait_request(
                    response_wait_attr,
                    request_id=meshPacket.id,
                )
            elif onResponse is not None:
                self._request_wait_runtime.drop_response_handler(meshPacket.id)
            raise

    def sendPosition(
        self,
        latitude: float = 0.0,
        longitude: float = 0.0,
        altitude: int = 0,
        destinationId: int | str = BROADCAST_ADDR,
        wantAck: bool = False,
        wantResponse: bool = False,
        channelIndex: int = 0,
        hopLimit: int | None = None,
    ) -> mesh_pb2.MeshPacket:
        """Send the device's position to a specific node or to broadcast.

        Parameters
        ----------
        latitude : float
            Latitude in degrees; if 0.0 the latitude field is omitted. (Default value = 0.0)
        longitude : float
            Longitude in degrees; if 0.0 the longitude field is omitted. (Default value = 0.0)
        altitude : int
            Altitude in meters; if 0 the altitude field is omitted. (Default value = 0)
        destinationId : int | str
            Destination address or node ID; defaults to broadcast.
        wantAck : bool
            Request an acknowledgment from the recipient. (Default value = False)
        wantResponse : bool
            If True, blocks until a position response is received. (Default value = False)
        channelIndex : int
            Channel index to send the packet on. (Default value = 0)
        hopLimit : int | None
            Optional hop limit override for the outgoing packet. (Default value = None)

        Returns
        -------
        mesh_pb2.MeshPacket
            The sent packet with its `id` populated.
        """
        p = mesh_pb2.Position()
        if latitude != 0.0:
            p.latitude_i = int(latitude / 1e-7)
            logger.debug("p.latitude_i:%s", p.latitude_i)

        if longitude != 0.0:
            p.longitude_i = int(longitude / 1e-7)
            logger.debug("p.longitude_i:%s", p.longitude_i)

        if altitude != 0:
            p.altitude = int(altitude)
            logger.debug("p.altitude:%s", p.altitude)

        if wantResponse:
            onResponse = self.onResponsePosition
            response_wait_attr = WAIT_ATTR_POSITION
        else:
            onResponse = None
            response_wait_attr = None

        d = self._send_data_with_wait(
            p,
            destinationId,
            portNum=portnums_pb2.PortNum.POSITION_APP,
            wantAck=wantAck,
            wantResponse=wantResponse,
            onResponse=onResponse,
            channelIndex=channelIndex,
            hopLimit=hopLimit,
            response_wait_attr=response_wait_attr,
        )
        if wantResponse:
            request_id = self._extract_request_id_from_sent_packet(d)
            if request_id is None:
                raise self.MeshInterfaceError(RESPONSE_WAIT_REQID_ERROR)
            self.waitForPosition(request_id=request_id)
        return d

    @staticmethod
    def _extract_request_id_from_packet(packet: dict[str, Any]) -> int | None:
        """Return decoded requestId as an int when present and valid."""
        decoded = packet.get("decoded")
        if not isinstance(decoded, dict):
            return None
        raw_request_id = decoded.get("requestId")
        if isinstance(raw_request_id, bool):
            return None
        if isinstance(raw_request_id, int):
            return raw_request_id if raw_request_id > 0 else None
        if isinstance(raw_request_id, str) and raw_request_id.isdigit():
            parsed_request_id = int(raw_request_id)
            return parsed_request_id if parsed_request_id > 0 else None
        return None

    @staticmethod
    def _extract_request_id_from_sent_packet(packet: object) -> int | None:
        """Return sent packet id when present and positive."""
        raw_packet_id = getattr(packet, "id", None)
        if isinstance(raw_packet_id, bool) or not isinstance(raw_packet_id, int):
            return None
        return raw_packet_id if raw_packet_id > 0 else None

    def _clear_wait_error(
        self,
        acknowledgment_attr: str,
        request_id: int | None = None,
        *,
        clear_scoped: bool = True,
    ) -> None:
        """Clear wait error state for an attribute and optional request id."""
        self._request_wait_runtime.clear_wait_error(
            acknowledgment_attr,
            request_id=request_id,
            clear_scoped=clear_scoped,
        )

    def _prune_retired_wait_request_ids_locked(
        self, acknowledgment_attr: str
    ) -> dict[int, float]:
        """Prune expired retired request ids for a wait attribute.

        Notes
        -----
        Must be called while holding `_response_handlers_lock`.
        """
        return self._request_wait_runtime.prune_retired_wait_request_ids_locked(
            acknowledgment_attr
        )

    def _set_wait_error(
        self,
        acknowledgment_attr: str,
        message: str,
        *,
        request_id: int | None = None,
    ) -> None:
        """Record a wait error and wake the matching waiter."""
        self._request_wait_runtime.set_wait_error(
            acknowledgment_attr,
            message,
            request_id=request_id,
        )

    def _mark_wait_acknowledged(
        self, acknowledgment_attr: str, *, request_id: int | None = None
    ) -> None:
        """Set acknowledgment flag for the matching request scope."""
        self._request_wait_runtime.mark_wait_acknowledged(
            acknowledgment_attr,
            request_id=request_id,
        )

    def _raise_wait_error_if_present(
        self, acknowledgment_attr: str, request_id: int | None = None
    ) -> None:
        """Raise and clear any pending wait error for the given wait scope."""
        self._request_wait_runtime.raise_wait_error_if_present(
            acknowledgment_attr,
            request_id=request_id,
            error_factory=self.MeshInterfaceError,
        )

    def _retire_wait_request(
        self, acknowledgment_attr: str, request_id: int | None = None
    ) -> None:
        """Retire response handler and wait bookkeeping for a completed wait."""
        self._request_wait_runtime.retire_wait_request(
            acknowledgment_attr,
            request_id=request_id,
        )

    def _wait_for_request_ack(
        self,
        acknowledgment_attr: str,
        request_id: int,
        *,
        timeout_seconds: float,
    ) -> bool:
        """Wait for a request-scoped acknowledgment flag.

        Parameters
        ----------
        acknowledgment_attr : str
            Acknowledgment attribute namespace (for example, "receivedTelemetry").
        request_id : int
            Packet request id that must acknowledge before timeout.
        timeout_seconds : float
            Maximum seconds to wait.

        Returns
        -------
        bool
            `True` when the scoped acknowledgment was observed before timeout,
            otherwise `False`.
        """
        return self._request_wait_runtime.wait_for_request_ack(
            acknowledgment_attr,
            request_id,
            timeout_seconds=timeout_seconds,
        )

    def _record_routing_wait_error(
        self,
        *,
        acknowledgment_attr: str,
        routing_error_reason: str | None,
        request_id: int | None = None,
    ) -> None:
        """Record non-success routing responses into shared wait state."""
        self._request_wait_runtime.record_routing_wait_error(
            acknowledgment_attr=acknowledgment_attr,
            routing_error_reason=routing_error_reason,
            request_id=request_id,
        )

    def onResponsePosition(self, p: dict[str, Any]) -> None:
        """Process a position response packet and emit a concise human-readable summary.

        Marks the interface's position acknowledgment as received, parses the Position
        protobuf from the packet payload, and emits latitude/longitude (degrees),
        altitude (meters) when present, and precision information. When INFO logging
        is configured, the summary is logged; otherwise it is printed to stdout for
        backward compatibility. Routing replies are recorded into shared wait
        state so waiters can surface routing failures consistently.

        Parameters
        ----------
        p : dict[str, Any]
            Decoded packet dictionary expected to contain at minimum
            `decoded["portnum"]` and `decoded["payload"]`. For routing error checks
            the nested `decoded["routing"]["errorReason"]` may be present.

        """
        request_id = self._extract_request_id_from_packet(p)
        if p["decoded"]["portnum"] == portnums_pb2.PortNum.Name(
            portnums_pb2.PortNum.POSITION_APP
        ):
            position = mesh_pb2.Position()
            try:
                position.ParseFromString(p["decoded"]["payload"])
            except (KeyError, TypeError, protobuf_message.DecodeError) as exc:
                self._set_wait_error(
                    WAIT_ATTR_POSITION,
                    f"Failed to parse position response payload: {exc}",
                    request_id=request_id,
                )
                return

            ret = "Position received: "
            if position.latitude_i != 0 and position.longitude_i != 0:
                ret += (
                    f"({position.latitude_i * 10**-7}, {position.longitude_i * 10**-7})"
                )
            else:
                ret += "(unknown)"
            if position.altitude != 0:
                ret += f" {position.altitude}m"

            if position.precision_bits not in (0, 32):
                ret += f" precision:{position.precision_bits}"
            elif position.precision_bits == 32:
                ret += " full precision"
            elif position.precision_bits == 0:
                ret += " position disabled"

            _emit_response_summary(ret)
            self._mark_wait_acknowledged(
                WAIT_ATTR_POSITION,
                request_id=request_id,
            )

        elif p["decoded"]["portnum"] == portnums_pb2.PortNum.Name(
            portnums_pb2.PortNum.ROUTING_APP
        ):
            self._record_routing_wait_error(
                acknowledgment_attr=WAIT_ATTR_POSITION,
                routing_error_reason=p["decoded"].get("routing", {}).get("errorReason"),
                request_id=request_id,
            )

    def sendTraceRoute(
        self, dest: int | str, hopLimit: int, channelIndex: int = 0
    ) -> None:
        """Initiate a traceroute request toward a destination node and wait for responses.

        Sends a RouteDiscovery to the specified destination using the given channel and waits for
        traceroute responses; the waiting period is extended based on the current node count up to
        the provided hopLimit.

        Parameters
        ----------
        dest : int | str
            Destination node (numeric node number, node ID string, or broadcast/local constants).
        hopLimit : int
            Maximum number of hops to probe for the traceroute.
        channelIndex : int
            Channel index to use for transmission. (Default value = 0)

        Raises
        ------
        MeshInterfaceError
            If waiting for traceroute responses times out or the operation fails.
        """
        r = mesh_pb2.RouteDiscovery()
        packet = self._send_data_with_wait(
            r,
            destinationId=dest,
            portNum=portnums_pb2.PortNum.TRACEROUTE_APP,
            wantResponse=True,
            onResponse=self.onResponseTraceRoute,
            channelIndex=channelIndex,
            hopLimit=hopLimit,
            response_wait_attr=WAIT_ATTR_TRACEROUTE,
        )
        # extend timeout based on number of nodes, limit by configured hopLimit
        with self._node_db_lock:
            node_count = len(self.nodes) if self.nodes else 0
        nodes_based_factor = (node_count - 1) if node_count else (hopLimit + 1)
        waitFactor = max(1, min(nodes_based_factor, hopLimit + 1))
        request_id = self._extract_request_id_from_sent_packet(packet)
        if request_id is None:
            raise self.MeshInterfaceError(RESPONSE_WAIT_REQID_ERROR)
        self.waitForTraceRoute(waitFactor, request_id=request_id)

    def onResponseTraceRoute(self, p: dict[str, Any]) -> None:
        """Emit human-readable traceroute results from a RouteDiscovery payload.

        Parses the RouteDiscovery protobuf found in p["decoded"]["payload"], emits a
        forward route from the response destination back to the origin and, when
        present, the traced return route back to this node. When INFO logging is
        configured, the summaries are logged; otherwise they are printed to stdout
        for backward compatibility.

        Parameters
        ----------
        p : dict[str, Any]
            The traceroute response packet.

        Notes
        -----
        Emits formatted route strings and acknowledges waits via
        `_mark_wait_acknowledged(WAIT_ATTR_TRACEROUTE, request_id=request_id)`.
        For legacy unscoped callers, acknowledgement falls back to
        `self._acknowledgment.receivedTraceRoute`.
        """
        decoded = p["decoded"]
        request_id = self._extract_request_id_from_packet(p)
        if decoded.get("portnum") == portnums_pb2.PortNum.Name(
            portnums_pb2.PortNum.ROUTING_APP
        ):
            self._record_routing_wait_error(
                acknowledgment_attr=WAIT_ATTR_TRACEROUTE,
                routing_error_reason=decoded.get("routing", {}).get("errorReason"),
                request_id=request_id,
            )
            return

        routeDiscovery = mesh_pb2.RouteDiscovery()
        try:
            routeDiscovery.ParseFromString(decoded["payload"])
            asDict = google.protobuf.json_format.MessageToDict(routeDiscovery)
        except (protobuf_message.DecodeError, KeyError, TypeError) as exc:
            self._set_wait_error(
                WAIT_ATTR_TRACEROUTE,
                f"Failed to parse traceroute response payload: {exc}",
                request_id=request_id,
            )
            return

        def _node_label(node_num: int) -> str:
            """Return a human-readable identifier for a numeric node.

            Parameters
            ----------
            node_num : int
                Numeric node identifier.

            Returns
            -------
            str
                The node's ID string if known; otherwise the node number formatted as an 8-digit lowercase hexadecimal string.
            """
            return self._node_num_to_id(node_num, False) or f"{node_num:08x}"

        def _format_snr(snr_value: int | None) -> str:
            """Format an SNR value encoded in quarter-dB units into a human-readable string.

            Parameters
            ----------
            snr_value : int | None
                SNR expressed as an integer number of quarter dB units,
                or `None`/`UNKNOWN_SNR_QUARTER_DB` to indicate an unknown value.

            Returns
            -------
            str
                Formatted SNR in dB as a string (for example, "7.5"), or "?" when the SNR is unknown.
            """
            return (
                str(snr_value / 4)
                if snr_value is not None and snr_value != UNKNOWN_SNR_QUARTER_DB
                else "?"
            )

        def _append_hop(route_text: str, node_num: int, snr_text: str) -> str:
            """Append a hop description to an existing route string.

            Parameters
            ----------
            route_text : str
                Current route representation.
            node_num : int
                Numeric node identifier to convert to a human-readable label.
            snr_text : str
                Signal-to-noise ratio value as text (without units).

            Returns
            -------
            str
                The route string with " --> <node_label> (<snr_text>dB)" appended.
            """
            return f"{route_text} --> {_node_label(node_num)} ({snr_text}dB)"

        route_towards = asDict.get("route", [])
        snr_towards = asDict.get("snrTowards", [])
        snr_towards_valid = len(snr_towards) == len(route_towards) + 1

        _emit_response_summary("Route traced towards destination:")
        routeStr = _node_label(p["to"])  # Start with destination of response
        for idx, nodeNum in enumerate(route_towards):  # Add intermediate hops
            hop_snr = _format_snr(snr_towards[idx]) if snr_towards_valid else "?"
            routeStr = _append_hop(routeStr, nodeNum, hop_snr)
        final_towards_snr = _format_snr(snr_towards[-1]) if snr_towards_valid else "?"
        routeStr = _append_hop(routeStr, p["from"], final_towards_snr)

        _emit_response_summary(routeStr)

        # Only if hopStart is set and there is an SNR entry (for the origin) it's valid, even though route might be empty (direct connection)
        route_back = asDict.get("routeBack", [])
        snr_back = asDict.get("snrBack", [])
        backValid = "hopStart" in p and len(snr_back) == len(route_back) + 1
        if backValid:
            _emit_response_summary("Route traced back to us:")
            routeStr = _node_label(p["from"])  # Start with origin of response
            for idx, nodeNum in enumerate(route_back):  # Add intermediate hops
                routeStr = _append_hop(routeStr, nodeNum, _format_snr(snr_back[idx]))
            routeStr = _append_hop(routeStr, p["to"], _format_snr(snr_back[-1]))
            _emit_response_summary(routeStr)

        self._mark_wait_acknowledged(
            WAIT_ATTR_TRACEROUTE,
            request_id=request_id,
        )

    def sendTelemetry(
        self,
        destinationId: int | str = BROADCAST_ADDR,
        wantResponse: bool = False,
        channelIndex: int = 0,
        telemetryType: TelemetryType | str = DEFAULT_TELEMETRY_TYPE,
        hopLimit: int | None = None,
    ) -> None:
        """Send a telemetry message to a node or broadcast and optionally wait for a telemetry response.

        Parameters
        ----------
        destinationId : int | str
            Numeric node id, node id string, or the broadcast address to receive the telemetry. (Default value = BROADCAST_ADDR)
        wantResponse : bool
            If true, register a telemetry response handler and wait for the corresponding response. (Default value = False)
        channelIndex : int
            Channel index to use for the outgoing packet. (Default value = 0)
        telemetryType : str
            Telemetry payload to send. Supported values: "environment_metrics", "air_quality_metrics",
            "power_metrics", "local_stats", and "device_metrics". When "device_metrics" is selected and local device
            metrics are available, the payload is populated from the local node's cached device metrics. (Default value = 'device_metrics')
        hopLimit : int | None
            Optional hop limit override for the outgoing packet. (Default value = None)
        """
        telemetry_type = telemetryType
        if telemetry_type not in VALID_TELEMETRY_TYPE_SET:
            # Backwards compatibility: unknown types fall back to device_metrics with deprecation warning
            logger.warning(
                "Unsupported telemetryType '%s' is deprecated. "
                "Supported values: %s. "
                "Falling back to '%s'. "
                "This will raise an error in a future version.",
                telemetry_type,
                sorted(VALID_TELEMETRY_TYPES),
                DEFAULT_TELEMETRY_TYPE,
                stacklevel=2,
            )
            telemetry_type = DEFAULT_TELEMETRY_TYPE
        r = telemetry_pb2.Telemetry()

        if telemetry_type == "environment_metrics":
            r.environment_metrics.CopyFrom(telemetry_pb2.EnvironmentMetrics())
        elif telemetry_type == "air_quality_metrics":
            r.air_quality_metrics.CopyFrom(telemetry_pb2.AirQualityMetrics())
        elif telemetry_type == "power_metrics":
            r.power_metrics.CopyFrom(telemetry_pb2.PowerMetrics())
        elif telemetry_type == "local_stats":
            r.local_stats.CopyFrom(telemetry_pb2.LocalStats())
        elif telemetry_type == DEFAULT_TELEMETRY_TYPE:
            with self._node_db_lock:
                node = (
                    self.nodesByNum.get(self.localNode.nodeNum)
                    if self.nodesByNum is not None
                    else None
                )
                if node is not None:
                    metrics = node.get("deviceMetrics")
                    if metrics:
                        batteryLevel = metrics.get("batteryLevel")
                        if batteryLevel is not None:
                            r.device_metrics.battery_level = batteryLevel
                        voltage = metrics.get("voltage")
                        if voltage is not None:
                            r.device_metrics.voltage = voltage
                        channel_utilization = metrics.get("channelUtilization")
                        if channel_utilization is not None:
                            r.device_metrics.channel_utilization = channel_utilization
                        air_util_tx = metrics.get("airUtilTx")
                        if air_util_tx is not None:
                            r.device_metrics.air_util_tx = air_util_tx
                        uptime_seconds = metrics.get("uptimeSeconds")
                        if uptime_seconds is not None:
                            r.device_metrics.uptime_seconds = uptime_seconds

        if wantResponse:
            onResponse = self.onResponseTelemetry
            response_wait_attr = WAIT_ATTR_TELEMETRY
        else:
            onResponse = None
            response_wait_attr = None

        packet = self._send_data_with_wait(
            r,
            destinationId=destinationId,
            portNum=portnums_pb2.PortNum.TELEMETRY_APP,
            wantResponse=wantResponse,
            onResponse=onResponse,
            channelIndex=channelIndex,
            hopLimit=hopLimit,
            response_wait_attr=response_wait_attr,
        )
        if wantResponse:
            request_id = self._extract_request_id_from_sent_packet(packet)
            if request_id is None:
                raise self.MeshInterfaceError(RESPONSE_WAIT_REQID_ERROR)
            self.waitForTelemetry(request_id=request_id)

    def onResponseTelemetry(self, p: dict[str, Any]) -> None:
        """Handle an incoming telemetry response: mark telemetry as received and emit human-readable telemetry values.

        This inspects p["decoded"]["portnum"] and:
        - For TELEMETRY_APP: parses the Telemetry payload, sets the telemetry-received flag on the interface,
          and emits device metrics (battery, voltage, channel/air utilization, uptime) when present;
          for non-device_metrics telemetry, emits top-level keys and their subfields except the protobuf 'time' field.
          When INFO logging is configured, the summaries are logged; otherwise they are
          printed to stdout for backward compatibility.
        - For ROUTING_APP: records routing failures into shared wait state so
          `waitForTelemetry()` can surface the failure to callers.

        Parameters
        ----------
        p : dict[str, Any]
            Decoded packet dictionary produced by _handle_packet_from_radio. Must contain "decoded"
            with a "portnum" string and either a "payload" (Telemetry protobuf bytes) for TELEMETRY_APP
            or a "routing" mapping for ROUTING_APP.

        """
        request_id = self._extract_request_id_from_packet(p)
        if p["decoded"]["portnum"] == portnums_pb2.PortNum.Name(
            portnums_pb2.PortNum.TELEMETRY_APP
        ):
            telemetry = telemetry_pb2.Telemetry()
            try:
                telemetry.ParseFromString(p["decoded"]["payload"])
            except (KeyError, TypeError, protobuf_message.DecodeError) as exc:
                self._set_wait_error(
                    WAIT_ATTR_TELEMETRY,
                    f"Failed to parse telemetry response payload: {exc}",
                    request_id=request_id,
                )
                return
            try:
                _emit_response_summary("Telemetry received:")
                # Check if the telemetry message has the device_metrics field
                # This is the original code that was the default for --request-telemetry and is kept for compatibility
                if telemetry.HasField("device_metrics"):
                    device_metrics_fields = {
                        field.name for field, _ in telemetry.device_metrics.ListFields()
                    }
                    if "battery_level" in device_metrics_fields:
                        _emit_response_summary(
                            f"Battery level: {telemetry.device_metrics.battery_level:.2f}%"
                        )
                    if "voltage" in device_metrics_fields:
                        _emit_response_summary(
                            f"Voltage: {telemetry.device_metrics.voltage:.2f} V"
                        )
                    if "channel_utilization" in device_metrics_fields:
                        _emit_response_summary(
                            "Total channel utilization: "
                            f"{telemetry.device_metrics.channel_utilization:.2f}%"
                        )
                    if "air_util_tx" in device_metrics_fields:
                        _emit_response_summary(
                            f"Transmit air utilization: {telemetry.device_metrics.air_util_tx:.2f}%"
                        )
                    if "uptime_seconds" in device_metrics_fields:
                        _emit_response_summary(
                            f"Uptime: {telemetry.device_metrics.uptime_seconds} s"
                        )
                else:
                    # this is the new code if --request-telemetry <type> is used.
                    telemetry_dict = google.protobuf.json_format.MessageToDict(
                        telemetry
                    )
                    for key, value in telemetry_dict.items():
                        if (
                            key != "time"
                        ):  # protobuf includes a time field that we don't emit for device_metrics.
                            if isinstance(value, dict):
                                _emit_response_summary(f"{key}:")
                                for sub_key, sub_value in value.items():
                                    _emit_response_summary(f"  {sub_key}: {sub_value}")
                            else:
                                _emit_response_summary(f"{key}: {value}")
            except Exception as exc:  # noqa: BLE001
                self._set_wait_error(
                    WAIT_ATTR_TELEMETRY,
                    f"Failed to format telemetry response: {exc}",
                    request_id=request_id,
                )
                return
            self._mark_wait_acknowledged(
                WAIT_ATTR_TELEMETRY,
                request_id=request_id,
            )

        elif p["decoded"]["portnum"] == portnums_pb2.PortNum.Name(
            portnums_pb2.PortNum.ROUTING_APP
        ):
            self._record_routing_wait_error(
                acknowledgment_attr=WAIT_ATTR_TELEMETRY,
                routing_error_reason=p["decoded"].get("routing", {}).get("errorReason"),
                request_id=request_id,
            )

    def onResponseWaypoint(self, p: dict[str, Any]) -> None:
        """Handle a waypoint response or routing error contained in a received packet.

        When the packet's port is WAYPOINT_APP, parse the Waypoint protobuf from
        decoded['payload'], mark the waypoint acknowledgment as received, and emit
        the waypoint. When INFO logging is configured, the summary is logged;
        otherwise it is printed to stdout for backward compatibility. Routing
        errors are recorded into shared wait state so `waitForWaypoint()`
        surfaces them directly.

        Parameters
        ----------
        p : dict[str, Any]
            Packet dictionary containing a 'decoded' mapping. Expected keys include
            'portnum' (str) and, for WAYPOINT_APP, 'payload' (bytes) with a serialized Waypoint.

        """
        request_id = self._extract_request_id_from_packet(p)
        if p["decoded"]["portnum"] == portnums_pb2.PortNum.Name(
            portnums_pb2.PortNum.WAYPOINT_APP
        ):
            w = mesh_pb2.Waypoint()
            try:
                w.ParseFromString(p["decoded"]["payload"])
            except (KeyError, TypeError, protobuf_message.DecodeError) as exc:
                self._set_wait_error(
                    WAIT_ATTR_WAYPOINT,
                    f"Failed to parse waypoint response payload: {exc}",
                    request_id=request_id,
                )
                return
            _emit_response_summary(f"Waypoint received: {w}")
            self._mark_wait_acknowledged(
                WAIT_ATTR_WAYPOINT,
                request_id=request_id,
            )
        elif p["decoded"]["portnum"] == portnums_pb2.PortNum.Name(
            portnums_pb2.PortNum.ROUTING_APP
        ):
            self._record_routing_wait_error(
                acknowledgment_attr=WAIT_ATTR_WAYPOINT,
                routing_error_reason=p["decoded"].get("routing", {}).get("errorReason"),
                request_id=request_id,
            )

    def sendWaypoint(  # pylint: disable=R0913
        self,
        name: str,
        description: str,
        icon: int | str,
        expire: int,
        waypoint_id: int | None = None,
        latitude: float = 0.0,
        longitude: float = 0.0,
        destinationId: int | str = BROADCAST_ADDR,
        wantAck: bool = True,
        wantResponse: bool = False,
        channelIndex: int = 0,
        hopLimit: int | None = None,
    ) -> mesh_pb2.MeshPacket:
        """Send a waypoint to a node or broadcast.

        Parameters
        ----------
        name : str
            Human-readable waypoint name.
        description : str
            Text description for the waypoint.
        icon : int | str
            Icon identifier (will be converted to an integer).
        expire : int
            Expiration time for the waypoint, in seconds.
        waypoint_id : int | None
            Waypoint identifier to use; if None a pseudo-random id is generated. (Default value = None)
        latitude : float
            Latitude in decimal degrees; included only when not 0.0. (Default value = 0.0)
        longitude : float
            Longitude in decimal degrees; included only when not 0.0. (Default value = 0.0)
        destinationId : int | str
            Destination node id or special address (broadcast/local). (Default value = BROADCAST_ADDR)
        wantAck : bool
            If True, request an acknowledgement for the sent packet. (Default value = True)
        wantResponse : bool
            If True, wait for and process a waypoint response before returning. (Default value = False)
        channelIndex : int
            Channel index to send the waypoint on. (Default value = 0)
        hopLimit : int | None
            Optional hop limit override for the outgoing packet. (Default value = None)

        Returns
        -------
        mesh_pb2.MeshPacket
            The MeshPacket that was sent; its `id` is populated for tracking.
        """
        w = mesh_pb2.Waypoint()
        w.name = name
        w.description = description
        w.icon = int(icon)
        w.expire = expire
        if waypoint_id is None:
            # Generate a waypoint's id, NOT a packet ID.
            # same algorithm as https://github.com/meshtastic/js/blob/715e35d2374276a43ffa93c628e3710875d43907/src/meshDevice.ts#L791
            seed = secrets.randbits(32)
            w.id = math.floor(seed * math.pow(2, -32) * 1e9)
            logger.debug("w.id:%s", w.id)
        else:
            w.id = waypoint_id
        if latitude != 0.0:
            w.latitude_i = int(latitude * 1e7)
            logger.debug("w.latitude_i:%s", w.latitude_i)
        if longitude != 0.0:
            w.longitude_i = int(longitude * 1e7)
            logger.debug("w.longitude_i:%s", w.longitude_i)

        if wantResponse:
            onResponse = self.onResponseWaypoint
            response_wait_attr = WAIT_ATTR_WAYPOINT
        else:
            onResponse = None
            response_wait_attr = None

        d = self._send_data_with_wait(
            w,
            destinationId,
            portNum=portnums_pb2.PortNum.WAYPOINT_APP,
            wantAck=wantAck,
            wantResponse=wantResponse,
            onResponse=onResponse,
            channelIndex=channelIndex,
            hopLimit=hopLimit,
            response_wait_attr=response_wait_attr,
        )
        if wantResponse:
            request_id = self._extract_request_id_from_sent_packet(d)
            if request_id is None:
                raise self.MeshInterfaceError(RESPONSE_WAIT_REQID_ERROR)
            self.waitForWaypoint(request_id=request_id)
        return d

    def deleteWaypoint(
        self,
        waypoint_id: int,
        destinationId: int | str = BROADCAST_ADDR,
        wantAck: bool = True,
        wantResponse: bool = False,
        channelIndex: int = 0,
        hopLimit: int | None = None,
    ) -> mesh_pb2.MeshPacket:
        """Delete a waypoint by sending a Waypoint message with expire=0 to a destination.

        Parameters
        ----------
        waypoint_id : int
            The waypoint's identifier to delete (the waypoint id, not a packet id).
        destinationId : int | str
            Destination node numeric id or address string; defaults to broadcast.
        wantAck : bool
            Request an acknowledgement for the transmitted packet when True. (Default value = True)
        wantResponse : bool
            If True, wait for and process a waypoint response before returning. (Default value = False)
        channelIndex : int
            Channel index to send the packet on. (Default value = 0)
        hopLimit : int | None
            Optional hop limit override for the outgoing packet. (Default value = None)

        Returns
        -------
        mesh_pb2.MeshPacket
            The MeshPacket that was sent; its `id` field is populated and can be used to track acknowledgements.
        """
        p = mesh_pb2.Waypoint()
        p.id = waypoint_id
        p.expire = 0

        if wantResponse:
            onResponse = self.onResponseWaypoint
            response_wait_attr = WAIT_ATTR_WAYPOINT
        else:
            onResponse = None
            response_wait_attr = None

        d = self._send_data_with_wait(
            p,
            destinationId,
            portNum=portnums_pb2.PortNum.WAYPOINT_APP,
            wantAck=wantAck,
            wantResponse=wantResponse,
            onResponse=onResponse,
            channelIndex=channelIndex,
            hopLimit=hopLimit,
            response_wait_attr=response_wait_attr,
        )
        if wantResponse:
            request_id = self._extract_request_id_from_sent_packet(d)
            if request_id is None:
                raise self.MeshInterfaceError(RESPONSE_WAIT_REQID_ERROR)
            self.waitForWaypoint(request_id=request_id)
        return d

    def _add_response_handler(
        self,
        requestId: int,
        callback: Callable[[dict[str, Any]], Any],
        ackPermitted: bool = False,
    ) -> None:
        """Register a response callback for a specific request identifier.

        Parameters
        ----------
        requestId : int
            Request identifier used to match incoming responses to this handler.
        callback : Callable[[dict[str, Any]], Any]
            Function called with the decoded response packet
            dictionary when a matching response is received.
        ackPermitted : bool
            If True, allow acknowledgement/negative-acknowledgement packets
            to invoke the callback; if False, only full responses will invoke it. (Default value = False)
        """
        self._request_wait_runtime.add_response_handler(
            requestId,
            callback,
            ack_permitted=ackPermitted,
        )

    def _send_packet(
        self,
        meshPacket: mesh_pb2.MeshPacket,
        destinationId: int | str = BROADCAST_ADDR,
        wantAck: bool = False,
        hopLimit: int | None = None,
        pkiEncrypted: bool | None = False,
        publicKey: bytes | None = None,
    ) -> mesh_pb2.MeshPacket:
        """Send a MeshPacket to a specific node or broadcast.

        Parameters
        ----------
        meshPacket : mesh_pb2.MeshPacket
            The packet to send; fields such as payload and port should be set by the caller.
        destinationId : int | str
            Destination identifier — can be a node
            number (int), a node ID string (hex-style), or the constants
            BROADCAST_ADDR or LOCAL_ADDR. Determines the packet's `to`
            field. (Default value = BROADCAST_ADDR)
        wantAck : bool
            If true, request an acknowledgement from the recipient; sets the packet's `want_ack`. (Default value = False)
        hopLimit : int | None
            Maximum hop count for the packet. If omitted, the local node's LoRa `hop_limit` is used. (Default value = None)
        pkiEncrypted : bool | None
            If true, mark the packet as PKI-encrypted. (Default value = False)
        publicKey : bytes | None
            Optional public key to include on the packet. (Default value = None)

        Returns
        -------
        mesh_pb2.MeshPacket
            The same MeshPacket instance that was sent,
            with transport-related fields populated (for example: `to`,
            `want_ack`, `hop_limit`, `id`, and encryption/public key
            fields).

        Raises
        ------
        MeshInterface.MeshInterfaceError
            If the destination node is not found in the node database or if no info is available.
        """

        with self._node_db_lock:
            my_node_num = self.myInfo.my_node_num if self.myInfo is not None else None

        # We allow users to talk to the local node before we've completed the full connection flow...
        if my_node_num is not None and destinationId != my_node_num:
            self._wait_connected()

        toRadio = mesh_pb2.ToRadio()

        nodeNum: int = 0
        if destinationId is None:
            raise MeshInterface.MeshInterfaceError("destinationId must not be None")
        elif isinstance(destinationId, int):
            nodeNum = destinationId
        elif destinationId == BROADCAST_ADDR:
            nodeNum = BROADCAST_NUM
        elif destinationId == LOCAL_ADDR:
            if my_node_num is not None:
                nodeNum = my_node_num
            else:
                raise MeshInterface.MeshInterfaceError("No myInfo found.")
        elif isinstance(destinationId, str):
            # A simple hex style nodeid - we can parse this without needing the DB.
            compact_hex_body = _extract_hex_node_id_body(destinationId)
            if compact_hex_body is not None:
                nodeNum = int(compact_hex_body, 16)
            else:
                with self._node_db_lock:
                    node = self.nodes.get(destinationId) if self.nodes else None
                    has_nodes = self.nodes is not None
                    node_found = node is not None
                    node_num = node.get("num") if isinstance(node, dict) else None
                if node_found:
                    if isinstance(node_num, int):
                        nodeNum = node_num
                    else:
                        raise MeshInterface.MeshInterfaceError(
                            _format_missing_node_num_error(destinationId)
                        )
                elif has_nodes:
                    raise MeshInterface.MeshInterfaceError(
                        _format_node_not_found_in_db_error(destinationId)
                    )
                else:
                    raise MeshInterface.MeshInterfaceError(
                        _format_node_db_unavailable_error(destinationId)
                    )
        else:
            with self._node_db_lock:
                has_nodes = self.nodes is not None
            if has_nodes:
                raise MeshInterface.MeshInterfaceError(
                    _format_node_not_found_in_db_error(destinationId)
                )
            raise MeshInterface.MeshInterfaceError(
                _format_node_db_unavailable_error(destinationId)
            )

        meshPacket.to = nodeNum
        meshPacket.want_ack = wantAck

        if hopLimit is not None:
            meshPacket.hop_limit = hopLimit
        else:
            with self._node_db_lock:
                default_hop_limit = self.localNode.localConfig.lora.hop_limit
            meshPacket.hop_limit = default_hop_limit

        if pkiEncrypted:
            meshPacket.pki_encrypted = True

        if publicKey is not None:
            meshPacket.public_key = publicKey

        # if the user hasn't set an ID for this packet (likely and recommended),
        # we should pick a new unique ID so the message can be tracked.
        if meshPacket.id == 0:
            meshPacket.id = self._generate_packet_id()

        toRadio.packet.CopyFrom(meshPacket)
        if self.noProto:
            logger.warning(
                "Not sending packet because protocol use is disabled by noProto"
            )
        else:
            logger.debug("Sending packet: %s", stripnl(meshPacket))
            self._send_to_radio(toRadio)
        return meshPacket

    # COMPAT_STABLE_SHIM: historical private camelCase helper used by external integrations.
    def _sendPacket(
        self,
        meshPacket: mesh_pb2.MeshPacket,
        destinationId: int | str = BROADCAST_ADDR,
        wantAck: bool = False,
        hopLimit: int | None = None,
        pkiEncrypted: bool | None = False,
        publicKey: bytes | None = None,
    ) -> mesh_pb2.MeshPacket:
        """Backward-compatible alias for `_send_packet`."""
        return self._send_packet(
            meshPacket=meshPacket,
            destinationId=destinationId,
            wantAck=wantAck,
            hopLimit=hopLimit,
            pkiEncrypted=pkiEncrypted,
            publicKey=publicKey,
        )

    def waitForConfig(self) -> None:
        """Block until the radio configuration and the local node's configuration are available.

        Raises
        ------
        MeshInterface.MeshInterfaceError
            If the configuration is not received before the interface timeout.
        """
        success = (
            self._timeout.waitForSet(self, attrs=("myInfo", "nodes"))
            and self.localNode.waitForConfig()
        )
        if not success:
            raise MeshInterface.MeshInterfaceError(
                "Timed out waiting for interface config"
            )

    def waitForAckNak(self) -> None:
        """Wait until an acknowledgement (ACK) or negative acknowledgement (NAK) is received or the wait times out.

        Raises
        ------
        MeshInterface.MeshInterfaceError
            If waiting times out before an ACK/NAK is received.
        """
        success = self._timeout.waitForAckNak(self._acknowledgment)
        self._raise_wait_error_if_present(WAIT_ATTR_NAK)
        if not success:
            raise MeshInterface.MeshInterfaceError(
                "Timed out waiting for an acknowledgment"
            )

    def waitForTraceRoute(
        self, waitFactor: float, request_id: int | None = None
    ) -> None:
        """Wait for trace route completion using the configured timeout.

        Blocks until a trace route response is acknowledged or the configured timeout multiplied by waitFactor elapses.

        Parameters
        ----------
        waitFactor : float
            Multiplier applied to the base trace-route timeout to extend the wait period.
        request_id : int | None
            Optional request id used to scope wait/error handling to a specific
            traceroute request.

        Raises
        ------
        MeshInterface.MeshInterfaceError
            If the wait times out before a traceroute response is received.
        """
        try:
            if request_id is None:
                success = self._timeout.waitForTraceRoute(
                    waitFactor, self._acknowledgment
                )
            else:
                success = self._wait_for_request_ack(
                    WAIT_ATTR_TRACEROUTE,
                    request_id,
                    timeout_seconds=self._timeout.expireTimeout * waitFactor,
                )
            self._raise_wait_error_if_present(
                WAIT_ATTR_TRACEROUTE, request_id=request_id
            )
            if not success:
                raise MeshInterface.MeshInterfaceError(
                    "Timed out waiting for traceroute"
                )
        finally:
            self._retire_wait_request(WAIT_ATTR_TRACEROUTE, request_id=request_id)

    def waitForTelemetry(self, request_id: int | None = None) -> None:
        """Wait for a telemetry response or until the configured timeout elapses.

        Parameters
        ----------
        request_id : int | None
            Optional request id used to scope wait/error handling to a specific
            telemetry request.

        Raises
        ------
        MeshInterface.MeshInterfaceError
            If a telemetry response is not received before the configured timeout.
        """
        try:
            if request_id is None:
                success = self._timeout.waitForTelemetry(self._acknowledgment)
            else:
                success = self._wait_for_request_ack(
                    WAIT_ATTR_TELEMETRY,
                    request_id,
                    timeout_seconds=self._timeout.expireTimeout,
                )
            self._raise_wait_error_if_present(
                WAIT_ATTR_TELEMETRY, request_id=request_id
            )
            if not success:
                raise MeshInterface.MeshInterfaceError(
                    "Timed out waiting for telemetry"
                )
        finally:
            self._retire_wait_request(WAIT_ATTR_TELEMETRY, request_id=request_id)

    def waitForPosition(self, request_id: int | None = None) -> None:
        """Block until a position acknowledgment is received.

        Parameters
        ----------
        request_id : int | None
            Optional request id used to scope wait/error handling to a specific
            position request.

        Raises
        ------
        MeshInterface.MeshInterfaceError
            If waiting for the position times out.
        """
        try:
            if request_id is None:
                success = self._timeout.waitForPosition(self._acknowledgment)
            else:
                success = self._wait_for_request_ack(
                    WAIT_ATTR_POSITION,
                    request_id,
                    timeout_seconds=self._timeout.expireTimeout,
                )
            self._raise_wait_error_if_present(WAIT_ATTR_POSITION, request_id=request_id)
            if not success:
                raise MeshInterface.MeshInterfaceError("Timed out waiting for position")
        finally:
            self._retire_wait_request(WAIT_ATTR_POSITION, request_id=request_id)

    def waitForWaypoint(self, request_id: int | None = None) -> None:
        """Block until a waypoint acknowledgment is received.

        Waits for the internal waypoint acknowledgment event to be set; raises MeshInterface.MeshInterfaceError if the wait times out.

        Parameters
        ----------
        request_id : int | None
            Optional request id used to scope wait/error handling to a specific
            waypoint request.

        Raises
        ------
        MeshInterface.MeshInterfaceError
            If the wait times out before a waypoint acknowledgment is received.
        """
        try:
            if request_id is None:
                success = self._timeout.waitForWaypoint(self._acknowledgment)
            else:
                success = self._wait_for_request_ack(
                    WAIT_ATTR_WAYPOINT,
                    request_id,
                    timeout_seconds=self._timeout.expireTimeout,
                )
            self._raise_wait_error_if_present(WAIT_ATTR_WAYPOINT, request_id=request_id)
            if not success:
                raise MeshInterface.MeshInterfaceError("Timed out waiting for waypoint")
        finally:
            self._retire_wait_request(WAIT_ATTR_WAYPOINT, request_id=request_id)

    def getMyNodeInfo(self) -> dict[str, Any] | None:
        """Get the stored node-info dictionary for the local node.

        Returns
        -------
        dict[str, Any] | None
            The local node's node-info entry from `nodesByNum`, or `None` if `myInfo`
            or `nodesByNum` is unset or the local node entry is missing.
        """
        with self._node_db_lock:
            if self.myInfo is None or self.nodesByNum is None:
                return None
            return self.nodesByNum.get(self.myInfo.my_node_num)

    def getMyUser(self) -> dict[str, Any] | None:
        """Get the user information for the local node.

        Returns
        -------
        user : dict[str, Any] | None
            The local node's `user` dictionary, or `None` if no local node info or no `user` field is present.
        """
        nodeInfo = self.getMyNodeInfo()
        if nodeInfo is not None:
            return nodeInfo.get("user")
        return None

    def getLongName(self) -> str | None:
        """Get the local user's configured long name.

        Returns
        -------
        str | None
            The long name string if configured, `None` otherwise.
        """
        user = self.getMyUser()
        if user is not None:
            return user.get("longName", None)
        return None

    def getShortName(self) -> str | None:
        """Get the local node user's short name.

        Returns
        -------
        str | None
            The user's `shortName` if present, `None` otherwise.
        """
        user = self.getMyUser()
        if user is not None:
            return user.get("shortName", None)
        return None

    def getPublicKey(self) -> bytes | None:
        """Return the local node's public key if available.

        Returns
        -------
        bytes | None
            The local node's public key bytes if present, `None` otherwise.
        """
        user = self.getMyUser()
        if user is not None:
            return user.get("publicKey", None)
        return None

    def getCannedMessage(self) -> str | None:
        """Retrieve the canned (predefined) message configured for the local node.

        Returns
        -------
        str | None
            The canned message text, or `None` if there is no local node or no canned message configured.
        """
        node = self.localNode
        if node is not None:
            return node.get_canned_message()
        return None

    def getRingtone(self) -> str | None:
        """Get the local node's ringtone name or identifier.

        Returns
        -------
        str | None
            The ringtone name or identifier as a string, or None if the local node or ringtone is unavailable.
        """
        node = self.localNode
        if node is not None:
            return node.get_ringtone()
        return None

    def _wait_connected(self, timeout: float = 30.0) -> None:
        """Wait until the interface is marked connected or the timeout elapses.

        Parameters
        ----------
        timeout : float
            Maximum seconds to wait for the connection. (Default value = 30.0)

        Raises
        ------
        MeshInterface.MeshInterfaceError
            If waiting timed out.
        Exception
            Re-raises a stored fatal exception if one occurred during connection.
        """
        if not self.noProto:
            if not self.isConnected.wait(timeout):  # timeout after x seconds
                raise MeshInterface.MeshInterfaceError(
                    "Timed out waiting for connection completion"
                )

        # If we failed while connecting, raise the connection to the client
        if self.failure is not None:
            raise self.failure

    def _generate_packet_id(self) -> int:
        """Generate a new 32-bit packet identifier combining a 10-bit monotonic counter with randomized upper bits.

        Returns
        -------
        packet_id : int
            New packet id where the low 10 bits are a monotonic counter and the remaining bits are randomized.

        Raises
        ------
        MeshInterface.MeshInterfaceError
            If `currentPacketId` is None and a packet id cannot be generated.
        """
        with self._packet_id_lock:
            if self.currentPacketId is None:
                raise MeshInterface.MeshInterfaceError(
                    "Not connected yet, can not generate packet"
                )
            next_packet_id = (self.currentPacketId + 1) & PACKET_ID_MASK
            next_packet_id = (
                next_packet_id & PACKET_ID_COUNTER_MASK
            )  # Keep only low 10-bit counter (clear upper 22 bits)
            random_part = (
                random.randint(0, PACKET_ID_RANDOM_MAX) << PACKET_ID_RANDOM_SHIFT_BITS  # noqa: S311
            ) & PACKET_ID_MASK  # generate number with 10 zeros at end
            self.currentPacketId = next_packet_id | random_part  # combine
            return self.currentPacketId

    # COMPAT_STABLE_SHIM: historical private camelCase helper used by external integrations.
    def _generatePacketId(self) -> int:
        """Backward-compatible alias for `_generate_packet_id`."""
        return self._generate_packet_id()

    def _disconnected(self) -> None:
        """Mark the interface as disconnected and publish meshtastic.connection.lost once per connection.

        Clears the internal connected flag and publishes a lost-connection
        notification only if the interface was previously connected. This keeps
        shutdown/retry paths from publishing duplicate "lost" events.
        """
        with self._heartbeat_lock:
            was_connected = self.isConnected.is_set()
            self.isConnected.clear()
        if was_connected:
            publishingThread.queueWork(
                lambda: pub.sendMessage("meshtastic.connection.lost", interface=self)
            )

    def sendHeartbeat(self) -> None:
        """Send a heartbeat message to the radio to indicate the interface is alive."""
        p = mesh_pb2.ToRadio()
        p.heartbeat.CopyFrom(mesh_pb2.Heartbeat())
        self._send_to_radio(p)

    def _start_heartbeat(self) -> None:
        """Start a recurring daemon timer that sends a heartbeat to the radio every 300 seconds.

        Schedules and runs the first heartbeat immediately and then re-schedules a daemon
        threading.Timer to invoke subsequent heartbeats at a fixed 300-second interval. The
        scheduler respects shutdown by checking self._closing and uses self._heartbeat_lock to
        avoid scheduling or storing timers after shutdown begins. Heartbeat sends are tracked
        as in-flight so close() can wait for quiescence and guarantee no post-close sends. The
        actual call to self.sendHeartbeat() is still performed outside the lock.
        """

        def callback() -> None:
            """Schedule the next heartbeat and emit one unless the interface is shutting down.

            Schedules a daemon timer to re-run this callback after a fixed interval and then calls
            self.sendHeartbeat(). If self._closing is set, no timer is scheduled and no heartbeat is sent.
            """
            interval = HEARTBEAT_INTERVAL_SECONDS
            logger.debug("Sending heartbeat, interval %s seconds", interval)
            with self._heartbeat_lock:
                # Keep timer update/start in one critical section for simpler
                # state reasoning while still honoring shutdown.
                if self._closing:
                    return
                self.heartbeatTimer = None
                timer = threading.Timer(interval, callback)
                # Heartbeat maintenance should never prevent process shutdown.
                timer.daemon = True
                self.heartbeatTimer = timer
                timer.start()
                # Mark this send as in-flight before releasing the lock so
                # close() cannot miss it when establishing shutdown quiescence.
                self._heartbeat_inflight += 1
            # sendHeartbeat() is intentionally outside the lock to avoid
            # holding the lock during I/O. close() handles timer cancellation.
            try:
                self.sendHeartbeat()
            finally:
                with self._heartbeat_lock:
                    self._heartbeat_inflight -= 1
                    if self._heartbeat_inflight == 0:
                        self._heartbeat_idle_condition.notify_all()

        callback()  # run our periodic callback now, it will make another timer if necessary

    def _connected(self) -> None:
        """Mark the interface as connected, start the heartbeat timer, and publish a connection-established event.

        If the interface is shutting down, do nothing. Otherwise set the
        internal connected event, start periodic heartbeats, and enqueue a
        "meshtastic.connection.established" publication.
        """
        start_heartbeat = False
        with self._heartbeat_lock:
            if self._closing:
                logger.debug("Skipping _connected(): interface is closing")
                return
            # (because I'm lazy) _connected might be called when remote Node
            # objects complete their config reads, don't generate redundant isConnected
            # for the local interface
            if not self.isConnected.is_set():
                self.isConnected.set()
                start_heartbeat = True
        if start_heartbeat:
            self._start_heartbeat()
        # Check _closing again before publishing to avoid race with close()
        with self._heartbeat_lock:
            # Publish once per disconnected->connected transition.
            should_publish = start_heartbeat and not self._closing
        if should_publish:
            publishingThread.queueWork(
                lambda: pub.sendMessage(
                    "meshtastic.connection.established", interface=self
                )
            )

    def _start_config(self) -> None:
        """Initialize internal node/config state and request the radio's configuration.

        Resets local state used during configuration (myInfo, nodes, nodesByNum, and local channel list),
        allocates a new non-conflicting configId when appropriate, and sends a ToRadio message
        requesting configuration using the chosen configId.
        """
        with self._node_db_lock:
            self.myInfo = None
            self.nodes = {}  # nodes keyed by ID
            self.nodesByNum = {}  # nodes keyed by nodenum
            self._localChannels = []  # empty until we start getting channels pushed from the device (during config)
            config_id = self.configId
            if config_id is None or not self.noNodes:
                config_id = random.randint(0, PACKET_ID_MASK)
                if config_id == NODELESS_WANT_CONFIG_ID:
                    config_id = config_id + 1
                self.configId = config_id

        startConfig = mesh_pb2.ToRadio()
        startConfig.want_config_id = config_id
        self._send_to_radio(startConfig)

    def _send_disconnect(self) -> None:
        """Notify the radio device that this interface is disconnecting."""
        m = mesh_pb2.ToRadio()
        m.disconnect = True
        self._send_to_radio(m)

    def _queue_has_free_space(self) -> bool:
        # We never got queueStatus, maybe the firmware is old
        """Indicate whether the cached transmit queue has free slots.

        Returns
        -------
        bool
            `True` if at least one free slot is available or the queue status is unknown, `False` otherwise.
        """
        return self._queue_send_runtime.has_free_space()

    def _queue_claim(self) -> None:
        """Decrement the cached transmit-queue free-slot counter when a packet is claimed.

        Does nothing if queue status information is not available.
        """
        self._queue_send_runtime.claim()

    def _queue_pop_for_send(self) -> tuple[int, mesh_pb2.ToRadio | bool] | None:
        """Atomically pop the next queued packet if TX queue state permits sending.

        Returns
        -------
        tuple[int, mesh_pb2.ToRadio | bool] | None
            The popped queue entry `(packet_id, payload)` when available and sendable,
            otherwise `None`.
        """
        return self._queue_send_runtime.pop_for_send()

    def _send_to_radio(self, toRadio: mesh_pb2.ToRadio) -> None:
        """Queue and transmit a ToRadio protobuf to the radio device.

        If the ToRadio has a MeshPacket in its `packet` field, that packet is enqueued and will be
        transmitted when TX queue space is available; otherwise the message is sent immediately.
        The method respects the interface's `noProto` setting (no transmission when disabled),
        may block while waiting for TX queue space, and preserves/requeues packets that remain unacknowledged.

        Parameters
        ----------
        toRadio : mesh_pb2.ToRadio
            The ToRadio protobuf to send; if it contains a `packet` field
            the contained MeshPacket will be queued for transmission.
        """
        if self.noProto:
            logger.warning(
                "Not sending packet because protocol use is disabled by noProto"
            )
            return

        self._queue_send_runtime.send_to_radio(
            toRadio,
            send_impl=self._send_to_radio_impl,
            pop_for_send=self._queue_pop_for_send,
            sleep_fn=time.sleep,
        )

    def _send_to_radio_impl(self, toRadio: mesh_pb2.ToRadio) -> None:
        """Transport hook that delivers a ToRadio protobuf to the radio device.

        Subclasses must override this method to perform the actual transmission; the base
        implementation logs an error when invoked.

        Parameters
        ----------
        toRadio : mesh_pb2.ToRadio
            Protobuf describing the action or packet to send to the radio.
        """
        logger.error("Subclass must provide toradio: %s", toRadio)

    def _handle_config_complete(self) -> None:
        """Finalize initial configuration by applying collected local channels and marking the interface as connected.

        Sets the local node's channels from the internally collected
        _localChannels and invokes _connected() to signal that configuration is
        complete and normal packet handling may begin.
        """
        # This is no longer necessary because the current protocol statemachine has already proactively sent us the locally visible channels
        # self.localNode.requestChannels()
        with self._node_db_lock:
            local_channels = list(self._localChannels)
        self.localNode.setChannels(local_channels)

        # the following should only be called after we have settings and channels
        self._connected()  # Tell everyone else we are ready to go

    def _handle_queue_status_from_radio(
        self, queueStatus: mesh_pb2.QueueStatus
    ) -> None:
        """Update internal transmit-queue state from a received QueueStatus message.

        Sets self.queueStatus and logs the reported free/total slots and packet id.
        If queueStatus.res is falsy, removes the entry for queueStatus.mesh_packet_id from
        self.queue; if no entry exists and mesh_packet_id is nonzero, records a False
        marker for that id to indicate an unexpected reply was observed.

        Parameters
        ----------
        queueStatus : mesh_pb2.QueueStatus
            An object (protobuf-like) with attributes `free`, `maxlen`,
            `res`, and `mesh_packet_id` describing the radio's transmit-queue state.
        """
        self._queue_send_runtime.handle_queue_status_from_radio(queueStatus)

    def _record_queue_status(self, queueStatus: mesh_pb2.QueueStatus) -> None:
        """Persist latest radio TX queue status under queue ownership."""
        self._queue_send_runtime.record_queue_status(queueStatus)

    def _correlate_queue_status_reply(self, queueStatus: mesh_pb2.QueueStatus) -> None:
        """Correlate queue reply IDs with local pending queue entries."""
        self._queue_send_runtime.correlate_queue_status_reply(queueStatus)
        # logger.warn("queue: " + " ".join(f'{k:08x}' for k in self.queue))

    def _handle_from_radio(self, fromRadioBytes: bytes) -> None:
        """Handle a raw FromRadio payload using parse -> normalize -> dispatch -> publish phases."""
        from_radio = self._parse_from_radio_bytes(fromRadioBytes)
        context = self._normalize_from_radio_message(from_radio)
        publication_intents = self._dispatch_from_radio_message(context)
        self._emit_publication_intents(publication_intents)

    def _parse_from_radio_bytes(self, from_radio_bytes: bytes) -> mesh_pb2.FromRadio:
        """Parse raw FromRadio bytes into protobuf form."""
        from_radio = mesh_pb2.FromRadio()
        logger.debug(
            "in mesh_interface.py _handle_from_radio() fromRadioBytes: %r",
            from_radio_bytes,
        )
        try:
            from_radio.ParseFromString(from_radio_bytes)
        except Exception:
            logger.exception("Error while parsing FromRadio bytes:%s", from_radio_bytes)
            raise
        return from_radio

    def _normalize_from_radio_message(
        self, from_radio: mesh_pb2.FromRadio
    ) -> _FromRadioContext:
        """Normalize parsed FromRadio data for dispatch and mutation handlers."""
        logger.debug("Received from radio: %s", from_radio)
        with self._node_db_lock:
            config_id = self.configId
        return _FromRadioContext(
            message=from_radio,
            message_dict=_LazyMessageDict(from_radio),
            config_id=config_id,
        )

    def _dispatch_from_radio_message(
        self, context: _FromRadioContext
    ) -> list[_PublicationIntent]:
        """Dispatch normalized FromRadio payloads to dedicated branch handlers."""
        branch = self._select_from_radio_branch(context)
        if branch is None:
            logger.debug("Unexpected FromRadio payload")
            return []
        handler = self._from_radio_dispatch_map()[branch]
        return handler(context)

    def _select_from_radio_branch(self, context: _FromRadioContext) -> str | None:
        """Select the active FromRadio branch using the historical precedence order."""
        from_radio = context.message
        branch: str | None = None
        if from_radio.HasField("my_info"):
            branch = "my_info"
        elif from_radio.HasField("metadata"):
            branch = "metadata"
        elif from_radio.HasField("node_info"):
            branch = "node_info"
        elif (
            from_radio.config_complete_id != 0
            and from_radio.config_complete_id == context.config_id
        ):
            branch = "config_complete_id"
        elif from_radio.HasField("channel"):
            branch = "channel"
        elif from_radio.HasField("packet"):
            branch = "packet"
        elif from_radio.HasField("log_record"):
            branch = "log_record"
        elif from_radio.HasField("queueStatus"):
            branch = "queueStatus"
        elif from_radio.HasField("clientNotification"):
            branch = "clientNotification"
        elif from_radio.HasField("mqttClientProxyMessage"):
            branch = "mqttClientProxyMessage"
        elif from_radio.HasField("xmodemPacket"):
            branch = "xmodemPacket"
        elif from_radio.HasField("rebooted") and from_radio.rebooted:
            branch = "rebooted"
        elif from_radio.HasField("config") or from_radio.HasField("moduleConfig"):
            branch = "config_or_moduleConfig"
        return branch

    def _from_radio_dispatch_map(
        self,
    ) -> dict[str, Callable[[_FromRadioContext], list[_PublicationIntent]]]:
        """Return branch handlers for FromRadio dispatch."""
        if self._from_radio_dispatch_map_cache is None:
            self._from_radio_dispatch_map_cache = {
                "my_info": self._handle_from_radio_my_info,
                "metadata": self._handle_from_radio_metadata,
                "node_info": self._handle_from_radio_node_info,
                "config_complete_id": self._handle_from_radio_config_complete_id,
                "channel": self._handle_from_radio_channel,
                "packet": self._handle_from_radio_packet,
                "log_record": self._handle_from_radio_log_record,
                "queueStatus": self._handle_from_radio_queue_status,
                "clientNotification": self._handle_from_radio_client_notification,
                "mqttClientProxyMessage": self._handle_from_radio_mqtt_client_proxy_message,
                "xmodemPacket": self._handle_from_radio_xmodem_packet,
                "rebooted": self._handle_from_radio_rebooted,
                "config_or_moduleConfig": self._handle_from_radio_config_update,
            }
        return self._from_radio_dispatch_map_cache

    def _handle_from_radio_my_info(
        self, context: _FromRadioContext
    ) -> list[_PublicationIntent]:
        """Apply my_info updates to interface state."""
        from_radio = context.message
        with self._node_db_lock:
            my_info = mesh_pb2.MyNodeInfo()
            my_info.CopyFrom(from_radio.my_info)
            self.myInfo = my_info
            self.localNode.nodeNum = my_info.my_node_num
        logger.debug("Received myinfo: %s", stripnl(from_radio.my_info))
        return []

    def _handle_from_radio_metadata(
        self, context: _FromRadioContext
    ) -> list[_PublicationIntent]:
        """Apply metadata updates to interface state."""
        from_radio = context.message
        with self._node_db_lock:
            metadata = mesh_pb2.DeviceMetadata()
            metadata.CopyFrom(from_radio.metadata)
            self.metadata = metadata
        logger.debug("Received device metadata: %s", stripnl(from_radio.metadata))
        return []

    def _handle_from_radio_node_info(
        self, context: _FromRadioContext
    ) -> list[_PublicationIntent]:
        """Apply node_info updates and emit node-updated publication intents."""
        node_info = context.message_dict.get()["nodeInfo"]
        logger.debug("Received nodeinfo: %s", node_info)

        node = self._get_or_create_by_num(node_info["num"])
        with self._node_db_lock:
            node.update(node_info)
            try:
                node["position"] = self._fixup_position(node["position"])
            except KeyError:
                logger.debug("Node has no position key")

            # no longer necessary since we're mutating directly in nodesByNum via _get_or_create_by_num
            # self.nodesByNum[node["num"]] = node
            if "user" in node:  # Some nodes might not have user/ids assigned yet
                if "id" in node["user"]:
                    # Keep nodes and nodesByNum mutation under the same lock
                    # so readers never observe partially-updated node mappings.
                    if self.nodes is not None:
                        self.nodes[node["user"]["id"]] = node

        return [
            self._publication_intent("meshtastic.node.updated", node=node),
        ]

    def _handle_from_radio_config_complete_id(
        self, context: _FromRadioContext
    ) -> list[_PublicationIntent]:
        """Handle config-complete correlation and startup completion."""
        logger.debug("Config complete ID %s", context.config_id)
        self._handle_config_complete()
        return []

    def _handle_from_radio_channel(
        self, context: _FromRadioContext
    ) -> list[_PublicationIntent]:
        """Handle incoming channel updates."""
        self._handle_channel(context.message.channel)
        return []

    def _handle_from_radio_packet(
        self, context: _FromRadioContext
    ) -> list[_PublicationIntent]:
        """Handle incoming mesh packets and return publication intents."""
        return self._handle_packet_from_radio(
            context.message.packet,
            emit_publication=False,
        )

    def _handle_from_radio_log_record(
        self, context: _FromRadioContext
    ) -> list[_PublicationIntent]:
        """Handle incoming log records."""
        self._handle_log_record(context.message.log_record)
        return []

    def _handle_from_radio_queue_status(
        self, context: _FromRadioContext
    ) -> list[_PublicationIntent]:
        """Handle inbound queue status updates/correlation."""
        self._handle_queue_status_from_radio(context.message.queueStatus)
        return []

    def _handle_from_radio_client_notification(
        self, context: _FromRadioContext
    ) -> list[_PublicationIntent]:
        """Build publication intent for client notifications."""
        return [
            self._publication_intent(
                "meshtastic.clientNotification",
                notification=context.message.clientNotification,
            ),
        ]

    def _handle_from_radio_mqtt_client_proxy_message(
        self, context: _FromRadioContext
    ) -> list[_PublicationIntent]:
        """Build publication intent for MQTT client proxy messages."""
        return [
            self._publication_intent(
                "meshtastic.mqttclientproxymessage",
                proxymessage=context.message.mqttClientProxyMessage,
            ),
        ]

    def _handle_from_radio_xmodem_packet(
        self, context: _FromRadioContext
    ) -> list[_PublicationIntent]:
        """Build publication intent for inbound XMODEM payloads."""
        return [
            self._publication_intent(
                "meshtastic.xmodempacket",
                packet=context.message.xmodemPacket,
            ),
        ]

    def _handle_from_radio_rebooted(
        self, _context: _FromRadioContext
    ) -> list[_PublicationIntent]:
        """Handle reboot notifications by disconnecting and restarting config flow."""
        # Tell clients the device went away.  Careful not to call the overridden
        # subclass version that closes the serial port
        MeshInterface._disconnected(self)
        self._start_config()  # redownload the node db etc...
        return []

    def _handle_from_radio_config_update(
        self, context: _FromRadioContext
    ) -> list[_PublicationIntent]:
        """Apply localConfig/moduleConfig updates from inbound FromRadio payloads."""
        self._apply_config_from_radio(context.message)
        return []

    def _apply_config_from_radio(self, from_radio: mesh_pb2.FromRadio) -> None:
        """Copy the active config/moduleConfig submessage into local cached config."""
        with self._node_db_lock:
            if self._apply_local_config_from_radio(from_radio.config):
                return
            self._apply_module_config_from_radio(from_radio.moduleConfig)

    def _apply_local_config_from_radio(self, config: config_pb2.Config) -> bool:
        """Apply one localConfig field from inbound config payload."""
        for field_name in LOCAL_CONFIG_FROM_RADIO_FIELDS:
            if config.HasField(field_name):
                getattr(self.localNode.localConfig, field_name).CopyFrom(
                    getattr(config, field_name)
                )
                return True
        return False

    def _apply_module_config_from_radio(
        self, module_config: module_config_pb2.ModuleConfig
    ) -> bool:
        """Apply one moduleConfig field from inbound moduleConfig payload."""
        for field_name in MODULE_CONFIG_FROM_RADIO_FIELDS:
            if module_config.HasField(field_name):
                getattr(self.localNode.moduleConfig, field_name).CopyFrom(
                    getattr(module_config, field_name)
                )
                return True
        return False

    def _publication_intent(self, topic: str, **payload: Any) -> _PublicationIntent:
        """Create a publication intent for deferred emission."""
        return _PublicationIntent(topic=topic, payload=dict(payload))

    def _emit_publication_intents(self, intents: list[_PublicationIntent]) -> None:
        """Emit queued publication intents in a dedicated publication phase."""
        for intent in intents:
            self._queue_publication(intent.topic, **intent.payload)

    def _queue_publication(self, topic: str, **payload: Any) -> None:
        """Queue a pubsub emission for the publishing thread."""
        payload_snapshot = dict(payload)

        def publish_work() -> None:
            pub.sendMessage(topic, interface=self, **payload_snapshot)

        publishingThread.queueWork(publish_work)

    def _fixup_position(self, position: dict[str, Any]) -> dict[str, Any]:
        """Convert integer micro-degree coordinates in a position dict to floating-point degrees.

        If present, 'latitudeI' and 'longitudeI' are converted to 'latitude' and 'longitude'
        by multiplying by 1e-7 (micro-degrees -> degrees) and stored back into the same dict.

        Parameters
        ----------
        position : dict[str, Any]
            Position dictionary that may contain integer keys 'latitudeI' and 'longitudeI'.

        Returns
        -------
        dict[str, Any]
            The same position dictionary with 'latitude' and/or 'longitude' set to float degrees when corresponding integer fields were present.
        """
        if "latitudeI" in position:
            position["latitude"] = position["latitudeI"] * 1e-7
        if "longitudeI" in position:
            position["longitude"] = position["longitudeI"] * 1e-7
        return position

    def _node_num_to_id(self, num: int, isDest: bool = True) -> str | None:
        """Map a mesh numeric node number to its node ID string or a broadcast/unknown literal.

        If num equals the broadcast numeric constant, returns BROADCAST_ADDR when isDest is True
        or the string "Unknown" when isDest is False. Otherwise looks up and returns the stored
        user ID for that node number.

        Parameters
        ----------
        isDest : bool
            When True treat the broadcast number as a destination (return
            BROADCAST_ADDR); when False treat it as an unknown source (return "Unknown"). (Default value = True)
        num : int
            Numeric node identifier.

        Returns
        -------
        str | None
            The node ID string, BROADCAST_ADDR for broadcast destinations, "Unknown" for
            broadcast sources, or `None` if the node number is not present in the local node map.
        """
        if num == BROADCAST_NUM:
            return BROADCAST_ADDR if isDest else "Unknown"

        with self._node_db_lock:
            nodes = self.nodesByNum
            if nodes is None:
                logger.debug(
                    "Node database not initialized while resolving node id for %s", num
                )
                return None
            node = nodes.get(num)
            if not isinstance(node, dict):
                logger.debug("Node %s not found for fromId", num)
                return None
            user = node.get("user")
            if not isinstance(user, dict):
                logger.debug("Node %s has no user payload for fromId", num)
                return None
            node_id = user.get("id")
            if not isinstance(node_id, str):
                logger.debug("Node %s user payload has no valid id", num)
                return None
            return node_id

    def _get_or_create_by_num(self, nodeNum: int) -> dict[str, Any]:
        """Retrieve the node record for a numeric node ID, creating a minimal placeholder if none exists.

        Parameters
        ----------
        nodeNum : int
            Numeric node identifier.

        Returns
        -------
        dict[str, Any]
            The node info dictionary stored in self.nodesByNum for the given nodeNum.

        Raises
        ------
        MeshInterface.MeshInterfaceError
            If nodeNum is the broadcast node number or if the node database has not been initialized.
        """
        if nodeNum == BROADCAST_NUM:
            raise MeshInterface.MeshInterfaceError(
                "Can not create/find nodenum by the broadcast num"
            )

        with self._node_db_lock:
            if self.nodesByNum is None:
                raise MeshInterface.MeshInterfaceError("Node database not initialized")

            if nodeNum in self.nodesByNum:
                return self.nodesByNum[nodeNum]
            presumptive_id = f"!{nodeNum:08x}"
            n = {
                "num": nodeNum,
                "user": {
                    "id": presumptive_id,
                    "longName": f"Meshtastic {presumptive_id[-4:]}",
                    "shortName": f"{presumptive_id[-4:]}",
                    "hwModel": "UNSET",
                },
            }  # Create a minimal node db entry
            self.nodesByNum[nodeNum] = n
            return n

    def _handle_channel(self, channel: channel_pb2.Channel) -> None:
        """Record a received local channel descriptor for later configuration.

        Parameters
        ----------
        channel : channel_pb2.Channel
            Channel descriptor to append to the internal _localChannels list.
        """
        with self._node_db_lock:
            self._localChannels.append(channel)

    def _handle_packet_from_radio(
        self,
        meshPacket: mesh_pb2.MeshPacket,
        hack: bool = False,
        *,
        emit_publication: bool = True,
    ) -> list[_PublicationIntent]:
        """Process incoming MeshPacket with explicit normalize/classify/mutate/publish phases."""
        packet_dict = self._normalize_packet_from_radio(meshPacket, hack=hack)
        if packet_dict is None:
            return []

        packet_context = _PacketRuntimeContext(packet_dict=packet_dict)
        self._enrich_packet_identity(packet_context.packet_dict)
        self._classify_packet_runtime(packet_context, meshPacket)
        self._apply_packet_runtime_mutations(packet_context, meshPacket)
        self._correlate_packet_response_handler(packet_context)

        publication_intents = [
            self._publication_intent(
                packet_context.topic,
                packet=packet_context.packet_dict,
            )
        ]
        logger.debug(
            "Publishing %s: packet=%s",
            packet_context.topic,
            stripnl(packet_context.packet_dict),
        )
        if emit_publication:
            self._emit_publication_intents(publication_intents)
        return publication_intents

    def _normalize_packet_from_radio(
        self,
        meshPacket: mesh_pb2.MeshPacket,
        *,
        hack: bool,
    ) -> dict[str, Any] | None:
        """Convert protobuf packet into runtime dict and enforce legacy defaults."""
        if not hack and getattr(meshPacket, "from") == 0:
            packet_dict = {"raw": meshPacket, "from": 0}
            logger.error(
                "Device returned a packet we sent, ignoring: %s",
                stripnl(packet_dict),
            )
            return None

        packet_dict = _LazyMessageDict(meshPacket).get()

        # We normally decompose the payload into a dictionary so that the client
        # doesn't need to understand protobufs.  But advanced clients might
        # want the raw protobuf, so we provide it in "raw"
        packet_dict["raw"] = meshPacket

        if "to" not in packet_dict:
            packet_dict["to"] = 0
        return packet_dict

    def _enrich_packet_identity(self, packet_dict: dict[str, Any]) -> None:
        """Populate fromId/toId fields from known node-number mappings."""
        try:
            packet_dict["fromId"] = self._node_num_to_id(packet_dict["from"], False)
        except Exception as ex:
            logger.warning("Not populating fromId: %s", ex, exc_info=True)
        try:
            packet_dict["toId"] = self._node_num_to_id(packet_dict["to"])
        except Exception as ex:
            logger.warning("Not populating toId: %s", ex, exc_info=True)

    def _classify_packet_runtime(
        self,
        packet_context: _PacketRuntimeContext,
        mesh_packet: mesh_pb2.MeshPacket,
    ) -> None:
        """Classify packet topic and decoded payload view."""
        # We could provide our objects as DotMaps - which work with . notation or as dictionaries
        # asObj = DotMap(asDict)
        packet_context.topic = "meshtastic.receive"  # Generic unknown packet type

        if "decoded" not in packet_context.packet_dict:
            return

        decoded = cast(dict[str, Any], packet_context.packet_dict["decoded"])
        packet_context.decoded = decoded
        # The default MessageToDict converts byte arrays into base64 strings.
        # We don't want that - it messes up data payload.  So slam in the correct
        # byte array.
        decoded["payload"] = mesh_packet.decoded.payload

        portnum = portnums_pb2.PortNum.Name(portnums_pb2.PortNum.UNKNOWN_APP)
        # UNKNOWN_APP is the default protobuf portnum value, and therefore if not
        # set it will not be populated at all to make API usage easier, set
        # it to prevent confusion
        if "portnum" not in decoded:
            decoded["portnum"] = portnum
            logger.warning("portnum was not in decoded. Setting to:%s", portnum)
        else:
            portnum = decoded["portnum"]
        packet_context.topic = f"meshtastic.receive.data.{portnum}"

    def _apply_packet_runtime_mutations(
        self,
        packet_context: _PacketRuntimeContext,
        mesh_packet: mesh_pb2.MeshPacket,
    ) -> None:
        """Decode known payloads and run protocol-specific onReceive handlers."""
        if packet_context.decoded is None:
            return

        # decode position protobufs and update nodedb, provide decoded version
        # as "position" in the published msg move the following into a 'decoders'
        # API that clients could register?
        port_num_int = mesh_packet.decoded.portnum  # we want portnum as an int
        handler = protocols.get(port_num_int)
        if handler is None:
            return

        packet_context.topic = f"meshtastic.receive.{handler.name}"
        self._decode_packet_payload_with_handler(packet_context, mesh_packet, handler)

        # Call specialized onReceive if necessary
        if handler.onReceive is not None:
            handler.onReceive(self, packet_context.packet_dict)

    def _decode_packet_payload_with_handler(
        self,
        packet_context: _PacketRuntimeContext,
        mesh_packet: mesh_pb2.MeshPacket,
        handler: Any,
    ) -> None:
        """Decode decoded.payload using a protocol handler protobuf factory when available."""
        if handler.protobufFactory is None:
            return

        pb = handler.protobufFactory()
        try:
            pb.ParseFromString(mesh_packet.decoded.payload)
            decoded_payload = google.protobuf.json_format.MessageToDict(pb)
            packet_context.packet_dict["decoded"][handler.name] = decoded_payload
            # Also provide the protobuf raw
            packet_context.packet_dict["decoded"][handler.name]["raw"] = pb
        except (protobuf_message.DecodeError, TypeError, ValueError) as exc:
            decode_error = f"{DECODE_FAILED_PREFIX}{exc}"
            logger.warning(
                "Failed to decode %s payload for packet id=%s from=%s to=%s: %s",
                handler.name,
                getattr(mesh_packet, "id", 0),
                packet_context.packet_dict.get("from"),
                packet_context.packet_dict.get("to"),
                exc,
            )
            packet_context.packet_dict["decoded"][handler.name] = {
                DECODE_ERROR_KEY: decode_error
            }
            if handler.name == "routing":
                packet_context.packet_dict["decoded"][handler.name]["errorReason"] = (
                    decode_error
                )
            if handler.name == "admin":
                # Admin callbacks frequently expect decoded.admin.raw.
                # Avoid dispatching malformed payloads through that path.
                packet_context.skip_response_callback_for_decode_failure = True

    def _correlate_packet_response_handler(
        self, packet_context: _PacketRuntimeContext
    ) -> None:
        """Correlate requestId responses with registered response handlers."""
        if packet_context.decoded is None:
            return
        self._request_wait_runtime.correlate_inbound_response(
            packet_dict=packet_context.packet_dict,
            skip_response_callback_for_decode_failure=(
                packet_context.skip_response_callback_for_decode_failure
            ),
            extract_request_id=self._extract_request_id_from_packet,
        )
