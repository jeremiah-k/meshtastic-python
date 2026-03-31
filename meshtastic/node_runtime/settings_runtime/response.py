"""OnResponseRequestSettings routing, decode, mutation, and output behavior."""

import logging
from typing import TYPE_CHECKING, Any

from meshtastic.protobuf import admin_pb2
from meshtastic.util import camel_to_snake

if TYPE_CHECKING:
    from meshtastic.node import Node

logger = logging.getLogger(__name__)

ERROR_REASON_NONE = "NONE"


class _NodeSettingsResponseRuntime:
    """Owns onResponseRequestSettings routing, decode, mutation, and output behavior."""

    def __init__(self, node: "Node") -> None:
        self._node = node

    # pylint: disable=too-many-positional-arguments
    def _resolve_config_response(
        self,
        admin_message: dict[str, Any],
        response_key: str,
        empty_warning: str,
        descriptor: Any,
        config_instance: Any,
        unknown_field_warning: str,
        response_type: str,
    ) -> tuple[str, str, Any] | None:
        """Resolve config response payload target, if present and valid."""
        response = admin_message.get(response_key)
        if response is None:
            return None
        if not response:
            logger.warning(empty_warning)
            return None
        response_field = next(iter(response))
        field_name = camel_to_snake(response_field)
        config_type = descriptor.fields_by_name.get(field_name)
        if config_type is None:
            logger.warning(unknown_field_warning, field_name)
            return None
        return (response_type, field_name, getattr(config_instance, field_name))

    def _resolve_local_config_target(
        self, admin_message: dict[str, Any]
    ) -> tuple[str, str, Any] | None:
        """Resolve getConfigResponse payload target, if present and valid."""
        return self._resolve_config_response(
            admin_message,
            response_key="getConfigResponse",
            empty_warning="Received empty config response from node.",
            descriptor=self._node.localConfig.DESCRIPTOR,
            config_instance=self._node.localConfig,
            unknown_field_warning="Ignoring unknown LocalConfig field in getConfigResponse: %s",
            response_type="get_config_response",
        )

    def _resolve_module_config_target(
        self, admin_message: dict[str, Any]
    ) -> tuple[str, str, Any] | None:
        """Resolve getModuleConfigResponse payload target, if present and valid."""
        return self._resolve_config_response(
            admin_message,
            response_key="getModuleConfigResponse",
            empty_warning="Received empty module config response from node.",
            descriptor=self._node.moduleConfig.DESCRIPTOR,
            config_instance=self._node.moduleConfig,
            unknown_field_warning="Ignoring unknown ModuleConfig field in getModuleConfigResponse: %s",
            response_type="get_module_config_response",
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
            "Did not receive a valid config response (no recognized config/module field)."
        )
        return None

    def handleSettingsResponse(self, packet: dict[str, Any]) -> None:
        """Process one settings response packet with preserved ACK/NAK semantics."""
        logger.debug("handleSettingsResponse() response received")
        decoded = packet.get("decoded")
        if not isinstance(decoded, dict):
            logger.warning("Received malformed settings response (missing decoded).")
            self._node.iface._acknowledgment.receivedNak = True
            return
        routing = decoded.get("routing")
        if isinstance(routing, dict):
            error_reason = routing.get("errorReason")
            if isinstance(error_reason, str) and error_reason != ERROR_REASON_NONE:
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
