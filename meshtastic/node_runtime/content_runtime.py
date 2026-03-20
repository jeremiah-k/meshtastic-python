"""Ringtone/canned-message content cache and request/response runtimes."""

import logging
import threading
from typing import TYPE_CHECKING, Any, Callable

from meshtastic.node_runtime.shared import (
    MAX_CANNED_MESSAGE_LENGTH,
    MAX_RINGTONE_LENGTH,
)
from meshtastic.protobuf import admin_pb2, mesh_pb2

if TYPE_CHECKING:
    from meshtastic.node import Node

logger = logging.getLogger(__name__)

class _NodeContentCacheStore:
    """Owns ringtone/canned-message cache state, fragment storage, and invalidation."""

    def __init__(self, node: "Node") -> None:
        self._node = node

    def get_cached_ringtone(self) -> str | None:
        """Return cached full ringtone value when already present."""
        with self._node._ringtone_lock:  # noqa: SLF001
            if self._node.ringtone:
                logger.debug("ringtone:%s", self._node.ringtone)
                return self._node.ringtone
            return None

    def clear_ringtone_fragment(self) -> None:
        """Clear stale ringtone fragment state before issuing a read request."""
        with self._node._ringtone_lock:  # noqa: SLF001
            self._node.ringtonePart = None

    def store_ringtone_fragment(self, ringtone_fragment: str) -> None:
        """Store ringtone fragment from the latest response packet."""
        with self._node._ringtone_lock:  # noqa: SLF001
            self._node.ringtonePart = ringtone_fragment
        logger.debug("self.ringtonePart:%s", ringtone_fragment)

    def resolve_ringtone_after_read(self) -> str | None:
        """Resolve ringtone result after a read wait by preferring full cache, then fragment."""
        with self._node._ringtone_lock:  # noqa: SLF001
            if self._node.ringtone:
                logger.debug("ringtone:%s", self._node.ringtone)
                return self._node.ringtone
            if self._node.ringtonePart:
                self._node.ringtone = self._node.ringtonePart
                logger.debug("ringtone:%s", self._node.ringtone)
                return self._node.ringtone
            return None

    def invalidate_ringtone_cache(self) -> None:
        """Invalidate ringtone full/fragment cache after writes."""
        with self._node._ringtone_lock:  # noqa: SLF001
            self._node.ringtone = None
            self._node.ringtonePart = None

    def get_cached_canned_message(self) -> str | None:
        """Return cached full canned-message value when already present."""
        with self._node._canned_message_lock:  # noqa: SLF001
            if self._node.cannedPluginMessage:
                logger.debug("canned_plugin_message:%s", self._node.cannedPluginMessage)
                return self._node.cannedPluginMessage
            return None

    def clear_canned_message_fragment(self) -> None:
        """Clear stale canned-message fragment state before issuing a read request."""
        with self._node._canned_message_lock:  # noqa: SLF001
            self._node.cannedPluginMessageMessages = None

    def store_canned_message_fragment(self, canned_messages: str) -> None:
        """Store canned-message fragment payload from response packets."""
        with self._node._canned_message_lock:  # noqa: SLF001
            self._node.cannedPluginMessageMessages = canned_messages
        logger.debug("self.cannedPluginMessageMessages:%s", canned_messages)

    def resolve_canned_message_after_read(self) -> str | None:
        """Resolve canned-message result after a read wait."""
        with self._node._canned_message_lock:  # noqa: SLF001
            if self._node.cannedPluginMessage:
                logger.debug("canned_plugin_message:%s", self._node.cannedPluginMessage)
                return self._node.cannedPluginMessage
            logger.debug(
                "self.cannedPluginMessageMessages:%s",
                self._node.cannedPluginMessageMessages,
            )
            if self._node.cannedPluginMessageMessages:
                self._node.cannedPluginMessage = self._node.cannedPluginMessageMessages
                logger.debug("canned_plugin_message:%s", self._node.cannedPluginMessage)
                return self._node.cannedPluginMessage
            return None

    def invalidate_canned_message_cache(self) -> None:
        """Invalidate canned-message full/fragment cache after writes."""
        with self._node._canned_message_lock:  # noqa: SLF001
            self._node.cannedPluginMessage = None
            self._node.cannedPluginMessageMessages = None


