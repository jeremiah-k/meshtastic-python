"""Typed test support for Node doubles backed by owned channel state."""

from unittest.mock import MagicMock

from meshtastic.node_runtime.channel_state import _ChannelLock, _NodeChannelState
from meshtastic.protobuf import channel_pb2


def _attach_channel_state(node: MagicMock) -> _NodeChannelState:
    """Back a Node double's compatibility attributes with one state owner."""
    state = _NodeChannelState()

    def _get_channels(_node: object) -> list[channel_pb2.Channel] | None:
        return state.channels

    def _set_channels(
        _node: object, value: list[channel_pb2.Channel] | None
    ) -> None:
        state.channels = value

    def _get_partial_channels(_node: object) -> list[channel_pb2.Channel]:
        return state.partial_channels

    def _set_partial_channels(
        _node: object, value: list[channel_pb2.Channel]
    ) -> None:
        state.partial_channels = value

    def _get_channels_lock(_node: object) -> _ChannelLock:
        return state.lock

    def _set_channels_lock(_node: object, value: _ChannelLock) -> None:
        state._replace_lock(value)  # noqa: SLF001

    node._test_channel_state = state
    type(node).channels = property(_get_channels, _set_channels)
    type(node).partialChannels = property(  # noqa: N802 - compatibility test surface
        _get_partial_channels,
        _set_partial_channels,
    )
    type(node)._channels_lock = property(_get_channels_lock, _set_channels_lock)
    return state
