"""Channel normalization/fill runtime owner."""

import logging
from typing import TYPE_CHECKING

from meshtastic.node_runtime.shared import MAX_CHANNELS
from meshtastic.protobuf import channel_pb2

if TYPE_CHECKING:
    from meshtastic.node import Node

logger = logging.getLogger(__name__)


class _NodeChannelNormalizationRuntime:
    """Owns channel index normalization and disabled-channel fill behavior."""

    def __init__(self, node: "Node") -> None:
        self._node = node

    def _fixup_channels(self) -> None:
        """Normalize channels under lock via ``_fixup_channels_locked`` semantics."""
        with self._node._channels_lock:  # noqa: SLF001
            self._fixup_channels_locked()

    def _fixup_channels_locked(self) -> None:
        """Normalize channel indices/size while ``_channels_lock`` is held."""
        channels = self._node.channels
        if channels is None:
            return

        if len(channels) > MAX_CHANNELS:
            logger.warning(
                "Truncating channel list from %d to %d entries",
                len(channels),
                MAX_CHANNELS,
            )
            del channels[MAX_CHANNELS:]

        if len(channels) < MAX_CHANNELS:
            self._fill_channels_locked()
        else:
            for index, channel in enumerate(channels):
                channel.index = index

    def _fill_channels(self) -> None:
        """Append disabled channels up to ``MAX_CHANNELS`` under lock."""
        with self._node._channels_lock:  # noqa: SLF001
            self._fill_channels_locked()

    def _fill_channels_locked(self) -> None:
        """Append disabled channels up to ``MAX_CHANNELS`` while lock is held."""
        channels = self._node.channels
        if channels is None:
            return

        for index, channel in enumerate(channels):
            channel.index = index

        index = len(channels)
        while index < MAX_CHANNELS:
            channel = channel_pb2.Channel()
            channel.role = channel_pb2.Channel.Role.DISABLED
            channel.index = index
            channels.append(channel)
            index += 1
