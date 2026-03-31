"""Channel-snapshot writes, delete-channel planning, and rewrite execution."""

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from meshtastic.node_runtime.shared import (
    MAX_CHANNELS,
    isNamedAdminChannelName as _isNamedAdminChannelName,
)
from meshtastic.node_runtime.transport_runtime.admin import _NodeAdminTransportRuntime
from meshtastic.node_runtime.transport_runtime.session import _NodeAdminSessionRuntime
from meshtastic.protobuf import admin_pb2, channel_pb2

if TYPE_CHECKING:
    from meshtastic.node import Node

logger = logging.getLogger(__name__)


def _channels_fingerprint(
    channels: list[channel_pb2.Channel],
) -> tuple[bytes, ...]:
    """Return an immutable, deterministic fingerprint of channel states for comparison."""
    return tuple(c.SerializeToString() for c in channels)


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

    def _write_channel_snapshot(
        self,
        channel_to_write: channel_pb2.Channel,
        *,
        admin_index: int | None = None,
    ) -> None:
        """Send a pre-built channel snapshot to the device and wait for ACK/NAK.

        Raises an interface error if the device responds with NAK.
        """
        self._node.ensureSessionKey(adminIndex=admin_index)
        request_message = admin_pb2.AdminMessage()
        request_message.set_channel.CopyFrom(channel_to_write)
        # Use remote ACK callback for remote nodes to detect NAK
        on_response = (
            self._node.onAckNak
            if self._node is not self._node.iface.localNode
            else None
        )
        request = self._node._send_admin(
            request_message,
            adminIndex=admin_index,
            onResponse=on_response,
        )
        if request is None:
            logger.error(
                "Channel write was not started for index %s.",
                channel_to_write.index,
            )
            self._node._raise_interface_error(
                f"Channel write for index {channel_to_write.index} was not started"
            )
        # Wait for ACK/NAK response for remote nodes
        if on_response is not None and request is not None:
            self._node.iface.waitForAckNak()
            # Check if we received a NAK and abort if so
            ack_state = getattr(self._node.iface, "_acknowledgment", None)
            if ack_state is not None and getattr(ack_state, "receivedNak", False):
                self._node._raise_interface_error(
                    f"Channel write for index {channel_to_write.index} was rejected by device (NAK)"
                )
        logger.debug("Wrote channel %s", channel_to_write.index)

    def _write_channel(
        self, channel_index: int, *, admin_index: int | None = None
    ) -> None:
        """Validate and write one channel by index using snapshot semantics."""
        with self._node._channels_lock:
            channels = self._node.channels
            if channels is None:
                self._node._raise_interface_error("Error: No channels have been read")
            if len(channels) == 0:
                self._node._raise_interface_error("Error: Channels list is empty")
            if channel_index < 0 or channel_index >= len(channels):
                self._node._raise_interface_error(
                    f"Channel index {channel_index} out of range (0-{len(channels) - 1})"
                )
            channel_snapshot = channel_pb2.Channel()
            channel_snapshot.CopyFrom(channels[channel_index])
        self._write_channel_snapshot(
            channel_snapshot,
            admin_index=admin_index,
        )


@dataclass(frozen=True)
class _DeleteChannelRewritePlan:
    """Delete-channel rewrite execution plan."""

    original_channels_ref: list[channel_pb2.Channel]
    original_channels_fingerprint: tuple[bytes, ...]
    pre_delete_admin_index: int
    post_delete_admin_index: int
    switch_after_admin_slot_rewrite: bool
    channels_to_rewrite: list[channel_pb2.Channel]
    staged_channels: list[channel_pb2.Channel]


