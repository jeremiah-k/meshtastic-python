"""Admin session runtime for session-key readiness checks."""

import logging
from typing import TYPE_CHECKING

from meshtastic.protobuf import admin_pb2

if TYPE_CHECKING:
    from meshtastic.node import Node

logger = logging.getLogger(__name__)


class _AdminSessionPassKeyProbe:
    """Probe to wait for adminSessionPassKey in node data."""

    def __init__(self, node: "Node") -> None:
        self._node = node

    @property
    def is_set(self) -> bool:
        """Return True if adminSessionPassKey is present and non-None."""
        node_info = self._node.iface._get_or_create_by_num(self._node.nodeNum)
        return node_info.get("adminSessionPassKey") is not None


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
            # Wait for adminSessionPassKey to be populated
            probe = _AdminSessionPassKeyProbe(self._node)
            key_received = self._node._timeout.waitForSet(probe, attrs=("is_set",))
            if not key_received:
                logger.warning("Timeout waiting for adminSessionPassKey after request")
