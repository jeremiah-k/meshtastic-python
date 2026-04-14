"""Shared helper functions for setURL transaction runtime."""

from meshtastic.protobuf import channel_pb2


def _channels_fingerprint(
    channels: list[channel_pb2.Channel],
) -> tuple[bytes, ...]:
    """
    Return an immutable, deterministic fingerprint of channel states for comparison.

    Parameters
    ----------
    channels : list[channel_pb2.Channel]
        List of channel protobuf objects to fingerprint.

    Returns
    -------
    tuple[bytes, ...]
        Immutable tuple of serialized channel byte strings, suitable for
        equality comparison and hashing.
    """
    return tuple(c.SerializeToString() for c in channels)


def _channel_matches_desired(
    actual: channel_pb2.Channel,
    desired: channel_pb2.Channel,
) -> bool:
    """
    Return True if *actual* matches *desired* by serialized byte content.

    Parameters
    ----------
    actual : channel_pb2.Channel
        The channel protobuf as currently present on the device.
    desired : channel_pb2.Channel
        The channel protobuf we want to apply.

    Returns
    -------
    bool
        True when the serialised representations are identical.
    """
    return actual.SerializeToString() == desired.SerializeToString()


def _compute_remaining_channel_writes(
    actual_channels: list[channel_pb2.Channel],
    desired_channels_by_index: dict[int, channel_pb2.Channel],
) -> set[int]:
    """
    Determine which desired channel indices still need to be written.

    Parameters
    ----------
    actual_channels : list[channel_pb2.Channel]
        The current channel list from the device.
    desired_channels_by_index : dict[int, channel_pb2.Channel]
        Mapping of channel index to the desired channel configuration.

    Returns
    -------
    set[int]
        Indices whose desired configuration does not yet match the device.
    """
    remaining: set[int] = set()
    for idx, desired in desired_channels_by_index.items():
        if idx >= len(actual_channels):
            remaining.add(idx)
        elif not _channel_matches_desired(actual_channels[idx], desired):
            remaining.add(idx)
    return remaining
