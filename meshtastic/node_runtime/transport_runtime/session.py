"""Admin session runtime for session-key readiness checks."""

import logging
from typing import TYPE_CHECKING

from meshtastic.protobuf import admin_pb2

if TYPE_CHECKING:
    from meshtastic.node import Node

logger = logging.getLogger(__name__)


class _NodeAdminSessionRuntime:
    """Owns admin-session readiness checks and session-key request behavior."""

    def __init__(self, node: "Node") -> None:
        self._node = node

    def _ensure_session_key(self, *, admin_index: int | None = None) -> None:
        """Ensure session key is present, preserving historical noProto behavior."""
        if self._node.noProto:
            logger.warning(
                "Not ensuring session key, because protocol use is disabled by noProto"
            )
            return
        if (
            self._node.iface._get_or_create_by_num(self._node.nodeNum).get(
                "adminSessionPassKey"
            )
            is None
        ):
            self._node.requestConfig(
                admin_pb2.AdminMessage.SESSIONKEY_CONFIG,
                adminIndex=admin_index,
            )
