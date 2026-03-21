"""Settings request/response and admin command-family runtime owners."""

import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from google.protobuf.descriptor import FieldDescriptor

from meshtastic.node_runtime.shared import (
    EMPTY_LONG_NAME_MSG,
    EMPTY_SHORT_NAME_MSG,
    MAX_LONG_NAME_LEN,
    MAX_SHORT_NAME_LEN,
)
from meshtastic.protobuf import admin_pb2, mesh_pb2
from meshtastic.util import camel_to_snake, toNodeNum

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

        message.get_module_config_request = (
            config_type.index  # pyright: ignore[reportAttributeAccessIssue]
        )
        return message

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


class _NodeSettingsRuntime:
    """Owns settings request/write orchestration and callback policy."""

    def __init__(
        self,
        node: "Node",
        *,
        message_builder: _NodeSettingsMessageBuilder,
    ) -> None:
        self._node = node
        self._message_builder = message_builder

    def request_config(
        self,
        config_type: int | FieldDescriptor,
        *,
        admin_index: int | None = None,
    ) -> None:
        """Send one settings request with preserved local/remote wait semantics."""
        if self._node is self._node.iface.localNode:
            on_response: Callable[[dict[str, Any]], Any] | None = None
        else:
            on_response = self._node.onResponseRequestSettings
            logger.info(
                "Requesting current config from remote node (this can take a while)."
            )

        message = self._message_builder.build_request_message(config_type)
        request = self._node._send_admin(
            message,
            wantResponse=True,
            onResponse=on_response,
            adminIndex=admin_index,
        )
        if on_response is not None and request is not None:
            self._node.iface.waitForAckNak()

    def _validate_write_configs_loaded(self, config_name: str) -> None:
        """Preserve historical writeConfig loaded-state behavior.

        Historical behavior only required that *some* local/module config had
        been loaded before writes. Keep that compatibility for configure flows
        that intentionally write empty/default sections.
        """
        config_entry = self._message_builder.get_write_config_entry(config_name)
        if config_entry is None:
            self._node._raise_interface_error(  # noqa: SLF001
                f"Error: No valid config with name {config_name}"
            )

        _, source_config = config_entry
        if len(source_config.ListFields()) > 0:
            return
        if (
            len(self._node.localConfig.ListFields()) > 0
            or len(self._node.moduleConfig.ListFields()) > 0
        ):
            logger.debug(
                "Writing %s with empty payload to preserve historical compatibility.",
                config_name,
            )
            return
        self._node._raise_interface_error(  # noqa: SLF001
            "Error: No localConfig has been read. "
            "Request config from the device before writing."
        )

    def write_config(self, config_name: str) -> None:
        """Send one settings write with preserved callback selection."""
        self._message_builder.validate_config_name(config_name)
        self._validate_write_configs_loaded(config_name)
        message = self._message_builder.build_write_message(config_name)
        logger.debug("Wrote: %s", config_name)
        on_response = (
            None if self._node is self._node.iface.localNode else self._node.onAckNak
        )
        request = self._node._send_admin(message, onResponse=on_response)
        if on_response is not None and request is not None:
            self._node.iface.waitForAckNak()


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
            logger.warning(
                "Received malformed settings response (missing decoded): %s", packet
            )
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
            logger.warning(
                "Received malformed settings response (missing admin): %s", packet
            )
            self._node.iface._acknowledgment.receivedNak = True
            return
        target = self._resolve_config_target(admin_message)
        if target is None:
            self._node.iface._acknowledgment.receivedNak = True
            return

        oneof, field_name, config_values = target
        raw_admin = admin_message.get("raw")
        if raw_admin is None:
            logger.warning(
                "Received malformed settings response (missing admin.raw): %s",
                packet,
            )
            self._node.iface._acknowledgment.receivedNak = True
            return
        parent_config = getattr(raw_admin, oneof)
        if not parent_config.HasField(field_name):
            logger.warning(
                "Received settings response without expected field '%s': %s",
                field_name,
                packet,
            )
            self._node.iface._acknowledgment.receivedNak = True
            return
        raw_config = getattr(parent_config, field_name)
        config_values.CopyFrom(raw_config)
        self._node.iface._acknowledgment.receivedAck = True
        logger.info("Received settings block: %s", field_name)


