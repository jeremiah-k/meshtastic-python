"""Channel/config request bootstrap runtime owner."""

import logging
from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING, cast

from meshtastic.protobuf import admin_pb2, channel_pb2, mesh_pb2

from .channel_normalization_runtime import _NodeChannelNormalizationRuntime
from .shared import MAX_CHANNELS

if TYPE_CHECKING:
    from meshtastic.node import Node

logger = logging.getLogger(__name__)


class _LocalConfigFieldProbe:
    """Expose local-config field presence as a boolean wait target."""

    def __init__(self, *, has_field_fn: Callable[[str], bool], name: str) -> None:
        self._has_field_fn = has_field_fn
        self._name = name

    @property
    def is_set(self) -> bool:
        """Return whether the target localConfig field is currently set."""
        try:
            return bool(self._has_field_fn(self._name))
        except (TypeError, ValueError) as exc:
            logger.debug("HasField check failed for %r: %s", self._name, exc)
            return False


class _ChannelRequestCompletionProbe:
    """Expose channel-request completion as a boolean wait target."""

    def __init__(self, *, node: "Node", channel_response_runtime: object) -> None:
        self._node = node
        self._channel_response_runtime = channel_response_runtime

    @property
    def is_set(self) -> bool:
        """Return True once channels are loaded or the request has terminally failed."""
        with self._node._channels_lock:  # noqa: SLF001
            if self._node.channels is not None:
                return True
        has_channel_request_failed = getattr(
            self._channel_response_runtime,
            "hasChannelRequestFailed",
            None,
        ) or getattr(
            self._channel_response_runtime,
            "has_channel_request_failed",
            None,
        )
        return bool(
            callable(has_channel_request_failed) and has_channel_request_failed()
        )


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

    def setChannels(self, channels: Sequence[channel_pb2.Channel]) -> None:
        """Set channels from sequence with copy + normalization semantics."""
        with self._node._channels_lock:  # noqa: SLF001
            copied_channels: list[channel_pb2.Channel] = []
            for source_channel in channels:
                copied_channel = channel_pb2.Channel()
                copied_channel.CopyFrom(source_channel)
                copied_channels.append(copied_channel)
            self._node.channels = copied_channels
            self._normalization_runtime._fixup_channels_locked()

    def set_channels(self, channels: Sequence[channel_pb2.Channel]) -> None:
        """COMPAT_STABLE_SHIM: Silent alias for setChannels."""
        return self.setChannels(channels)

    def requestChannels(self, *, starting_index: int = 0) -> None:
        """Bootstrap channel request flow from ``starting_index``."""
        logger.debug("requestChannels for nodeNum:%s", self._node.nodeNum)
        if not 0 <= starting_index < MAX_CHANNELS:
            logger.warning(
                "Invalid starting_index %d (must be 0-%d), ignoring request.",
                starting_index,
                MAX_CHANNELS - 1,
            )
            return
        if starting_index == 0:
            with self._node._channels_lock:  # noqa: SLF001
                self._node.channels = None
                self._node.partialChannels = []
        self.requestChannel(starting_index)

    def request_channels(self, *, starting_index: int = 0) -> None:
        """COMPAT_STABLE_SHIM: Silent alias for requestChannels."""
        return self.requestChannels(starting_index=starting_index)

    def waitForConfig(self, *, attribute: str = "channels") -> bool:
        """Wait for node attribute using historical timeout semantics."""
        if attribute == "channels":
            channel_response_runtime = getattr(
                self._node,
                "_channel_response_runtime",
                None,
            )
            has_channel_request_failed = (
                getattr(channel_response_runtime, "hasChannelRequestFailed", None)
                or getattr(channel_response_runtime, "has_channel_request_failed", None)
                if channel_response_runtime is not None
                else None
            )
            if callable(has_channel_request_failed):
                probe = _ChannelRequestCompletionProbe(
                    node=self._node,
                    channel_response_runtime=channel_response_runtime,
                )
                completed = self._node._timeout.waitForSet(  # noqa: SLF001
                    probe,
                    attrs=("is_set",),
                )
                if not completed:
                    return False
                with self._node._channels_lock:  # noqa: SLF001
                    if self._node.channels is not None:
                        return True
                return not bool(has_channel_request_failed())
            return self._node._timeout.waitForSet(  # noqa: SLF001
                self._node,
                attrs=("channels",),
            )

        local_config = self._node.localConfig
        has_field = getattr(local_config, "HasField", None)
        if callable(has_field):
            return self._node._timeout.waitForSet(  # noqa: SLF001
                _LocalConfigFieldProbe(
                    has_field_fn=cast(Callable[[str], bool], has_field),
                    name=attribute,
                ),
                attrs=("is_set",),
            )

        return self._node._timeout.waitForSet(  # noqa: SLF001
            local_config,
            attrs=(attribute,),
        )

    def wait_for_config(self, *, attribute: str = "channels") -> bool:
        """COMPAT_STABLE_SHIM: Silent alias for waitForConfig."""
        return self.waitForConfig(attribute=attribute)

    def requestChannel(self, channel_num: int) -> mesh_pb2.MeshPacket | None:
        """Send one get-channel request preserving progress logging behavior."""
        if not 0 <= channel_num < MAX_CHANNELS:
            logger.warning(
                "Invalid channel_num %d (must be 0-%d), ignoring request.",
                channel_num,
                MAX_CHANNELS - 1,
            )
            return None
        channel_response_runtime = getattr(
            self._node,
            "_channel_response_runtime",
            None,
        )
        mark_channel_request_sent = (
            getattr(channel_response_runtime, "markChannelRequestSent", None)
            or getattr(channel_response_runtime, "mark_channel_request_sent", None)
            if channel_response_runtime is not None
            else None
        )
        if callable(mark_channel_request_sent):
            mark_channel_request_sent(channel_num)

        message = admin_pb2.AdminMessage()
        # Protocol uses 1-indexed channel numbers; API uses 0-indexed
        message.get_channel_request = channel_num + 1

        if self._node != self._node.iface.localNode:
            logger.info(
                "Requesting channel %s info from remote node (this could take a while)",
                channel_num,
            )
        else:
            logger.debug("Requesting channel %s", channel_num)

        request = self._node._send_admin(
            message,
            wantResponse=True,
            onResponse=self._node.onResponseRequestChannel,
        )
        if request is None:
            mark_channel_request_send_failed = (
                getattr(
                    channel_response_runtime,
                    "markChannelRequestSendFailed",
                    None,
                )
                or getattr(
                    channel_response_runtime,
                    "mark_channel_request_send_failed",
                    None,
                )
                if channel_response_runtime is not None
                else None
            )
            if callable(mark_channel_request_send_failed):
                mark_channel_request_send_failed(channel_num)
        return request

    def request_channel(self, channel_num: int) -> mesh_pb2.MeshPacket | None:
        """COMPAT_STABLE_SHIM: Silent alias for requestChannel."""
        return self.requestChannel(channel_num)
