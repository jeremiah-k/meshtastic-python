"""Admin transport mechanics for ADMIN_APP sends."""

import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from meshtastic.protobuf import admin_pb2, mesh_pb2, portnums_pb2

if TYPE_CHECKING:
    from meshtastic.node import Node

logger = logging.getLogger(__name__)


class _NodeAdminTransportRuntime:
    """Owns admin transport mechanics for ADMIN_APP sends."""

    def __init__(self, node: "Node") -> None:
        self._node = node

    def _resolve_admin_index(self, admin_index: int | None) -> int:
        """Resolve None to auto-detected admin channel index; preserve explicit zero."""
        if admin_index is None:
            return self._node.iface.localNode._get_admin_channel_index()
        return admin_index

    def _send_admin(
        self,
        message: admin_pb2.AdminMessage,
        *,
        want_response: bool = False,
        on_response: Callable[[dict[str, Any]], Any] | None = None,
        admin_index: int | None = None,
    ) -> mesh_pb2.MeshPacket | None:
        """Send an AdminMessage via iface.sendData with preserved transport semantics.

        Returns
        -------
        MeshPacket | None
            The sent packet, or None if the send was skipped (e.g., when noProto is enabled).
            Callers must check for None to avoid applying local state changes when the
            device was not actually updated.
        """
        if self._node.noProto:
            logger.warning(
                "Not sending packet because protocol use is disabled by noProto"
            )
            return None

        resolved_admin_index = self._resolve_admin_index(admin_index)
        logger.debug("adminIndex:%s", resolved_admin_index)

        node_info = self._node.iface._get_or_create_by_num(self._node.nodeNum)
        passkey = node_info.get("adminSessionPassKey")
        outbound_message = admin_pb2.AdminMessage()
        outbound_message.CopyFrom(message)
        if isinstance(passkey, bytes):
            outbound_message.session_passkey = passkey

        return self._node.iface.sendData(
            outbound_message,
            self._node.nodeNum,
            portNum=portnums_pb2.PortNum.ADMIN_APP,
            wantAck=True,
            wantResponse=want_response,
            onResponse=on_response,
            channelIndex=resolved_admin_index,
            pkiEncrypted=True,
        )