class _NodeAdminCommandRuntime:
    """Owns generic admin-command session/callback/send policy for command family methods."""

    def __init__(self, node: "Node") -> None:
        self._node = node

    def _select_remote_ack_callback(self) -> Callable[[dict[str, Any]], Any] | None:
        """Return callback policy used by remote admin command sends."""
        if self._node is self._node.iface.localNode:
            return None
        return self._node.onAckNak

    def _send_command(
        self,
        message: admin_pb2.AdminMessage,
        *,
        ensure_session_key: bool,
        use_remote_ack_callback: bool,
    ) -> mesh_pb2.MeshPacket | None:
        """Apply shared session/callback/send policy for admin commands."""
        if ensure_session_key:
            self._node.ensureSessionKey()
        on_response = (
            self._select_remote_ack_callback() if use_remote_ack_callback else None
        )
        request = self._node._send_admin(message, onResponse=on_response)
        if on_response is not None and request is not None:
            self._node.iface.waitForAckNak()
        return request

    def send_owner_message(
        self, message: admin_pb2.AdminMessage
    ) -> mesh_pb2.MeshPacket | None:
        """Send set_owner message with historical session and remote-ACK behavior."""
        return self._send_command(
            message,
            ensure_session_key=True,
            use_remote_ack_callback=True,
        )

    def exit_simulator(self) -> mesh_pb2.MeshPacket | None:
        """Send exit-simulator admin command."""
        message = admin_pb2.AdminMessage()
        message.exit_simulator = True
        logger.debug("in exitSimulator()")
        return self._send_command(
            message,
            ensure_session_key=True,
            use_remote_ack_callback=False,
        )

    def reboot(self, secs: int) -> mesh_pb2.MeshPacket | None:
        """Send reboot command with delayed reboot seconds."""
        message = admin_pb2.AdminMessage()
        message.reboot_seconds = secs
        logger.info("Telling node to reboot in %s seconds", secs)
        return self._send_command(
            message,
            ensure_session_key=True,
            use_remote_ack_callback=True,
        )

    def begin_settings_transaction(self) -> mesh_pb2.MeshPacket | None:
        """Send begin-edit-settings command."""
        message = admin_pb2.AdminMessage()
        message.begin_edit_settings = True
        logger.info("Telling node to open a transaction to edit settings")
        return self._send_command(
            message,
            ensure_session_key=True,
            use_remote_ack_callback=True,
        )

    def commit_settings_transaction(self) -> mesh_pb2.MeshPacket | None:
        """Send commit-edit-settings command."""
        message = admin_pb2.AdminMessage()
        message.commit_edit_settings = True
        logger.info("Telling node to commit open transaction for editing settings")
        return self._send_command(
            message,
            ensure_session_key=True,
            use_remote_ack_callback=True,
        )

    def reboot_ota(self, secs: int) -> mesh_pb2.MeshPacket | None:
        """Send reboot-to-OTA command."""
        message = admin_pb2.AdminMessage()
        message.reboot_ota_seconds = secs
        logger.info("Telling node to reboot to OTA in %s seconds", secs)
        return self._send_command(
            message,
            ensure_session_key=True,
            use_remote_ack_callback=True,
        )

    @staticmethod
    def _resolve_ota_hash(
        *,
        ota_file_hash: bytes | None,
        ota_hash: bytes | None,
        legacy_hash: bytes | None,
    ) -> bytes:
        """Resolve one OTA hash input while preserving legacy validation behavior."""
        # Build list of provided hash values first
        provided_hashes: list[bytes] = []
        for value in (ota_file_hash, ota_hash, legacy_hash):
            if value is not None:
                if not isinstance(value, bytes):
                    raise TypeError("ota_file_hash must be bytes")
                provided_hashes.append(value)
        if not provided_hashes:
            raise TypeError("startOTA() missing required argument: 'ota_file_hash'")
        # Deduplicate after validation
        hash_values = set(provided_hashes)
        if len(hash_values) > 1:
            raise ValueError("Conflicting OTA hash arguments provided")
        return hash_values.pop()

    def start_ota(
        self,
        mode: admin_pb2.OTAMode.ValueType | None = None,
        ota_file_hash: bytes | None = None,
        *,
        ota_mode: admin_pb2.OTAMode.ValueType | None = None,
        ota_hash: bytes | None = None,
        extra_kwargs: dict[str, Any],
    ) -> mesh_pb2.MeshPacket | None:
        """Validate OTA args and send ota_request command."""
        if self._node is not self._node.iface.localNode:
            self._node._raise_interface_error(
                "startOTA only possible on local node"
            )  # noqa: SLF001

        # COMPAT_STABLE_SHIM: support legacy keyword aliases used by older callers:
        # `ota_mode` -> `mode`, and `ota_hash`/`hash` -> `ota_file_hash`.
        legacy_hash = extra_kwargs.pop("hash", None)
        if extra_kwargs:
            unexpected = ", ".join(sorted(extra_kwargs))
            raise TypeError(
                f"startOTA() got unexpected keyword argument(s): {unexpected}"
            )

        if mode is not None and ota_mode is not None and mode != ota_mode:
            raise ValueError("Conflicting OTA mode arguments provided")
        resolved_mode = mode if mode is not None else ota_mode
        if resolved_mode is None:
            raise TypeError("startOTA() missing required argument: 'mode'")

        resolved_hash = self._resolve_ota_hash(
            ota_file_hash=ota_file_hash,
            ota_hash=ota_hash,
            legacy_hash=legacy_hash,
        )

        message = admin_pb2.AdminMessage()
        message.ota_request.reboot_ota_mode = resolved_mode
        message.ota_request.ota_hash = resolved_hash
        return self._send_command(
            message,
            ensure_session_key=True,
            use_remote_ack_callback=False,
        )

    def enter_dfu_mode(self) -> mesh_pb2.MeshPacket | None:
        """Send enter-DFU-mode command."""
        message = admin_pb2.AdminMessage()
        message.enter_dfu_mode_request = True
        logger.info("Telling node to enable DFU mode")
        return self._send_command(
            message,
            ensure_session_key=True,
            use_remote_ack_callback=True,
        )

    def shutdown(self, secs: int) -> mesh_pb2.MeshPacket | None:
        """Send shutdown command with delayed shutdown seconds."""
        message = admin_pb2.AdminMessage()
        message.shutdown_seconds = secs
        logger.info("Telling node to shutdown in %s seconds", secs)
        return self._send_command(
            message,
            ensure_session_key=True,
            use_remote_ack_callback=True,
        )

    def factory_reset(self, *, full: bool) -> mesh_pb2.MeshPacket | None:
        """Send factory-reset command, preserving full/config split behavior."""
        message = admin_pb2.AdminMessage()
        if full:
            message.factory_reset_device = (
                self._node._get_factory_reset_request_value()
            )  # noqa: SLF001
            logger.info("Telling node to factory reset (full device reset)")
        else:
            message.factory_reset_config = (
                self._node._get_factory_reset_request_value()
            )  # noqa: SLF001
            logger.info("Telling node to factory reset (config reset)")
        return self._send_command(
            message,
            ensure_session_key=True,
            use_remote_ack_callback=True,
        )

    def _send_node_id_command(
        self,
        *,
        node_id: int | str,
        set_field: Callable[[admin_pb2.AdminMessage, int], None],
    ) -> mesh_pb2.MeshPacket | None:
        """Send one node-id command after toNodeNum conversion."""
        node_num = toNodeNum(node_id)
        message = admin_pb2.AdminMessage()
        set_field(message, node_num)
        return self._send_command(
            message,
            ensure_session_key=True,
            use_remote_ack_callback=True,
        )

    def remove_node(self, node_id: int | str) -> mesh_pb2.MeshPacket | None:
        """Send remove-by-nodenum command."""
        return self._send_node_id_command(
            node_id=node_id,
            set_field=lambda message, node_num: setattr(
                message, "remove_by_nodenum", node_num
            ),
        )

    def set_favorite(self, node_id: int | str) -> mesh_pb2.MeshPacket | None:
        """Send set-favorite command."""
        return self._send_node_id_command(
            node_id=node_id,
            set_field=lambda message, node_num: setattr(
                message, "set_favorite_node", node_num
            ),
        )

    def remove_favorite(self, node_id: int | str) -> mesh_pb2.MeshPacket | None:
        """Send remove-favorite command."""
        return self._send_node_id_command(
            node_id=node_id,
            set_field=lambda message, node_num: setattr(
                message, "remove_favorite_node", node_num
            ),
        )

    def set_ignored(self, node_id: int | str) -> mesh_pb2.MeshPacket | None:
        """Send set-ignored command."""
        return self._send_node_id_command(
            node_id=node_id,
            set_field=lambda message, node_num: setattr(
                message, "set_ignored_node", node_num
            ),
        )

    def remove_ignored(self, node_id: int | str) -> mesh_pb2.MeshPacket | None:
        """Send remove-ignored command."""
        return self._send_node_id_command(
            node_id=node_id,
            set_field=lambda message, node_num: setattr(
                message, "remove_ignored_node", node_num
            ),
        )

    def reset_node_db(self) -> mesh_pb2.MeshPacket | None:
        """Send NodeDB reset command."""
        message = admin_pb2.AdminMessage()
        message.nodedb_reset = True
        logger.info("Telling node to reset the NodeDB")
        return self._send_command(
            message,
            ensure_session_key=True,
            use_remote_ack_callback=True,
        )