class _NodeDeleteChannelRuntime:
    """Owns delete-channel validation, planning, and ordered rewrite execution."""

    def __init__(
        self,
        node: "Node",
        *,
        channel_write_runtime: _NodeChannelWriteRuntime,
    ) -> None:
        self._node = node
        self._channel_write_runtime = channel_write_runtime

    @staticmethod
    def _named_admin_index_from_channels(
        channel_list: list[channel_pb2.Channel],
    ) -> int:
        """Find the index of the named admin channel in a channel list.

        Parameters
        ----------
        channel_list : list[channel_pb2.Channel]
            List of channels to search.

        Returns
        -------
        int
            The index of the first enabled channel with a named admin channel name,
            or 0 if none is found.
        """
        for channel in channel_list:
            if (
                channel.role != channel_pb2.Channel.Role.DISABLED
                and channel.settings
                and _isNamedAdminChannelName(channel.settings.name)
            ):
                return channel.index
        return 0

    @staticmethod
    def _normalize_staged_channels(channels: list[channel_pb2.Channel]) -> None:
        """Normalize staged channel list using Node lock-scoped fixup semantics."""
        if len(channels) > MAX_CHANNELS:
            logger.warning(
                "Truncating channel list from %d to %d entries",
                len(channels),
                MAX_CHANNELS,
            )
            del channels[MAX_CHANNELS:]
        for index, channel in enumerate(channels):
            channel.index = index
        index = len(channels)
        while index < MAX_CHANNELS:
            channel = channel_pb2.Channel()
            channel.role = channel_pb2.Channel.Role.DISABLED
            channel.index = index
            channels.append(channel)
            index += 1

    def _build_rewrite_plan(self, channel_index: int) -> _DeleteChannelRewritePlan:
        """Build delete/rewrite plan with pre/post admin indexes.

        Caller must hold self._node._channels_lock.
        """
        channels = self._node.channels
        if channels is None:
            self._node._raise_interface_error("Error: No channels have been read")
        if len(channels) == 0:
            self._node._raise_interface_error("Error: Channels list is empty")
        if channel_index < 0 or channel_index >= len(channels):
            self._node._raise_interface_error(
                f"Channel index {channel_index} out of range (0-{len(channels) - 1})"
            )

        channel_to_delete = channels[channel_index]
        if channel_to_delete.role not in (
            channel_pb2.Channel.Role.SECONDARY,
            channel_pb2.Channel.Role.DISABLED,
        ):
            self._node._raise_interface_error(
                "Only SECONDARY or DISABLED channels can be deleted"
            )

        is_local_node = self._node.iface.localNode is self._node
        if is_local_node:
            pre_delete_admin_index = self._named_admin_index_from_channels(channels)
        else:
            local_node = self._node.iface.localNode
            if local_node is None:
                self._node._raise_interface_error(
                    "Cannot delete remote channel: local node not available"
                )
            pre_delete_admin_index = local_node._get_admin_channel_index()

        staged_channels: list[channel_pb2.Channel] = []
        for existing_channel in channels:
            staged_channel = channel_pb2.Channel()
            staged_channel.CopyFrom(existing_channel)
            staged_channels.append(staged_channel)
        staged_channels.pop(channel_index)
        self._normalize_staged_channels(staged_channels)

        channels_to_rewrite: list[channel_pb2.Channel] = []
        for rewrite_index in range(channel_index, MAX_CHANNELS):
            channel_snapshot = channel_pb2.Channel()
            channel_snapshot.CopyFrom(staged_channels[rewrite_index])
            channels_to_rewrite.append(channel_snapshot)

        if is_local_node:
            post_delete_admin_index = self._named_admin_index_from_channels(
                staged_channels
            )
        else:
            local_node = self._node.iface.localNode
            if local_node is None:
                self._node._raise_interface_error(
                    "Cannot delete remote channel: local node not available"
                )
            post_delete_admin_index = local_node._get_admin_channel_index()

        return _DeleteChannelRewritePlan(
            original_channels_ref=channels,
            original_channels_fingerprint=_channels_fingerprint(channels),
            pre_delete_admin_index=pre_delete_admin_index,
            post_delete_admin_index=post_delete_admin_index,
            switch_after_admin_slot_rewrite=(pre_delete_admin_index >= channel_index),
            channels_to_rewrite=channels_to_rewrite,
            staged_channels=staged_channels,
        )

    def _execute_rewrite_plan(self, plan: _DeleteChannelRewritePlan) -> None:
        """Execute channel rewrites while preserving historical admin-index switch timing."""
        admin_index_for_write = plan.pre_delete_admin_index
        for channel_snapshot in plan.channels_to_rewrite:
            self._channel_write_runtime._write_channel_snapshot(
                channel_snapshot,
                admin_index=admin_index_for_write,
            )
            if (
                plan.switch_after_admin_slot_rewrite
                and channel_snapshot.index == plan.pre_delete_admin_index
            ):
                admin_index_for_write = plan.post_delete_admin_index

    def _delete_channel(self, channel_index: int) -> None:
        """Delete one channel and execute ordered rewrite plan.

        The entire delete operation is serialized under the channels lock to prevent
        concurrent in-place mutations from racing with the delete/rewrite sequence.
        """
        with self._node._channels_lock:
            rewrite_plan = self._build_rewrite_plan(channel_index)
            try:
                self._execute_rewrite_plan(rewrite_plan)
            except Exception:
                self._node.channels = None
                self._node.partialChannels = []
                raise
            current_channels = self._node.channels
            if current_channels is None:
                logger.warning(
                    "Channel cache became unavailable during delete rewrite; skipping staged cache commit."
                )
                self._node.partialChannels = []
                return
            if current_channels is not rewrite_plan.original_channels_ref:
                logger.warning(
                    "Channel cache changed during delete rewrite; invalidating local channel cache."
                )
                self._node.channels = None
                self._node.partialChannels = []
                return
            if (
                _channels_fingerprint(current_channels)
                != rewrite_plan.original_channels_fingerprint
            ):
                logger.warning(
                    "Channel fingerprint mismatch after delete rewrite; invalidating local channel cache."
                )
                self._node.channels = None
                self._node.partialChannels = []
                return
            current_channels.clear()
            for staged_channel in rewrite_plan.staged_channels:
                channel_copy = channel_pb2.Channel()
                channel_copy.CopyFrom(staged_channel)
                current_channels.append(channel_copy)
            self._node._fixup_channels_locked()
