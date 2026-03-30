"""Settings request/response and admin command-family runtime owners."""

import logging
from typing import TYPE_CHECKING, Any

from google.protobuf.descriptor import FieldDescriptor

from meshtastic.protobuf import admin_pb2

if TYPE_CHECKING:
    from meshtastic.node import Node

logger = logging.getLogger(__name__)


class _NodeSettingsMessageBuilder:
    """Owns settings request/write AdminMessage construction and field mapping."""

    def __init__(self, node: "Node") -> None:
        self._node = node

    def build_request_message(
        self, config_type: int | FieldDescriptor
    ) -> admin_pb2.AdminMessage:
        """Build request-config message from int or protobuf field descriptor."""
        message = admin_pb2.AdminMessage()
        if isinstance(config_type, int):
            message.get_config_request = config_type  # type: ignore[assignment] # pyright: ignore[reportAttributeAccessIssue]
            return message

        if config_type.containing_type.name == "LocalConfig":
            message.get_config_request = admin_pb2.AdminMessage.ConfigType.Value(
                f"{config_type.name.upper()}_CONFIG"
            )
            return message

        if config_type.containing_type.name in ("ModuleConfig", "LocalModuleConfig"):
            message.get_module_config_request = (
                config_type.index  # pyright: ignore[reportAttributeAccessIssue]
            )
            return message

        raise ValueError(
            f"Unsupported config descriptor: {config_type.name} in {config_type.containing_type.name}"
        )

    def _write_config_dispatch(self) -> dict[str, tuple[str, Any]]:
        """Return config-name mapping to (setter oneof, source config message)."""
        node = self._node
        dispatch = {
            "device": ("set_config", node.localConfig.device),
            "position": ("set_config", node.localConfig.position),
            "power": ("set_config", node.localConfig.power),
            "network": ("set_config", node.localConfig.network),
            "display": ("set_config", node.localConfig.display),
            "lora": ("set_config", node.localConfig.lora),
            "bluetooth": ("set_config", node.localConfig.bluetooth),
            "security": ("set_config", node.localConfig.security),
            "mqtt": ("set_module_config", node.moduleConfig.mqtt),
            "serial": ("set_module_config", node.moduleConfig.serial),
            "external_notification": (
                "set_module_config",
                node.moduleConfig.external_notification,
            ),
            "store_forward": ("set_module_config", node.moduleConfig.store_forward),
            "range_test": ("set_module_config", node.moduleConfig.range_test),
            "telemetry": ("set_module_config", node.moduleConfig.telemetry),
            "canned_message": ("set_module_config", node.moduleConfig.canned_message),
            "audio": ("set_module_config", node.moduleConfig.audio),
            "remote_hardware": (
                "set_module_config",
                node.moduleConfig.remote_hardware,
            ),
            "neighbor_info": ("set_module_config", node.moduleConfig.neighbor_info),
            "detection_sensor": (
                "set_module_config",
                node.moduleConfig.detection_sensor,
            ),
            "ambient_lighting": (
                "set_module_config",
                node.moduleConfig.ambient_lighting,
            ),
            "paxcounter": ("set_module_config", node.moduleConfig.paxcounter),
            "statusmessage": ("set_module_config", node.moduleConfig.statusmessage),
            "traffic_management": (
                "set_module_config",
                node.moduleConfig.traffic_management,
            ),
        }
        # Optional fields may appear in some protobuf schema revisions.
        if hasattr(node.localConfig, "sessionkey"):
            dispatch["sessionkey"] = ("set_config", node.localConfig.sessionkey)
        if hasattr(node.localConfig, "device_ui"):
            dispatch["device_ui"] = ("set_config", node.localConfig.device_ui)
        return dispatch

    def get_write_config_entry(self, config_name: str) -> tuple[str, Any] | None:
        """Return dispatch entry for one write-config section when available."""
        return self._write_config_dispatch().get(config_name)

    def build_write_message(self, config_name: str) -> admin_pb2.AdminMessage:
        """Build one set_config/set_module_config message for a config name."""
        config_entry = self.get_write_config_entry(config_name)
        if config_entry is None:
            self._node._raise_interface_error(  # noqa: SLF001
                f"Error: No valid config with name {config_name}"
            )

        message = admin_pb2.AdminMessage()
        setter_name, source_config = config_entry
        config_setter = getattr(message, setter_name)
        getattr(config_setter, config_name).CopyFrom(source_config)
        return message

    def validate_config_name(self, config_name: str) -> None:
        """Validate config-name dispatch key without constructing a message."""
        if self.get_write_config_entry(config_name) is None:
            self._node._raise_interface_error(  # noqa: SLF001
                f"Error: No valid config with name {config_name}"
            )
