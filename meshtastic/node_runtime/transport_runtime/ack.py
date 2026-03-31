"""ACK/NAK payload classification and acknowledgment flag mutation."""

import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from meshtastic.node import Node

logger = logging.getLogger(__name__)

ERROR_REASON_NONE: str = "NONE"


class _NodeAckNakRuntime:
    """Owns ACK/NAK payload classification and acknowledgment flag mutation."""

    def __init__(self, node: "Node") -> None:
        self._node = node

    def _handle_ack_nak(
        self, packet: dict[str, Any]
    ) -> None:  # pylint: disable=too-many-return-statements
        """Classify ACK/NAK payload and update interface acknowledgment state."""
        if self._node.iface is None:
            logger.warning(
                "Received ACK/NAK response but interface is not available: packet_id=%s",
                packet.get("id"),
            )
            return
        ack_state = self._node.iface._acknowledgment
        decoded = packet.get("decoded")
        if not isinstance(decoded, dict):
            logger.warning(
                "Received ACK/NAK response without decoded payload: packet_id=%s, from=%s",
                packet.get("id"),
                packet.get("from"),
            )
            ack_state.receivedNak = True
            return
        routing = decoded.get("routing")
        if not isinstance(routing, dict):
            logger.warning(
                "Received ACK/NAK response without routing details: packet_id=%s, from=%s",
                packet.get("id"),
                packet.get("from"),
            )
            ack_state.receivedNak = True
            return

        error_reason = routing.get("errorReason", ERROR_REASON_NONE)
        if error_reason != ERROR_REASON_NONE:
            logger.warning("Received a NAK, error reason: %s", error_reason)
            ack_state.receivedNak = True
            return

        from_value = packet.get("from")
        if from_value is None:
            logger.warning(
                "Received ACK/NAK response without sender: packet_id=%s",
                packet.get("id"),
            )
            ack_state.receivedNak = True
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
            return

        local_node = self._node.iface.localNode
        if local_node is None:
            logger.warning(
                "Received ACK/NAK response but localNode is not available: packet_id=%s",
                packet.get("id"),
            )
            ack_state.receivedNak = True
            return

        if from_num == local_node.nodeNum:
            logger.info(
                "Received an implicit ACK. Packet will likely arrive, but cannot be guaranteed."
            )
            ack_state.receivedImplAck = True
            return

        logger.info("Received an ACK.")
        ack_state.receivedAck = True
