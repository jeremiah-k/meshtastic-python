"""Channel/config request bootstrap runtime owner."""

import logging
from collections.abc import Sequence
from typing import TYPE_CHECKING

from meshtastic.protobuf import admin_pb2, channel_pb2, mesh_pb2

from .channel_normalization_runtime import _NodeChannelNormalizationRuntime

if TYPE_CHECKING:
    from meshtastic.node import Node

logger = logging.getLogger(__name__)


class _NodeChannelRequestRuntime:
    """Owns channel/config bootstrap, waiting, and request-channel send path."""

    def __init__(
        self,
        node: "Node",
        *,
        normalization_runtime: _NodeChannelNormalizationRuntime,
    ) -> None:
        self._node = node
        self._normalization_runtime = normalization_runtime

    def set_channels(self, channels: Sequence[channel_pb2.Channel]) -> None:
        """Set channels from sequence with copy + normalization semantics."""
        with self._node._channels_lock:  # noqa: SLF001
            copied_channels: list[channel_pb2.Channel] = []
            for source_channel in channels:
                copied_channel = channel_pb2.Channel()
                copied_channel.CopyFrom(source_channel)
                copied_channels.append(copied_channel)
            self._node.channels = copied_channels
            self._normalization_runtime.fixup_channels_locked()

    def request_channels(self, *, starting_index: int = 0) -> None:
        """Bootstrap channel request flow from ``starting_index``."""
        logger.debug("requestChannels for nodeNum:%s", self._node.nodeNum)
        if starting_index == 0:
            with self._node._channels_lock:  # noqa: SLF001
                self._node.channels = None
                self._node.partialChannels = []
        self.request_channel(starting_index)

    def wait_for_config(self, *, attribute: str = "channels") -> bool:
        """Wait for node attribute using historical timeout semantics."""
        if attribute == "channels":
            return self._node._timeout.waitForSet(  # noqa: SLF001
                self._node,
                attrs=("channels",),
            )

        local_config = self._node.localConfig
        has_field = getattr(local_config, "HasField", None)
        if callable(has_field):
            field_name = attribute

            class _LocalConfigFieldProbe:
                """Expose local-config field presence as a boolean wait target."""

                def __init__(self, *, has_field_fn: object, name: str) -> None:
                    self._has_field_fn = has_field_fn
                    self._name = name

                @property
                def is_set(self) -> bool:
                    """Return whether the target localConfig field is currently set."""
                    has_field_fn = self._has_field_fn
                    if not callable(has_field_fn):
                        return False
                    try:
                        return bool(has_field_fn(self._name))
                    except (TypeError, ValueError):
                        return False

            return self._node._timeout.waitForSet(  # noqa: SLF001
                _LocalConfigFieldProbe(has_field_fn=has_field, name=field_name),
                attrs=("is_set",),
            )

        return self._node._timeout.waitForSet(  # noqa: SLF001
            local_config,
            attrs=(attribute,),
        )

    def request_channel(self, channel_num: int) -> mesh_pb2.MeshPacket | None:
        """Send one get-channel request preserving progress logging behavior."""
        message = admin_pb2.AdminMessage()
        message.get_channel_request = channel_num + 1

        if self._node != self._node.iface.localNode:
            logger.info(
                "Requesting channel %s info from remote node (this could take a while)",
                channel_num,
            )
        else:
            logger.debug("Requesting channel %s", channel_num)

        return self._node._send_admin(
            message,
            wantResponse=True,
            onResponse=self._node.onResponseRequestChannel,
        )
