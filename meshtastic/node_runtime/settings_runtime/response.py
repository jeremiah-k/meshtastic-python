"""OnResponseRequestSettings routing, decode, mutation, and output behavior."""

import logging
from typing import TYPE_CHECKING, Any

from meshtastic.protobuf import admin_pb2
from meshtastic.util import camel_to_snake

if TYPE_CHECKING:
    from meshtastic.node import Node

logger = logging.getLogger(__name__)


class _NodeSettingsResponseRuntime:
    """Owns onResponseRequestSettings routing, decode, mutation, and output behavior."""

    def __init__(self, node: "Node") -> None:
        self._node = node

    def _resolve_local_config_target(
        self, admin_message: dict[str, Any]
    ) -> tuple[str, str, Any] | None:
        """Resolve getConfigResponse payload target, if present and valid."""
        response = admin_message.get("getConfigResponse")
        if response is None:
            return None
        if not response:
            logger.warning("Received empty config response from node.")
            return None
        response_field = next(iter(response.keys()))
        field_name = camel_to_snake(response_field)
        config_type = self._node.localConfig.DESCRIPTOR.fields_by_name.get(field_name)
        if config_type is None:
            logger.warning(
                "Ignoring unknown LocalConfig field in getConfigResponse: %s",
                field_name,
            )
            return None
        return (
            "get_config_response",
            field_name,
            getattr(self._node.localConfig, config_type.name),
        )

    def _resolve_module_config_target(
        self, admin_message: dict[str, Any]
    ) -> tuple[str, str, Any] | None:
        """Resolve getModuleConfigResponse payload target, if present and valid."""
        response = admin_message.get("getModuleConfigResponse")
        if response is None:
            return None
        if not response:
            logger.warning("Received empty module config response from node.")
            return None
        response_field = next(iter(response.keys()))
        field_name = camel_to_snake(response_field)
        config_type = self._node.moduleConfig.DESCRIPTOR.fields_by_name.get(field_name)
        if config_type is None:
            logger.warning(
                "Ignoring unknown ModuleConfig field in getModuleConfigResponse: %s",
                field_name,
            )
            return None
        return (
            "get_module_config_response",
            field_name,
            getattr(self._node.moduleConfig, config_type.name),
        )

    def _resolve_config_target(
        self,
        admin_message: dict[str, Any],
    ) -> tuple[str, str, Any] | None:
        """Resolve response type/field to destination config submessage."""
        local_target = self._resolve_local_config_target(admin_message)
        if local_target is not None:
            return local_target

        module_target = self._resolve_module_config_target(admin_message)
        if module_target is not None:
            return module_target

        logger.warning(
            "Did not receive a valid response. Make sure to have a shared channel named 'admin'."
        )
        return None

    def handle_settings_response(self, packet: dict[str, Any]) -> None:
        """Process one settings response packet with preserved ACK/NAK semantics."""
        logger.debug("handle_settings_response() response received")
        decoded = packet.get("decoded")
        if not isinstance(decoded, dict):
            logger.warning("Received malformed settings response (missing decoded).")
            self._node.iface._acknowledgment.receivedNak = True
            return
        routing = decoded.get("routing")
        if isinstance(routing, dict):
            error_reason = routing.get("errorReason")
            if isinstance(error_reason, str) and error_reason != "NONE":
                logger.error(
                    "Error on response: %s",
                    error_reason,
                )
                self._node.iface._acknowledgment.receivedNak = True
                return

        admin_message = decoded.get("admin")
        if not isinstance(admin_message, dict):
            logger.warning("Received malformed settings response (missing admin).")
            self._node.iface._acknowledgment.receivedNak = True
            return
        target = self._resolve_config_target(admin_message)
        if target is None:
            self._node.iface._acknowledgment.receivedNak = True
            return

        oneof, field_name, config_values = target
        raw_admin = admin_message.get("raw")
        if not isinstance(raw_admin, admin_pb2.AdminMessage):
            logger.warning("Received malformed settings response (invalid admin.raw).")
            self._node.iface._acknowledgment.receivedNak = True
            return
        parent_config = getattr(raw_admin, oneof)
        if not parent_config.HasField(field_name):
            logger.warning(
                "Received settings response without expected field '%s'.",
                field_name,
            )
            self._node.iface._acknowledgment.receivedNak = True
            return
        raw_config = getattr(parent_config, field_name)
        config_values.CopyFrom(raw_config)
        self._node.iface._acknowledgment.receivedAck = True
        logger.info("Received settings block: %s", field_name)
