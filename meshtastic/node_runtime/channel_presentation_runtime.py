"""User-facing channel/config presentation runtime owner."""

import logging
from typing import TYPE_CHECKING

from meshtastic.protobuf import channel_pb2
from meshtastic.util import messageToJson, pskToString

from .channel_export_runtime import _NodeChannelExportRuntime

if TYPE_CHECKING:
    from meshtastic.node import Node

logger = logging.getLogger(__name__)


class _NodeChannelPresentationRuntime:
    """Owns channel/info display formatting and presentation orchestration."""

    def __init__(
        self,
        node: "Node",
        *,
        export_runtime: _NodeChannelExportRuntime,
    ) -> None:
        self._node = node
        self._export_runtime = export_runtime

    def show_channels(self) -> None:
        """Print channels and URL exports preserving historical output behavior."""
        print("Channels:")
        with self._node._channels_lock:  # noqa: SLF001
            channels_snapshot: list[channel_pb2.Channel] = []
            for source_channel in self._node.channels or []:
                copied_channel = channel_pb2.Channel()
                copied_channel.CopyFrom(source_channel)
                channels_snapshot.append(copied_channel)
        if channels_snapshot:
            logger.debug("self.channels:%s", channels_snapshot)
            for channel in channels_snapshot:
                channel_string = messageToJson(channel.settings)
                if channel_pb2.Channel.Role.Name(channel.role) != "DISABLED":
                    print(
                        f"  Index {channel.index}: {channel_pb2.Channel.Role.Name(channel.role)} "
                        f"psk={pskToString(channel.settings.psk)} {channel_string}"
                    )
        public_url = self._export_runtime.get_url(include_all=False)
        admin_url = self._export_runtime.get_url(include_all=True)
        print(f"\nPrimary channel URL: {public_url}")
        if admin_url != public_url:
            print(f"Complete URL (includes all channels): {admin_url}")

    def show_info(self) -> None:
        """Print local/module preferences and current channel presentation."""
        prefs = ""
        if self._node.localConfig:
            prefs = messageToJson(self._node.localConfig, multiline=True)
        print(f"Preferences: {prefs}\n")
        prefs = ""
        if self._node.moduleConfig:
            prefs = messageToJson(self._node.moduleConfig, multiline=True)
        print(f"Module preferences: {prefs}\n")
        self.show_channels()
