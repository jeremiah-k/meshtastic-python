"""ACK/NAK payload classification and acknowledgment flag mutation."""

import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from meshtastic.node import Node

logger = logging.getLogger(__name__)


class _NodeAckNakRuntime:
    """Owns ACK/NAK payload classification and acknowledgment flag mutation."""

    def __init__(self, node: "Node") -> None:
        self._node = node

    def _handle_ack_nak(self, packet: dict[str, Any]) -> None:
        """Classify ACK/NAK payload and update interface acknowledgment state."""
        decoded = packet.get("decoded")
        if not isinstance(decoded, dict):
            logger.warning(
                "Received ACK/NAK response without decoded payload: %s", packet
            )
            self._node.iface._acknowledgment.receivedNak = True
            return
        routing = decoded.get("routing")
        if not isinstance(routing, dict):
            logger.warning(
                "Received ACK/NAK response without routing details: %s", packet
            )
            self._node.iface._acknowledgment.receivedNak = True
            return

        error_reason = routing.get("errorReason", "NONE")
        if error_reason != "NONE":
            logger.warning("Received a NAK, error reason: %s", error_reason)
            self._node.iface._acknowledgment.receivedNak = True
            return

        from_value = packet.get("from")
        if from_value is None:
            logger.warning("Received ACK/NAK response without sender: %s", packet)
            self._node.iface._acknowledgment.receivedNak = True
            return
        try:
            from_num = int(from_value)
        except (TypeError, ValueError):
            logger.warning("Received ACK/NAK response with invalid sender: %s", packet)
            self._node.iface._acknowledgment.receivedNak = True
            return

        if from_num == self._node.iface.localNode.nodeNum:
            logger.info(
                "Received an implicit ACK. Packet will likely arrive, but cannot be guaranteed."
            )
            self._node.iface._acknowledgment.receivedImplAck = True
            return

        logger.info("Received an ACK.")
        self._node.iface._acknowledgment.receivedAck = True
