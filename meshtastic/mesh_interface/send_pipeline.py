"""Send pipeline for transmitting packets to the radio."""

from __future__ import annotations

import collections
import logging
import math
import secrets
import threading
import time
from typing import TYPE_CHECKING, Any, Callable, Literal, Protocol, TypeAlias, cast

if TYPE_CHECKING:
    from meshtastic.mesh_interface import MeshInterface

import google.protobuf.json_format
from google.protobuf import message as protobuf_message

from meshtastic import BROADCAST_ADDR, BROADCAST_NUM, LOCAL_ADDR
from meshtastic.protobuf import mesh_pb2, portnums_pb2, telemetry_pb2
from meshtastic.util import Acknowledgment, Timeout, stripnl

logger = logging.getLogger(__name__)

PACKET_ID_MASK = 0xFFFFFFFF
PACKET_ID_COUNTER_MASK = 0x3FF
PACKET_ID_RANDOM_MAX = 0x3FFFFF
PACKET_ID_RANDOM_SHIFT_BITS = 10

QUEUE_WAIT_DELAY_SECONDS = 0.5

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
NO_RESPONSE_FIRMWARE_ERROR: str = "No response from node. At least firmware 2.1.22 is required on the destination node."
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


class _SerializablePayload(Protocol):
    """Protocol for payloads that can serialize to bytes."""

    def SerializeToString(self) -> bytes:
        """Return serialized payload bytes."""
        ...


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
    """Return a compact 8-hex node-id body when ``destination_id`` matches supported forms."""
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


def _emit_response_summary(message: str) -> None:
    """Emit a short response summary without hiding legacy stdout behavior."""
    logger.info("%s", message)