class _NodeContentResponseRuntime:
    """Owns ringtone/canned-message response parsing and fragment cache updates."""

    def __init__(self, node: "Node", *, cache_store: _NodeContentCacheStore) -> None:
        self._node = node
        self._cache_store = cache_store

    @staticmethod
    def _has_routing_error(decoded: dict[str, Any]) -> bool:
        """Return True when decoded routing payload contains a non-NONE error reason."""
        if "routing" in decoded and decoded["routing"]["errorReason"] != "NONE":
            logger.error("Error on response: %s", decoded["routing"]["errorReason"])
            return True
        return False

    def handle_ringtone_response(self, packet: dict[str, Any]) -> None:
        """Parse ringtone response packet and store ringtone fragment when valid."""
        logger.debug("onResponseRequestRingtone() p:%s", packet)
        decoded = packet["decoded"]
        if self._has_routing_error(decoded):
            return
        if "admin" in decoded and "raw" in decoded["admin"]:
            ringtone_part = decoded["admin"]["raw"].get_ringtone_response
            self._cache_store.store_ringtone_fragment(ringtone_part)

    def handle_canned_message_response(self, packet: dict[str, Any]) -> None:
        """Parse canned-message response packet and store payload fragment when valid."""
        logger.debug(
            "onResponseRequestCannedMessagePluginMessageMessages() p:%s", packet
        )
        decoded = packet["decoded"]
        if self._has_routing_error(decoded):
            return
        if "admin" in decoded and "raw" in decoded["admin"]:
            canned_messages = decoded["admin"][
                "raw"
            ].get_canned_message_module_messages_response
            self._cache_store.store_canned_message_fragment(canned_messages)


