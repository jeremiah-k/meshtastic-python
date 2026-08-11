"""Channel normalization/fill runtime owner."""

from meshtastic.node_runtime.channel_state import _NodeChannelState


class _NodeChannelNormalizationRuntime:
    """Own channel normalization behavior over the shared channel-state owner."""

    def __init__(self, channel_state: _NodeChannelState) -> None:
        self._channel_state = channel_state

    def _fixup_channels(self) -> None:
        """Normalize cached channel indexes and disabled-channel fill."""
        self._channel_state.normalize()

    def _fixup_channels_locked(self) -> None:
        """Normalize channels while the caller already holds the owner lock."""
        self._channel_state._normalize_locked()  # noqa: SLF001

    def _fill_channels(self) -> None:
        """Append disabled channels up to the device channel limit."""
        self._channel_state.fill()

    def _fill_channels_locked(self) -> None:
        """Append disabled channels while the caller holds the owner lock."""
        self._channel_state._fill_locked()  # noqa: SLF001