class _RequestWaitRuntime:
    """Owns request/wait bookkeeping, scoped wait semantics, and response correlation."""

    def __init__(
        self,
        *,
        lock: threading.RLock,
        get_response_handlers: Callable[[], dict[int, Any]],
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
            handler: Any = type(
                "ResponseHandler",
                (),
                {
                    "callback": callback,
                    "ackPermitted": ack_permitted,
                },
            )()
            self._get_response_handlers()[request_id] = handler

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
                    wait_acks.discard((acknowledgment_attr, UNSCOPED_WAIT_REQUEST_ID))
            else:
                wait_errors.pop((acknowledgment_attr, request_id), None)
                wait_acks.discard((acknowledgment_attr, request_id))

    def set_wait_error(
        self,
        acknowledgment_attr: str,
        message: str,
        *,
        request_id: int | None = None,
    ) -> None:
        """Record a wait error and wake the matching waiter."""
        with self._lock:
            wait_errors = self._get_wait_errors()
            ack_key = (
                acknowledgment_attr,
                request_id if request_id is not None else UNSCOPED_WAIT_REQUEST_ID,
            )
            wait_errors[ack_key] = message
        acknowledgment = self._get_acknowledgment()
        if request_id is None:
            acknowledgment.receivedNak = True
        else:
            acknowledgment.receivedNak = True

    def mark_wait_acknowledged(
        self, acknowledgment_attr: str, *, request_id: int | None = None
    ) -> None:
        """Set acknowledgment flag for the matching request scope."""
        with self._lock:
            wait_acks = self._get_wait_acks()
            active_wait_request_ids = self._get_active_wait_request_ids()
            ack_key = (
                acknowledgment_attr,
                request_id if request_id is not None else UNSCOPED_WAIT_REQUEST_ID,
            )
            wait_acks.add(ack_key)
            if request_id is not None:
                if acknowledgment_attr not in active_wait_request_ids:
                    active_wait_request_ids[acknowledgment_attr] = set()
                active_wait_request_ids[acknowledgment_attr].add(request_id)
        acknowledgment = self._get_acknowledgment()
        if acknowledgment_attr == WAIT_ATTR_POSITION:
            acknowledgment.receivedPosition = True
        elif acknowledgment_attr == WAIT_ATTR_TELEMETRY:
            acknowledgment.receivedTelemetry = True
        elif acknowledgment_attr == WAIT_ATTR_TRACEROUTE:
            acknowledgment.receivedTraceRoute = True
        elif acknowledgment_attr == WAIT_ATTR_WAYPOINT:
            acknowledgment.receivedWaypoint = True

    def raise_wait_error_if_present(
        self,
        acknowledgment_attr: str,
        request_id: int | None = None,
        *,
        error_factory: type[Exception] = Exception,
    ) -> None:
        """Raise and clear any pending wait error for the given wait scope."""
        with self._lock:
            wait_errors = self._get_wait_errors()
            ack_key = (
                acknowledgment_attr,
                request_id if request_id is not None else UNSCOPED_WAIT_REQUEST_ID,
            )
            error_message = wait_errors.pop(ack_key, None)
        if error_message:
            raise error_factory(error_message)

    def retire_wait_request(
        self, acknowledgment_attr: str, request_id: int | None = None
    ) -> None:
        """Retire response handler and wait bookkeeping for a completed wait."""
        with self._lock:
            wait_acks = self._get_wait_acks()
            wait_errors = self._get_wait_errors()
            active_wait_request_ids = self._get_active_wait_request_ids()
            retired_wait_request_ids = self._get_retired_wait_request_ids()
            ack_key = (
                acknowledgment_attr,
                request_id if request_id is not None else UNSCOPED_WAIT_REQUEST_ID,
            )
            wait_acks.discard(ack_key)
            wait_errors.pop(ack_key, None)
            if request_id is not None:
                active_wait_request_ids.get(acknowledgment_attr, set()).discard(
                    request_id
                )
                if acknowledgment_attr not in retired_wait_request_ids:
                    retired_wait_request_ids[acknowledgment_attr] = {}
                retired_wait_request_ids[acknowledgment_attr][request_id] = time.time()
                self._get_response_handlers().pop(request_id, None)

    def prune_retired_wait_request_ids_locked(
        self, acknowledgment_attr: str
    ) -> dict[int, float]:
        """Prune expired retired request ids for a wait attribute."""
        with self._lock:
            retired_wait_request_ids = self._get_retired_wait_request_ids()
            expired: dict[int, float] = {}
            if acknowledgment_attr in retired_wait_request_ids:
                now = time.time()
                for rid, timestamp in list(
                    retired_wait_request_ids[acknowledgment_attr].items()
                ):
                    if now - timestamp > self._retired_wait_ttl_seconds:
                        expired[rid] = timestamp
                        retired_wait_request_ids[acknowledgment_attr].pop(rid, None)
                if not retired_wait_request_ids[acknowledgment_attr]:
                    retired_wait_request_ids.pop(acknowledgment_attr, None)
            return expired

    def wait_for_request_ack(
        self,
        acknowledgment_attr: str,
        request_id: int,
        *,
        timeout_seconds: float,
    ) -> bool:
        """Wait for a request-scoped acknowledgment flag."""
        timeout = self._get_timeout()
        acknowledgment = self._get_acknowledgment()
        wait_acks = self._get_wait_acks()
        ack_key = (acknowledgment_attr, request_id)
        start_time = time.time()
        while time.time() - start_time < timeout_seconds:
            if ack_key in wait_acks:
                return True
            time.sleep(0.01)
        return False

    def record_routing_wait_error(
        self,
        *,
        acknowledgment_attr: str,
        routing_error_reason: str | None,
        request_id: int | None = None,
    ) -> None:
        """Record non-success routing responses into shared wait state."""
        if routing_error_reason:
            self.set_wait_error(
                acknowledgment_attr,
                f"Routing error: {routing_error_reason}",
                request_id=request_id,
            )


class SendPipeline:
    """Send pipeline for transmitting packets to the radio.

    This class encapsulates all send-related functionality, including data发送,
    position, telemetry, waypoint, and traceroute operations.
    """

    def __init__(self, interface: "MeshInterface") -> None:
        """Initialize the send pipeline with a parent MeshInterface.

        Parameters
        ----------
        interface : MeshInterface
            The parent MeshInterface instance providing access to interface state.
        """
        self._interface = interface

    @property
    def _node_db_lock(self) -> threading.RLock:
        """Return the node database lock from the parent interface."""
        return self._interface._node_db_lock

    @property
    def _request_wait_runtime(self) -> _RequestWaitRuntime:
        """Return the request wait runtime from the parent interface."""
        return self._interface._request_wait_runtime

    @property
    def _queue_send_runtime(self) -> Any:
        """Return the queue send runtime from the parent interface."""
        return self._interface._queue_send_runtime

    @property
    def localNode(self) -> Any:
        """Return the local node from the parent interface."""
        return self._interface.localNode

    @property
    def myInfo(self) -> Any:
        """Return the myInfo from the parent interface."""
        return self._interface.myInfo

    @property
    def nodes(self) -> dict[str, dict[str, Any]] | None:
        """Return the nodes dictionary from the parent interface."""
        return self._interface.nodes

    @property
    def nodesByNum(self) -> dict[int, dict[str, Any]] | None:
        """Return the nodes by number dictionary from the parent interface."""
        return self._interface.nodesByNum

    @property
    def configId(self) -> int | None:
        """Return the config ID from the parent interface."""
        return self._interface.configId

    @property
    def noProto(self) -> bool:
        """Return the noProto flag from the parent interface."""
        return self._interface.noProto

    @property
    def _acknowledgment(self) -> Acknowledgment:
        """Return the acknowledgment from the parent interface."""
        return self._interface._acknowledgment

    @property
    def _timeout(self) -> Timeout:
        """Return the timeout from the parent interface."""
        return self._interface._timeout

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
        """Send UTF-8 text to a node (or broadcast) and return the transmitted MeshPacket."""
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
        """Send a high-priority alert text to a node."""
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
        """Send an MQTT client-proxy message through the radio."""
        prox = mesh_pb2.MqttClientProxyMessage()
        prox.topic = topic
        prox.data = data
        toRadio = mesh_pb2.ToRadio()
        toRadio.mqttClientProxyMessage.CopyFrom(prox)
        self._send_to_radio(toRadio)

    def sendData(
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
        """Send a payload to a mesh node."""
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

    def _send_data_with_wait(
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
        """Send payload data while optionally pre-registering request-scoped wait bookkeeping."""
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
            raise self._interface.MeshInterfaceError("Data payload too big")

        if portNum == portnums_pb2.PortNum.UNKNOWN_APP:
            raise self._interface.MeshInterfaceError(
                "A non-zero port number must be specified"
            )

        meshPacket = mesh_pb2.MeshPacket()
        meshPacket.channel = channelIndex
        meshPacket.decoded.payload = payload
        meshPacket.decoded.portnum = portNum
        meshPacket.decoded.want_response = wantResponse
        meshPacket.id = self._interface._generate_packet_id()
        while meshPacket.id == 0:
            meshPacket.id = self._interface._generate_packet_id()
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

    def _extract_request_id_from_packet(self, packet: dict[str, Any]) -> int | None:
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

    def _extract_request_id_from_sent_packet(self, packet: object) -> int | None:
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
        """Prune expired retired request ids for a wait attribute."""
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
            error_factory=self._interface.MeshInterfaceError,
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
        """Wait for a request-scoped acknowledgment flag."""
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
        """Process a position response packet and emit a concise human-readable summary."""
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
        """Send the device's position to a specific node or to broadcast."""
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
                raise self._interface.MeshInterfaceError(RESPONSE_WAIT_REQID_ERROR)
            self.waitForPosition(request_id=request_id)
        return d

    def onResponseTraceRoute(self, p: dict[str, Any]) -> None:
        """Emit human-readable traceroute results from a RouteDiscovery payload."""
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
            """Return a human-readable identifier for a numeric node."""
            return self._interface._node_num_to_id(node_num, False) or f"{node_num:08x}"

        def _format_snr(snr_value: int | None) -> str:
            """Format an SNR value encoded in quarter-dB units into a human-readable string."""
            return (
                str(snr_value / 4)
                if snr_value is not None and snr_value != UNKNOWN_SNR_QUARTER_DB
                else "?"
            )

        def _append_hop(route_text: str, node_num: int, snr_text: str) -> str:
            """Append a hop description to an existing route string."""
            return f"{route_text} --> {_node_label(node_num)} ({snr_text}dB)"

        route_towards = asDict.get("route", [])
        snr_towards = asDict.get("snrTowards", [])
        snr_towards_valid = len(snr_towards) == len(route_towards) + 1

        _emit_response_summary("Route traced towards destination:")
        routeStr = _node_label(p["to"])
        for idx, nodeNum in enumerate(route_towards):
            hop_snr = _format_snr(snr_towards[idx]) if snr_towards_valid else "?"
            routeStr = _append_hop(routeStr, nodeNum, hop_snr)
        final_towards_snr = _format_snr(snr_towards[-1]) if snr_towards_valid else "?"
        routeStr = _append_hop(routeStr, p["from"], final_towards_snr)

        _emit_response_summary(routeStr)

        route_back = asDict.get("routeBack", [])
        snr_back = asDict.get("snrBack", [])
        backValid = "hopStart" in p and len(snr_back) == len(route_back) + 1
        if backValid:
            _emit_response_summary("Route traced back to us:")
            routeStr = _node_label(p["from"])
            for idx, nodeNum in enumerate(route_back):
                routeStr = _append_hop(routeStr, nodeNum, _format_snr(snr_back[idx]))
            routeStr = _append_hop(routeStr, p["to"], _format_snr(snr_back[-1]))
            _emit_response_summary(routeStr)

        self._mark_wait_acknowledged(
            WAIT_ATTR_TRACEROUTE,
            request_id=request_id,
        )

    def sendTraceRoute(
        self, dest: int | str, hopLimit: int, channelIndex: int = 0
    ) -> None:
        """Initiate a traceroute request toward a destination node and wait for responses."""
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
        with self._node_db_lock:
            node_count = len(self.nodes) if self.nodes else 0
        nodes_based_factor = (node_count - 1) if node_count else (hopLimit + 1)
        waitFactor = max(1, min(nodes_based_factor, hopLimit + 1))
        request_id = self._extract_request_id_from_sent_packet(packet)
        if request_id is None:
            raise self._interface.MeshInterfaceError(RESPONSE_WAIT_REQID_ERROR)
        self.waitForTraceRoute(waitFactor, request_id=request_id)

    def sendTelemetry(
        self,
        destinationId: int | str = BROADCAST_ADDR,
        wantResponse: bool = False,
        channelIndex: int = 0,
        telemetryType: TelemetryType | str = DEFAULT_TELEMETRY_TYPE,
        hopLimit: int | None = None,
    ) -> None:
        """Send a telemetry message to a node or broadcast and optionally wait for a telemetry response."""
        telemetry_type = telemetryType
        if telemetry_type not in VALID_TELEMETRY_TYPE_SET:
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
                raise self._interface.MeshInterfaceError(RESPONSE_WAIT_REQID_ERROR)
            self.waitForTelemetry(request_id=request_id)

    def onResponseTelemetry(self, p: dict[str, Any]) -> None:
        """Handle an incoming telemetry response."""
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
                    telemetry_dict = google.protobuf.json_format.MessageToDict(
                        telemetry
                    )
                    for key, value in telemetry_dict.items():
                        if key != "time":
                            if isinstance(value, dict):
                                _emit_response_summary(f"{key}:")
                                for sub_key, sub_value in value.items():
                                    _emit_response_summary(f"  {sub_key}: {sub_value}")
                            else:
                                _emit_response_summary(f"{key}: {value}")
            except Exception as exc:
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
        """Handle a waypoint response or routing error contained in a received packet."""
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

    def sendWaypoint(
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
        """Send a waypoint to a node or broadcast."""
        w = mesh_pb2.Waypoint()
        w.name = name
        w.description = description
        w.icon = int(icon)
        w.expire = expire
        if waypoint_id is None:
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
                raise self._interface.MeshInterfaceError(RESPONSE_WAIT_REQID_ERROR)
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
        """Delete a waypoint by sending a Waypoint message with expire=0 to a destination."""
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
                raise self._interface.MeshInterfaceError(RESPONSE_WAIT_REQID_ERROR)
            self.waitForWaypoint(request_id=request_id)
        return d

    def _add_response_handler(
        self,
        requestId: int,
        callback: Callable[[dict[str, Any]], Any],
        ackPermitted: bool = False,
    ) -> None:
        """Register a response callback for a specific request identifier."""
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
        """Send a MeshPacket to a specific node or broadcast."""
        with self._node_db_lock:
            my_node_num = self.myInfo.my_node_num if self.myInfo is not None else None

        if my_node_num is not None and destinationId != my_node_num:
            self._interface._wait_connected()

        toRadio = mesh_pb2.ToRadio()

        nodeNum: int = 0
        if destinationId is None:
            raise self._interface.MeshInterfaceError("destinationId must not be None")
        elif isinstance(destinationId, int):
            nodeNum = destinationId
        elif destinationId == BROADCAST_ADDR:
            nodeNum = BROADCAST_NUM
        elif destinationId == LOCAL_ADDR:
            if my_node_num is not None:
                nodeNum = my_node_num
            else:
                raise self._interface.MeshInterfaceError("No myInfo found.")
        elif isinstance(destinationId, str):
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
                        raise self._interface.MeshInterfaceError(
                            _format_missing_node_num_error(destinationId)
                        )
                elif has_nodes:
                    raise self._interface.MeshInterfaceError(
                        _format_node_not_found_in_db_error(destinationId)
                    )
                else:
                    raise self._interface.MeshInterfaceError(
                        _format_node_db_unavailable_error(destinationId)
                    )
        else:
            with self._node_db_lock:
                has_nodes = self.nodes is not None
            if has_nodes:
                raise self._interface.MeshInterfaceError(
                    _format_node_not_found_in_db_error(destinationId)
                )
            raise self._interface.MeshInterfaceError(
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

        if meshPacket.id == 0:
            meshPacket.id = self._interface._generate_packet_id()

        toRadio.packet.CopyFrom(meshPacket)
        if self.noProto:
            logger.warning(
                "Not sending packet because protocol use is disabled by noProto"
            )
        else:
            logger.debug("Sending packet: %s", stripnl(meshPacket))
            self._send_to_radio(toRadio)
        return meshPacket

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
        """Block until the radio configuration and the local node's configuration are available."""
        success = (
            self._timeout.waitForSet(self._interface, attrs=("myInfo", "nodes"))
            and self.localNode.waitForConfig()
        )
        if not success:
            raise self._interface.MeshInterfaceError(
                "Timed out waiting for interface config"
            )

    def waitForAckNak(self) -> None:
        """Wait until an acknowledgement (ACK) or negative acknowledgement (NAK) is received or the wait times out."""
        success = self._timeout.waitForAckNak(self._acknowledgment)
        self._raise_wait_error_if_present(WAIT_ATTR_NAK)
        if not success:
            raise self._interface.MeshInterfaceError(
                "Timed out waiting for an acknowledgment"
            )

    def waitForTraceRoute(
        self, waitFactor: float, request_id: int | None = None
    ) -> None:
        """Wait for trace route completion using the configured timeout."""
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
                raise self._interface.MeshInterfaceError(
                    "Timed out waiting for traceroute"
                )
        finally:
            self._retire_wait_request(WAIT_ATTR_TRACEROUTE, request_id=request_id)

    def waitForTelemetry(self, request_id: int | None = None) -> None:
        """Wait for a telemetry response or until the configured timeout elapses."""
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
                raise self._interface.MeshInterfaceError(
                    "Timed out waiting for telemetry"
                )
        finally:
            self._retire_wait_request(WAIT_ATTR_TELEMETRY, request_id=request_id)

    def waitForPosition(self, request_id: int | None = None) -> None:
        """Block until a position acknowledgment is received."""
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
                raise self._interface.MeshInterfaceError(
                    "Timed out waiting for position"
                )
        finally:
            self._retire_wait_request(WAIT_ATTR_POSITION, request_id=request_id)

    def waitForWaypoint(self, request_id: int | None = None) -> None:
        """Block until a waypoint acknowledgment is received."""
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
                raise self._interface.MeshInterfaceError(
                    "Timed out waiting for waypoint"
                )
        finally:
            self._retire_wait_request(WAIT_ATTR_WAYPOINT, request_id=request_id)

    def _send_to_radio(self, toRadio: mesh_pb2.ToRadio) -> None:
        """Queue and transmit a ToRadio protobuf to the radio device."""
        if self.noProto:
            logger.warning(
                "Not sending packet because protocol use is disabled by noProto"
            )
            return

        self._queue_send_runtime.send_to_radio(
            toRadio,
            send_impl=self._send_to_radio_impl,
            pop_for_send=self._interface._queue_pop_for_send,
            sleep_fn=time.sleep,
        )

    def _send_to_radio_impl(self, toRadio: mesh_pb2.ToRadio) -> None:
        """Transport hook that delivers a ToRadio protobuf to the radio device."""
        self._interface._send_to_radio_impl(toRadio)

    def _send_disconnect(self) -> None:
        """Notify the radio device that this interface is disconnecting."""
        m = mesh_pb2.ToRadio()
        m.disconnect = True
        self._send_to_radio(m)

    def sendHeartbeat(self) -> None:
        """Send a heartbeat message to the radio to indicate the interface is alive."""
        p = mesh_pb2.ToRadio()
        p.heartbeat.CopyFrom(mesh_pb2.Heartbeat())
        self._send_to_radio(p)