class _NodeAdminContentRuntime:
    """Owns admin request/wait orchestration for ringtone and canned-message reads/writes."""

    def __init__(
        self,
        node: "Node",
        *,
        cache_store: _NodeContentCacheStore,
        response_runtime: _NodeContentResponseRuntime,
    ) -> None:
        self._node = node
        self._cache_store = cache_store
        self._response_runtime = response_runtime

    def _module_available_or_warn(
        self, excluded_bit: int, warning_message: str
    ) -> bool:
        """Evaluate module availability and emit the legacy warning when unavailable."""
        if not self._node.module_available(excluded_bit):
            logger.warning("%s", warning_message)
            return False
        return True

    def _send_content_read_request(
        self,
        *,
        build_request: Callable[[admin_pb2.AdminMessage], None],
        handle_response: Callable[[dict[str, Any]], None],
        skipped_send_debug_message: str,
        timeout_warning_message: str,
        resolve_result: Callable[[], str | None],
    ) -> str | None:
        """Send content read request, wait for callback, and resolve cached result."""
        response_event = threading.Event()

        def _on_response(packet: dict[str, Any]) -> None:
            try:
                handle_response(packet)
            finally:
                response_event.set()

        request_message = admin_pb2.AdminMessage()
        build_request(request_message)
        request = self._node._send_admin(
            request_message,
            wantResponse=True,
            onResponse=_on_response,
        )
        if request is None:
            logger.debug("%s", skipped_send_debug_message)
            return None
        if not response_event.wait(timeout=self._node._timeout.expireTimeout):
            logger.warning("%s", timeout_warning_message)
            return None
        return resolve_result()

    def _select_write_response_handler(
        self,
    ) -> Callable[[dict[str, Any]], Any] | None:
        """Return legacy ACK callback selection for local-vs-remote writes."""
        if self._node == self._node.iface.localNode:
            return None
        return self._node.onAckNak

    def read_ringtone(self) -> str | None:
        """Read ringtone using cached-short-circuit + request/wait orchestration."""
        logger.debug("in get_ringtone()")
        if not self._module_available_or_warn(
            mesh_pb2.EXTNOTIF_CONFIG,
            "External Notification module not present (excluded by firmware)",
        ):
            return None
        cached_ringtone = self._cache_store.get_cached_ringtone()
        if cached_ringtone is not None:
            return cached_ringtone
        self._cache_store.clear_ringtone_fragment()
        return self._send_content_read_request(
            build_request=lambda message: setattr(
                message, "get_ringtone_request", True
            ),
            handle_response=self._response_runtime.handle_ringtone_response,
            skipped_send_debug_message=(
                "Skipping ringtone wait because protocol send was not started"
            ),
            timeout_warning_message="Timed out waiting for ringtone response",
            resolve_result=self._cache_store.resolve_ringtone_after_read,
        )

    def write_ringtone(self, ringtone: str) -> mesh_pb2.MeshPacket | None:
        """Write ringtone payload and invalidate local ringtone cache."""
        if not self._module_available_or_warn(
            mesh_pb2.EXTNOTIF_CONFIG,
            "External Notification module not present (excluded by firmware)",
        ):
            return None
        if len(ringtone) > MAX_RINGTONE_LENGTH:
            self._node._raise_interface_error(  # noqa: SLF001
                f"The ringtone must be {MAX_RINGTONE_LENGTH} characters or fewer."
            )
        self._node.ensureSessionKey()
        request_message = admin_pb2.AdminMessage()
        request_message.set_ringtone_message = ringtone
        logger.debug("Setting ringtone '%s'", ringtone)
        send_result = self._node._send_admin(
            request_message,
            onResponse=self._select_write_response_handler(),
        )
        self._cache_store.invalidate_ringtone_cache()
        return send_result

    def read_canned_message(self) -> str | None:
        """Read canned-message payload using cached-short-circuit + request/wait orchestration."""
        logger.debug("in get_canned_message()")
        if not self._module_available_or_warn(
            mesh_pb2.CANNEDMSG_CONFIG,
            "Canned Message module not present (excluded by firmware)",
        ):
            return None
        cached_canned_message = self._cache_store.get_cached_canned_message()
        if cached_canned_message is not None:
            return cached_canned_message
        self._cache_store.clear_canned_message_fragment()
        return self._send_content_read_request(
            build_request=lambda message: setattr(
                message,
                "get_canned_message_module_messages_request",
                True,
            ),
            handle_response=self._response_runtime.handle_canned_message_response,
            skipped_send_debug_message=(
                "Skipping canned-message wait because protocol send was not started"
            ),
            timeout_warning_message="Timed out waiting for canned message response",
            resolve_result=self._cache_store.resolve_canned_message_after_read,
        )

    def write_canned_message(self, message: str) -> mesh_pb2.MeshPacket | None:
        """Write canned-message payload and invalidate local canned-message cache."""
        if not self._module_available_or_warn(
            mesh_pb2.CANNEDMSG_CONFIG,
            "Canned Message module not present (excluded by firmware)",
        ):
            return None
        if len(message) > MAX_CANNED_MESSAGE_LENGTH:
            self._node._raise_interface_error(  # noqa: SLF001
                f"The canned message must be {MAX_CANNED_MESSAGE_LENGTH} characters or fewer."
            )
        self._node.ensureSessionKey()
        request_message = admin_pb2.AdminMessage()
        request_message.set_canned_message_module_messages = message
        logger.debug("Setting canned message '%s'", message)
        send_result = self._node._send_admin(
            request_message,
            onResponse=self._select_write_response_handler(),
        )
        self._cache_store.invalidate_canned_message_cache()
        return send_result
