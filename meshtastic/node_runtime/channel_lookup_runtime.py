"""Channel lookup and admin-index resolution runtime owner."""

from typing import TYPE_CHECKING

from meshtastic.node_runtime.shared import (
    is_named_admin_channel_name as _is_named_admin_channel_name,
)
from meshtastic.protobuf import channel_pb2

if TYPE_CHECKING:
    from meshtastic.node import Node


class _NodeChannelLookupRuntime:
    """Owns lock-safe channel lookup and admin-channel index resolution."""

    def __init__(self, node: "Node") -> None:
        self._node = node

    def get_channel_by_index(self, channel_index: int) -> channel_pb2.Channel | None:
        """Return live channel by index when available, preserving compatibility."""
        with self._node._channels_lock:  # noqa: SLF001
            channels = self._node.channels
            if channels and 0 <= channel_index < len(channels):
                return channels[channel_index]
            return None

    def get_channel_copy_by_index(
        self, channel_index: int
    ) -> channel_pb2.Channel | None:
        """Return defensive channel copy by index for read-only callers."""
        with self._node._channels_lock:  # noqa: SLF001
            channels = self._node.channels
            if channels and 0 <= channel_index < len(channels):
                copied = channel_pb2.Channel()
                copied.CopyFrom(channels[channel_index])
                return copied
            return None

    def get_channel_by_name(self, name: str) -> channel_pb2.Channel | None:
        """Return live channel whose settings.name exactly matches ``name``."""
        with self._node._channels_lock:  # noqa: SLF001
            for channel in self._node.channels or []:
                if channel.settings and channel.settings.name == name:
                    return channel
            return None

    def get_channel_copy_by_name(self, name: str) -> channel_pb2.Channel | None:
        """Return defensive channel copy found by exact settings.name match."""
        with self._node._channels_lock:  # noqa: SLF001
            for channel in self._node.channels or []:
                if channel.settings and channel.settings.name == name:
                    copied = channel_pb2.Channel()
                    copied.CopyFrom(channel)
                    return copied
            return None

    def get_disabled_channel(self) -> channel_pb2.Channel | None:
        """Return first live disabled channel, if present."""
        with self._node._channels_lock:  # noqa: SLF001
            channels = self._node.channels
            if channels is None:
                return None
            for channel in channels:
                if channel.role == channel_pb2.Channel.Role.DISABLED:
                    return channel
            return None

    def get_disabled_channel_copy(self) -> channel_pb2.Channel | None:
        """Return defensive copy of first disabled channel, if present."""
        with self._node._channels_lock:  # noqa: SLF001
            channels = self._node.channels
            if channels is None:
                return None
            for channel in channels:
                if channel.role == channel_pb2.Channel.Role.DISABLED:
                    copied = channel_pb2.Channel()
                    copied.CopyFrom(channel)
                    return copied
            return None

    def get_named_admin_channel_index(self) -> int | None:
        """Return index of explicitly named ``admin`` channel, if present."""
        with self._node._channels_lock:  # noqa: SLF001
            for channel in self._node.channels or []:
                if (
                    channel.role != channel_pb2.Channel.Role.DISABLED
                    and channel.settings
                    and _is_named_admin_channel_name(channel.settings.name)
                ):
                    return channel.index
            return None

    def get_admin_channel_index(self) -> int:
        """Return named admin index when present; otherwise channel index zero."""
        named_admin_index = self.get_named_admin_channel_index()
        return 0 if named_admin_index is None else named_admin_index