class _NodeOwnerProfileRuntime:
    """Owns setOwner validation/truncation/message-build and send orchestration."""

    def __init__(
        self,
        node: "Node",
        *,
        admin_command_runtime: _NodeAdminCommandRuntime,
    ) -> None:
        self._node = node
        self._admin_command_runtime = admin_command_runtime

    def set_owner(
        self,
        *,
        long_name: str | None = None,
        short_name: str | None = None,
        is_licensed: bool = False,
        is_unmessagable: bool | None = None,
    ) -> mesh_pb2.MeshPacket | None:
        """Apply setOwner validation/truncation and send policy."""
        logger.debug("in setOwner nodeNum:%s", self._node.nodeNum)
        message = admin_pb2.AdminMessage()

        if long_name is not None:
            long_name = long_name.strip()
            # Validate that long_name is not empty or whitespace-only
            if not long_name:
                self._node._raise_interface_error(EMPTY_LONG_NAME_MSG)  # noqa: SLF001
            if len(long_name) > MAX_LONG_NAME_LEN:
                long_name = long_name[:MAX_LONG_NAME_LEN]
                logger.warning(
                    "Long name is longer than %s characters, truncating to '%s'",
                    MAX_LONG_NAME_LEN,
                    long_name,
                )
            message.set_owner.long_name = long_name

        if short_name is not None:
            short_name = short_name.strip()
            # Validate that short_name is not empty or whitespace-only
            if not short_name:
                self._node._raise_interface_error(EMPTY_SHORT_NAME_MSG)  # noqa: SLF001
            if len(short_name) > MAX_SHORT_NAME_LEN:
                short_name = short_name[:MAX_SHORT_NAME_LEN]
                logger.warning(
                    "Short name is longer than %s characters, truncating to '%s'",
                    MAX_SHORT_NAME_LEN,
                    short_name,
                )
            message.set_owner.short_name = short_name

        message.set_owner.is_licensed = is_licensed
        if is_unmessagable is not None:
            message.set_owner.is_unmessagable = is_unmessagable

        # Note: These debug lines are used in unit tests
        logger.debug("p.set_owner.long_name:%s:", message.set_owner.long_name)
        logger.debug("p.set_owner.short_name:%s:", message.set_owner.short_name)
        logger.debug("p.set_owner.is_licensed:%s:", message.set_owner.is_licensed)
        logger.debug(
            "p.set_owner.is_unmessagable:%s:",
            message.set_owner.is_unmessagable,
        )
        return self._admin_command_runtime.send_owner_message(message)
