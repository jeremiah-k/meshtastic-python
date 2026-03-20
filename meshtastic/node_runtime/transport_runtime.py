"""Admin transport, channel write/delete, ACK/NAK, and position/time command runtimes."""

import logging
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Callable

from meshtastic.node_runtime.shared import (
    MAX_CHANNELS,
    is_named_admin_channel_name as _is_named_admin_channel_name,
)
from meshtastic.protobuf import admin_pb2, channel_pb2, mesh_pb2, portnums_pb2

if TYPE_CHECKING:
    from meshtastic.node import Node

logger = logging.getLogger(__name__)


class _NodeAdminSessionRuntime:
    """Owns admin-session readiness checks and session-key request behavior."""

    def __init__(self, node: "Node") -> None:
        self._node = node

    def ensure_session_key(self, *, admin_index: int | None = None) -> None:
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


class _NodeAdminTransportRuntime:
    """Owns admin transport mechanics for ADMIN_APP sends."""

    def __init__(self, node: "Node") -> None:
        self._node = node

    def _resolve_admin_index(self, admin_index: int | None) -> int:
        """Resolve None to auto-detected admin channel index; preserve explicit zero."""
        if admin_index is None:
            return self._node.iface.localNode._get_admin_channel_index()
        return admin_index

    def send_admin(
        self,
        message: admin_pb2.AdminMessage,
        *,
        want_response: bool = False,
        on_response: Callable[[dict[str, Any]], Any] | None = None,
        admin_index: int | None = None,
    ) -> mesh_pb2.MeshPacket | None:
        """Send an AdminMessage via iface.sendData with preserved transport semantics."""
        if self._node.noProto:
            logger.warning(
                "Not sending packet because protocol use is disabled by noProto"
            )
            return None

        resolved_admin_index = self._resolve_admin_index(admin_index)
        logger.debug("adminIndex:%s", resolved_admin_index)

        node_info = self._node.iface._get_or_create_by_num(self._node.nodeNum)
        passkey = node_info.get("adminSessionPassKey")
        if isinstance(passkey, bytes):
            message.session_passkey = passkey

        return self._node.iface.sendData(
            message,
            self._node.nodeNum,
            portNum=portnums_pb2.PortNum.ADMIN_APP,
            wantAck=True,
            wantResponse=want_response,
            onResponse=on_response,
            channelIndex=resolved_admin_index,
            pkiEncrypted=True,
        )


class _NodeChannelWriteRuntime:
    """Owns channel-snapshot writes and writeChannel orchestration."""

    def __init__(
        self,
        node: "Node",
        *,
        admin_session_runtime: _NodeAdminSessionRuntime,
        admin_transport_runtime: _NodeAdminTransportRuntime,
    ) -> None:
        self._node = node
        self._admin_session_runtime = admin_session_runtime
        self._admin_transport_runtime = admin_transport_runtime

    def write_channel_snapshot(
        self,
        channel_to_write: channel_pb2.Channel,
        *,
        admin_index: int | None = None,
    ) -> None:
        """Send a pre-built channel snapshot to the device."""
        # Keep compatibility for callers/tests that patch Node facade methods.
        # Runtime ownership remains here, but transport/session side effects still
        # flow through the historical Node entry points.
        self._node.ensureSessionKey(adminIndex=admin_index)
        request_message = admin_pb2.AdminMessage()
        request_message.set_channel.CopyFrom(channel_to_write)
        self._node._send_admin(
            request_message,
            adminIndex=admin_index,
        )
        logger.debug("Wrote channel %s", channel_to_write.index)

    def write_channel(self, channel_index: int, *, admin_index: int | None = None) -> None:
        """Validate and write one channel by index using snapshot semantics."""
        with self._node._channels_lock:  # noqa: SLF001
            channels = self._node.channels
            if channels is None:
                self._node._raise_interface_error("Error: No channels have been read")  # noqa: SLF001
            if channel_index < 0 or channel_index >= len(channels):
                self._node._raise_interface_error(  # noqa: SLF001
                    f"Channel index {channel_index} out of range (0-{len(channels) - 1})"
                )
            channel_snapshot = channel_pb2.Channel()
            channel_snapshot.CopyFrom(channels[channel_index])
        self.write_channel_snapshot(
            channel_snapshot,
            admin_index=admin_index,
        )


