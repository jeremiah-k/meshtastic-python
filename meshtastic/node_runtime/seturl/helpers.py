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
