"""ACK/NAK payload classification and acknowledgment flag mutation."""

import logging
from typing import TYPE_CHECKING, Any, cast

from meshtastic.node_runtime.shared import ERROR_REASON_NONE

if TYPE_CHECKING:
    from meshtastic.node import Node

logger = logging.getLogger(__name__)

WAIT_ATTR_NAK = "receivedNak"


class _NodeAckNakRuntime:
    """Owns ACK/NAK payload classification and acknowledgment flag mutation."""

    def __init__(self, node: "Node") -> None:
        self._node = node

    def _extract_request_id(self, packet: dict[str, Any]) -> int | None:
        """Extract request id from packet when interface helper is available."""
        extract_request_id = getattr(
            self._node.iface,
            "_extract_request_id_from_packet",
            None,
        )
        if not callable(extract_request_id):
            return None
        return cast(int | None, extract_request_id(packet))

    def _set_wait_nak_error(
        self,
        *,
        message: str,
        request_id: int | None,
    ) -> None:
        """Record scoped/unscoped wait errors for ACK/NAK waits when supported."""
        set_wait_error = getattr(self._node.iface, "_set_wait_error", None)
        if not callable(set_wait_error):
            return
        set_wait_error(WAIT_ATTR_NAK, message)
        if request_id is not None:
            set_wait_error(WAIT_ATTR_NAK, message, request_id=request_id)

    def _mark_wait_acknowledged(self, request_id: int | None) -> None:
        """Mark ACK wait completion for request-scoped waiters when supported."""
        mark_wait_acknowledged = getattr(
            self._node.iface,
            "_mark_wait_acknowledged",
            None,
        )
        if callable(mark_wait_acknowledged):
            mark_wait_acknowledged(WAIT_ATTR_NAK, request_id=request_id)

    # pylint: disable=too-many-return-statements
    def _handle_ack_nak(self, packet: dict[str, Any]) -> None:
        """Classify ACK/NAK payload and update interface acknowledgment state."""
        if self._node.iface is None:
            logger.warning(
                "Received ACK/NAK response but interface is not available: packet_id=%s",
                packet.get("id"),
            )
            return
        request_id = self._extract_request_id(packet)
        ack_state = self._node.iface._acknowledgment
        decoded = packet.get("decoded")
        if not isinstance(decoded, dict):
            logger.warning(
                "Received ACK/NAK response without decoded payload: packet_id=%s, from=%s",
                packet.get("id"),
                packet.get("from"),
            )
            ack_state.receivedNak = True
            self._set_wait_nak_error(
                message="Received malformed ACK/NAK response without decoded payload.",
                request_id=request_id,
            )
            return
        routing = decoded.get("routing")
        if not isinstance(routing, dict):
            logger.warning(
                "Received ACK/NAK response without routing details: packet_id=%s, from=%s",
                packet.get("id"),
                packet.get("from"),
            )
            ack_state.receivedNak = True
            self._set_wait_nak_error(
                message="Received malformed ACK/NAK response without routing details.",
                request_id=request_id,
            )
            return

        error_reason = routing.get("errorReason", ERROR_REASON_NONE)
        if error_reason != ERROR_REASON_NONE:
            logger.warning("Received a NAK, error reason: %s", error_reason)
            ack_state.receivedNak = True
            self._set_wait_nak_error(
                message=f"Routing error on response: {error_reason}",
                request_id=request_id,
            )
            return

        from_value = packet.get("from")
        if from_value is None:
            logger.warning(
                "Received ACK/NAK response without sender: packet_id=%s",
                packet.get("id"),
            )
            ack_state.receivedNak = True
            self._set_wait_nak_error(
                message="Received malformed ACK/NAK response without sender.",
                request_id=request_id,
            )
            return
        try:
            from_num = int(from_value)
        except (TypeError, ValueError):
            logger.warning(
                "Received ACK/NAK response with invalid sender: packet_id=%s, from=%s",
                packet.get("id"),
                from_value,
            )
            ack_state.receivedNak = True
            self._set_wait_nak_error(
                message="Received malformed ACK/NAK response with invalid sender.",
                request_id=request_id,
            )
            return

        local_node = self._node.iface.localNode
        if local_node is None:
            logger.warning(
                "Received ACK/NAK response but localNode is not available: packet_id=%s",
                packet.get("id"),
            )
            ack_state.receivedNak = True
            self._set_wait_nak_error(
                message="Received ACK/NAK response but local node is unavailable.",
                request_id=request_id,
            )
            return

        if from_num == local_node.nodeNum:
            logger.info(
                "Received an implicit ACK. Packet will likely arrive, but cannot be guaranteed."
            )
            ack_state.receivedImplAck = True
            self._mark_wait_acknowledged(request_id)
            return

        logger.info("Received an ACK.")
        ack_state.receivedAck = True
        self._mark_wait_acknowledged(request_id)