@dataclass(frozen=True)
class _DeleteChannelRewritePlan:
    """Delete-channel rewrite execution plan."""

    pre_delete_admin_index: int
    post_delete_admin_index: int
    switch_after_admin_slot_rewrite: bool
    channels_to_rewrite: list[channel_pb2.Channel]


class _NodeDeleteChannelRuntime:
    """Owns delete-channel validation, planning, and ordered rewrite execution."""

    def __init__(self, node: "Node", *, channel_write_runtime: _NodeChannelWriteRuntime):
        self._node = node
        self._channel_write_runtime = channel_write_runtime

    @staticmethod
    def _named_admin_index_from_channels(
        channel_list: list[channel_pb2.Channel],
    ) -> int:
        for channel in channel_list:
            if (
                channel.role != channel_pb2.Channel.Role.DISABLED
                and channel.settings
                and _is_named_admin_channel_name(channel.settings.name)
            ):
                return channel.index
        return 0

    def _build_rewrite_plan(self, channel_index: int) -> _DeleteChannelRewritePlan:
        """Build lock-scoped delete/rewrite plan with pre/post admin indexes."""
        with self._node._channels_lock:  # noqa: SLF001
            channels = self._node.channels
            if channels is None:
                self._node._raise_interface_error("Error: No channels have been read")  # noqa: SLF001
            if channel_index < 0 or channel_index >= len(channels):
                self._node._raise_interface_error(  # noqa: SLF001
                    f"Channel index {channel_index} out of range (0-{len(channels) - 1})"
                )

            channel_to_delete = channels[channel_index]
            if channel_to_delete.role not in (
                channel_pb2.Channel.Role.SECONDARY,
                channel_pb2.Channel.Role.DISABLED,
            ):
                self._node._raise_interface_error(  # noqa: SLF001
                    "Only SECONDARY or DISABLED channels can be deleted"
                )

            is_local_node = self._node.iface.localNode == self._node
            if is_local_node:
                pre_delete_admin_index = self._named_admin_index_from_channels(channels)
            else:
                pre_delete_admin_index = self._node.iface.localNode.getAdminChannelIndex()

            # If we move the "admin" channel, the index used for admin writes
            # will need to be recomputed as writes progress.
            channels.pop(channel_index)
            self._node._fixup_channels_locked()  # noqa: SLF001

            channels_to_rewrite: list[channel_pb2.Channel] = []
            for rewrite_index in range(channel_index, MAX_CHANNELS):
                channel_snapshot = channel_pb2.Channel()
                channel_snapshot.CopyFrom(channels[rewrite_index])
                channels_to_rewrite.append(channel_snapshot)

            if is_local_node:
                post_delete_admin_index = self._named_admin_index_from_channels(channels)
            else:
                post_delete_admin_index = self._node.iface.localNode.getAdminChannelIndex()

        return _DeleteChannelRewritePlan(
            pre_delete_admin_index=pre_delete_admin_index,
            post_delete_admin_index=post_delete_admin_index,
            switch_after_admin_slot_rewrite=(pre_delete_admin_index >= channel_index),
            channels_to_rewrite=channels_to_rewrite,
        )

    def _execute_rewrite_plan(self, plan: _DeleteChannelRewritePlan) -> None:
        """Execute channel rewrites while preserving historical admin-index switch timing."""
        admin_index_for_write = plan.pre_delete_admin_index
        for channel_snapshot in plan.channels_to_rewrite:
            self._channel_write_runtime.write_channel_snapshot(
                channel_snapshot,
                admin_index=admin_index_for_write,
            )
            if (
                plan.switch_after_admin_slot_rewrite
                and channel_snapshot.index == plan.pre_delete_admin_index
            ):
                admin_index_for_write = plan.post_delete_admin_index

    def delete_channel(self, channel_index: int) -> None:
        """Delete one channel and execute ordered rewrite plan."""
        rewrite_plan = self._build_rewrite_plan(channel_index)
        self._execute_rewrite_plan(rewrite_plan)


