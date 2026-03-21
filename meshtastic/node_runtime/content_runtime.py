"""Ringtone/canned-message content cache and request/response runtimes."""

import logging
import threading
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

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
            if self._node.ringtone is not None:
                logger.debug("ringtone cached (%d chars)", len(self._node.ringtone))
                return self._node.ringtone
            return None

    def clear_ringtone_fragment(self) -> None:
        """Clear stale ringtone fragment state before issuing a read request."""
        with self._node._ringtone_lock:  # noqa: SLF001
            self._node.ringtonePart = None
        logger.debug("ringtone fragment cache cleared")

    def store_ringtone_fragment(self, ringtone_fragment: str) -> None:
        """Store ringtone fragment from the latest response packet."""
        with self._node._ringtone_lock:  # noqa: SLF001
            self._node.ringtonePart = ringtone_fragment
        logger.debug("ringtone fragment stored (%d chars)", len(ringtone_fragment))

    def resolve_ringtone_after_read(self) -> str | None:
        """Resolve ringtone result after a read wait by preferring full cache, then fragment."""
        with self._node._ringtone_lock:  # noqa: SLF001
            if self._node.ringtone is not None:
                logger.debug(
                    "ringtone resolved from full cache (%d chars)",
                    len(self._node.ringtone),
                )
                return self._node.ringtone
            if self._node.ringtonePart is not None:
                self._node.ringtone = self._node.ringtonePart
                logger.debug(
                    "ringtone resolved from fragment cache (%d chars)",
                    len(self._node.ringtone),
                )
                return self._node.ringtone
            return None

    def invalidate_ringtone_cache(self) -> None:
        """Invalidate ringtone full/fragment cache after writes."""
        with self._node._ringtone_lock:  # noqa: SLF001
            self._node.ringtone = None
            self._node.ringtonePart = None
        logger.debug("ringtone cache invalidated")

    def get_cached_canned_message(self) -> str | None:
        """Return cached full canned-message value when already present."""
        with self._node._canned_message_lock:  # noqa: SLF001
            if self._node.cannedPluginMessage is not None:
                logger.debug(
                    "canned message cached (%d chars)",
                    len(self._node.cannedPluginMessage),
                )
                return self._node.cannedPluginMessage
            return None

    def clear_canned_message_fragment(self) -> None:
        """Clear stale canned-message fragment state before issuing a read request."""
        with self._node._canned_message_lock:  # noqa: SLF001
            self._node.cannedPluginMessageMessages = None
        logger.debug("canned message fragment cache cleared")

    def store_canned_message_fragment(self, canned_messages: str) -> None:
        """Store canned-message fragment payload from response packets."""
        with self._node._canned_message_lock:  # noqa: SLF001
            self._node.cannedPluginMessageMessages = canned_messages
        logger.debug("canned message fragment stored (%d chars)", len(canned_messages))

    def resolve_canned_message_after_read(self) -> str | None:
        """Resolve canned-message result after a read wait."""
        with self._node._canned_message_lock:  # noqa: SLF001
            if self._node.cannedPluginMessage is not None:
                logger.debug(
                    "canned message resolved from full cache (%d chars)",
                    len(self._node.cannedPluginMessage),
                )
                return self._node.cannedPluginMessage
            if self._node.cannedPluginMessageMessages is not None:
                self._node.cannedPluginMessage = self._node.cannedPluginMessageMessages
                logger.debug(
                    "canned message resolved from fragment cache (%d chars)",
                    len(self._node.cannedPluginMessage),
                )
                return self._node.cannedPluginMessage
            return None

    def invalidate_canned_message_cache(self) -> None:
        """Invalidate canned-message full/fragment cache after writes."""
        with self._node._canned_message_lock:  # noqa: SLF001
            self._node.cannedPluginMessage = None
            self._node.cannedPluginMessageMessages = None
        logger.debug("canned message cache invalidated")


class _NodeContentResponseRuntime:
    """Owns ringtone/canned-message response parsing and fragment cache updates."""

    def __init__(self, node: "Node", *, cache_store: _NodeContentCacheStore) -> None:
        self._node = node
        self._cache_store = cache_store

    @staticmethod
    def _has_routing_error(decoded: dict[str, Any]) -> bool:
        """Return True when decoded routing payload contains a non-NONE error reason."""
        routing = decoded.get("routing")
        if not isinstance(routing, dict):
            return False
        error_reason = routing.get("errorReason")
        if isinstance(error_reason, str) and error_reason != "NONE":
            logger.error("Error on response: %s", error_reason)
            return True
        return False

    def handle_ringtone_response(self, packet: dict[str, Any]) -> bool:
        """Parse ringtone response packet and return True for terminal callbacks."""
        logger.debug("onResponseRequestRingtone()")
        decoded = packet.get("decoded")
        if not isinstance(decoded, dict):
            logger.warning("Unexpected ringtone response without decoded payload")
            return False
        if "routing" in decoded:
            return self._has_routing_error(decoded)
        admin_message = decoded.get("admin")
        if not isinstance(admin_message, dict):
            logger.warning("Unexpected ringtone response without admin payload")
            return False
        raw_admin = admin_message.get("raw")
        if raw_admin is None or not hasattr(raw_admin, "get_ringtone_response"):
            logger.warning("Unexpected ringtone response without raw ringtone data")
            return False
        try:
            ringtone_part = raw_admin.get_ringtone_response
            self._cache_store.store_ringtone_fragment(ringtone_part)
        except AttributeError:
            logger.warning("Failed to parse ringtone response payload")
            return False
        else:
            return True

    def handle_canned_message_response(self, packet: dict[str, Any]) -> bool:
        """Parse canned-message response packet and return True for terminal callbacks."""
        logger.debug("onResponseRequestCannedMessagePluginMessageMessages()")
        decoded = packet.get("decoded")
        if not isinstance(decoded, dict):
            logger.warning("Unexpected canned-message response without decoded payload")
            return False
        if "routing" in decoded:
            return self._has_routing_error(decoded)
        admin_message = decoded.get("admin")
        if not isinstance(admin_message, dict):
            logger.warning("Unexpected canned-message response without admin payload")
            return False
        raw_admin = admin_message.get("raw")
        if raw_admin is None or not hasattr(
            raw_admin, "get_canned_message_module_messages_response"
        ):
            logger.warning(
                "Unexpected canned-message response without raw message data"
            )
            return False
        try:
            canned_messages = raw_admin.get_canned_message_module_messages_response
            self._cache_store.store_canned_message_fragment(canned_messages)
        except AttributeError:
            logger.warning("Failed to parse canned-message response payload")
            return False
        else:
            return True


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
        handle_response: Callable[[dict[str, Any]], bool],
        skipped_send_debug_message: str,
        timeout_warning_message: str,
        resolve_result: Callable[[], str | None],
    ) -> str | None:
        """Send content read request, wait for callback, and resolve cached result."""
        response_event = threading.Event()

        def _on_response(packet: dict[str, Any]) -> None:
            if handle_response(packet):
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
        if self._node is self._node.iface.localNode:
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
        logger.debug("Setting ringtone (%d chars)", len(ringtone))
        send_result = self._node._send_admin(
            request_message,
            onResponse=self._select_write_response_handler(),
        )
        if send_result is not None:
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
        logger.debug("Setting canned message (%d chars)", len(message))
        send_result = self._node._send_admin(
            request_message,
            onResponse=self._select_write_response_handler(),
        )
        if send_result is not None:
            self._cache_store.invalidate_canned_message_cache()
        return send_result
