"""Metadata and channel response runtimes for Node admin requests."""

import logging
import time
from typing import TYPE_CHECKING, Any

from meshtastic.node_runtime.shared import MAX_CHANNELS
from meshtastic.protobuf import channel_pb2, config_pb2, mesh_pb2, portnums_pb2
from meshtastic.util import stripnl

if TYPE_CHECKING:
    from meshtastic.node import Node

logger = logging.getLogger(__name__)


class _NodeMetadataResponseRuntime:
    """Owns metadata response routing/error handling, state mutation, and signaling."""

    def __init__(self, node: "Node") -> None:
        self._node = node

    def _handle_routing_portnum(self, decoded: dict[str, Any]) -> bool:
        """Handle ROUTING_APP metadata responses and indicate whether processing is complete."""
        if decoded["portnum"] != portnums_pb2.PortNum.Name(
            portnums_pb2.PortNum.ROUTING_APP
        ):
            return False
        if decoded["routing"]["errorReason"] != "NONE":
            logger.warning(
                "Metadata request failed, error reason: %s",
                decoded["routing"]["errorReason"],
            )
            self._node.iface._acknowledgment.receivedNak = True
            self._node._timeout.expireTime = time.time()  # Do not wait any longer
            self._node._signal_metadata_stdout_event()
            return True  # Don't try to parse this routing message
        logger.debug(
            "Metadata request routed successfully; waiting for ADMIN_APP payload."
        )
        return True

    def _handle_generic_routing_error(self, decoded: dict[str, Any]) -> bool:
        """Handle non-routing-port metadata errors and indicate completion."""
        if "routing" in decoded and decoded["routing"]["errorReason"] != "NONE":
            logger.error("Error on response: %s", decoded["routing"]["errorReason"])
            self._node.iface._acknowledgment.receivedNak = True
            self._node._signal_metadata_stdout_event()
            return True
        return False

    def _emit_metadata_lines(self, metadata: mesh_pb2.DeviceMetadata) -> None:
        """Emit metadata lines with historical formatting and enum fallback behavior."""
        self._node._emit_metadata_line(
            f"\nfirmware_version: {metadata.firmware_version}"
        )
        self._node._emit_metadata_line(
            f"device_state_version: {metadata.device_state_version}"
        )
        if metadata.role in config_pb2.Config.DeviceConfig.Role.values():
            self._node._emit_metadata_line(
                f"role: {config_pb2.Config.DeviceConfig.Role.Name(metadata.role)}"
            )
        else:
            self._node._emit_metadata_line(f"role: {metadata.role}")
        self._node._emit_metadata_line(
            f"position_flags: {self._node.position_flags_list(metadata.position_flags)}"
        )
        if metadata.hw_model in mesh_pb2.HardwareModel.values():
            self._node._emit_metadata_line(
                f"hw_model: {mesh_pb2.HardwareModel.Name(metadata.hw_model)}"
            )
        else:
            self._node._emit_metadata_line(f"hw_model: {metadata.hw_model}")
        self._node._emit_metadata_line(f"hasPKC: {metadata.hasPKC}")
        if metadata.excluded_modules > 0:
            self._node._emit_metadata_line(
                f"excluded_modules: {self._node.excluded_modules_list(metadata.excluded_modules)}"
            )

    def handle_metadata_response(self, packet: dict[str, Any]) -> None:
        """Process metadata response packet and preserve historical ACK/timeout semantics."""
        logger.debug("onRequestGetMetadata() p:%s", packet)
        decoded = packet["decoded"]
        if self._handle_routing_portnum(decoded):
            return
        if self._handle_generic_routing_error(decoded):
            return

        self._node.iface._acknowledgment.receivedAck = True
        metadata_response = decoded["admin"]["raw"].get_device_metadata_response
        metadata_snapshot = mesh_pb2.DeviceMetadata()
        metadata_snapshot.CopyFrom(metadata_response)
        self._node._set_metadata_snapshot(metadata_snapshot)
        self._node._timeout.reset()  # We made forward progress
        logger.debug("Received metadata %s", stripnl(metadata_response))
        self._emit_metadata_lines(metadata_response)
        self._node._signal_metadata_stdout_event()


class _NodeChannelResponseRuntime:
    """Owns channel-response routing/error handling, sequencing, and final installation."""

    def __init__(self, node: "Node") -> None:
        self._node = node

    def _handle_routing_response(self, decoded: dict[str, Any]) -> bool:
        """Handle ROUTING_APP channel responses and indicate whether processing is complete."""
        if decoded["portnum"] != portnums_pb2.PortNum.Name(
            portnums_pb2.PortNum.ROUTING_APP
        ):
            return False
        if decoded["routing"]["errorReason"] != "NONE":
            logger.warning(
                "Channel request failed, error reason: %s",
                decoded["routing"]["errorReason"],
            )
            self._node._timeout.expireTime = time.time()  # Do not wait any longer
            return True  # Don't try to parse this routing message

        last_tried = 0
        with self._node._channels_lock:  # noqa: SLF001
            if self._node.partialChannels:
                last_tried = self._node.partialChannels[-1].index
        logger.debug("Retrying previous channel request.")
        self._node._request_channel(last_tried)  # noqa: SLF001
        return True

    def handle_channel_response(self, packet: dict[str, Any]) -> None:
        """Process channel response packet and maintain partial/final channel sequencing."""
        logger.debug("onResponseRequestChannel() p:%s", packet)
        decoded = packet["decoded"]
        if self._handle_routing_response(decoded):
            return

        response_channel = decoded["admin"]["raw"].get_channel_response
        channel_response = channel_pb2.Channel()
        channel_response.CopyFrom(response_channel)
        with self._node._channels_lock:  # noqa: SLF001
            self._node.partialChannels.append(channel_response)
        self._node._timeout.reset()  # We made forward progress
        logger.debug("Received channel %s", stripnl(channel_response))
        index = channel_response.index

        if index >= MAX_CHANNELS - 1:
            logger.debug("Finished downloading channels")
            with self._node._channels_lock:  # noqa: SLF001
                self._node.channels = list(self._node.partialChannels)
                self._node._fixup_channels_locked()  # noqa: SLF001
            return
        self._node._request_channel(index + 1)  # noqa: SLF001