class _NodeAckNakRuntime:
    """Owns ACK/NAK payload classification and acknowledgment flag mutation."""

    def __init__(self, node: "Node") -> None:
        self._node = node

    def handle_ack_nak(self, packet: dict[str, Any]) -> None:
        """Classify ACK/NAK payload and update interface acknowledgment state."""
        decoded = packet.get("decoded", {})
        routing = decoded.get("routing")
        if not isinstance(routing, dict):
            logger.warning(
                "Received ACK/NAK response without routing details: %s", packet
            )
            return

        error_reason = routing.get("errorReason", "NONE")
        if error_reason != "NONE":
            logger.warning("Received a NAK, error reason: %s", error_reason)
            self._node.iface._acknowledgment.receivedNak = True
            return

        from_value = packet.get("from")
        if from_value is None:
            logger.warning("Received ACK/NAK response without sender: %s", packet)
            return
        try:
            from_num = int(from_value)
        except (TypeError, ValueError):
            logger.warning("Received ACK/NAK response with invalid sender: %s", packet)
            return

        if from_num == self._node.iface.localNode.nodeNum:
            logger.info(
                "Received an implicit ACK. Packet will likely arrive, but cannot be guaranteed."
            )
            self._node.iface._acknowledgment.receivedImplAck = True
            return

        logger.info("Received an ACK.")
        self._node.iface._acknowledgment.receivedAck = True


class _NodePositionTimeCommandRuntime:
    """Owns setFixedPosition/removeFixedPosition/setTime command orchestration."""

    def __init__(self, node: "Node") -> None:
        self._node = node

    def _select_remote_ack_callback(self) -> Callable[[dict[str, Any]], Any] | None:
        """Return callback policy used by remote admin command sends."""
        if self._node == self._node.iface.localNode:
            return None
        return self._node.onAckNak

    def set_fixed_position(
        self,
        *,
        lat: int | float,
        lon: int | float,
        alt: int,
    ) -> mesh_pb2.MeshPacket | None:
        """Send set_fixed_position admin command with preserved conversion semantics."""
        self._node.ensureSessionKey()

        position_message = mesh_pb2.Position()
        if isinstance(lat, float) and lat != 0.0:
            position_message.latitude_i = int(lat * 1e7)
        elif isinstance(lat, int) and lat != 0:
            position_message.latitude_i = lat

        if isinstance(lon, float) and lon != 0.0:
            position_message.longitude_i = int(lon * 1e7)
        elif isinstance(lon, int) and lon != 0:
            position_message.longitude_i = lon

        if alt != 0:
            position_message.altitude = alt

        admin_message = admin_pb2.AdminMessage()
        admin_message.set_fixed_position.CopyFrom(position_message)
        return self._node._send_admin(
            admin_message,
            onResponse=self._select_remote_ack_callback(),
        )

    def remove_fixed_position(self) -> mesh_pb2.MeshPacket | None:
        """Send remove_fixed_position admin command."""
        self._node.ensureSessionKey()
        admin_message = admin_pb2.AdminMessage()
        admin_message.remove_fixed_position = True
        logger.info("Telling node to remove fixed position")
        return self._node._send_admin(
            admin_message,
            onResponse=self._select_remote_ack_callback(),
        )

    def set_time(self, *, time_sec: int = 0) -> mesh_pb2.MeshPacket | None:
        """Send set_time_only admin command with current-time fallback."""
        self._node.ensureSessionKey()
        if time_sec == 0:
            time_sec = int(time.time())
        admin_message = admin_pb2.AdminMessage()
        admin_message.set_time_only = time_sec
        logger.info("Setting node time to %s", time_sec)
        return self._node._send_admin(
            admin_message,
            onResponse=self._select_remote_ack_callback(),
        )
