"""Unit tests for channel_normalization_runtime module."""

# pylint: disable=redefined-outer-name

import logging
from unittest.mock import MagicMock

import pytest

from meshtastic.node_runtime.channel_normalization_runtime import (
    _NodeChannelNormalizationRuntime,
)
from meshtastic.node_runtime.channel_state import _NodeChannelState
from meshtastic.node_runtime.shared import MAX_CHANNELS
from meshtastic.protobuf import channel_pb2


@pytest.fixture
def channel_state() -> _NodeChannelState:
    """Provide isolated owned channel state."""
    state = _NodeChannelState()
    state.channels = []
    return state


@pytest.fixture
def runtime(channel_state: _NodeChannelState) -> _NodeChannelNormalizationRuntime:
    """Provide a normalization runtime over owned channel state."""
    return _NodeChannelNormalizationRuntime(channel_state)


@pytest.mark.unit
class TestFixupChannelsLocked:
    """Tests for _NodeChannelNormalizationRuntime._fixup_channels_locked()."""

    def test_fixup_channels_locked_with_none_channels_returns_early(
        self, channel_state: _NodeChannelState
    ) -> None:
        """When channels is None, _fixup_channels_locked should return early."""
        channel_state.channels = None
        runtime = _NodeChannelNormalizationRuntime(channel_state)

        # Should not raise and should return without error
        runtime._fixup_channels_locked()

        # Channels should still be None (no modification)
        assert channel_state.channels is None

    def test_fixup_channels_locked_truncates_when_exceeds_max(
        self, channel_state: _NodeChannelState, caplog: pytest.LogCaptureFixture
    ) -> None:
        """When channels exceed MAX_CHANNELS, should truncate and log warning."""
        # Create more channels than MAX_CHANNELS
        num_channels = MAX_CHANNELS + 3
        channel_state.channels = []
        for i in range(num_channels):
            ch = channel_pb2.Channel()
            ch.index = i
            ch.role = channel_pb2.Channel.Role.PRIMARY
            channel_state.channels.append(ch)

        runtime = _NodeChannelNormalizationRuntime(channel_state)

        with caplog.at_level(logging.WARNING):
            runtime._fixup_channels_locked()

        # Should have truncated to MAX_CHANNELS
        assert len(channel_state.channels) == MAX_CHANNELS

        # Should have logged a warning about truncation
        assert any(
            "Truncating channel list" in record.message for record in caplog.records
        )
        assert any(
            f"from {num_channels} to {MAX_CHANNELS}" in record.message
            for record in caplog.records
        )

    def test_fixup_channels_locked_reindexes_all_channels(
        self, channel_state: _NodeChannelState
    ) -> None:
        """All channels should have their index field set correctly."""
        # Create channels with incorrect indexes
        channel_state.channels = []
        for i in range(3):
            ch = channel_pb2.Channel()
            ch.index = 99 + i  # Wrong index
            ch.role = channel_pb2.Channel.Role.PRIMARY
            channel_state.channels.append(ch)

        runtime = _NodeChannelNormalizationRuntime(channel_state)
        runtime._fixup_channels_locked()

        # All channels should now have correct sequential indexes
        for i, ch in enumerate(channel_state.channels):
            assert ch.index == i


@pytest.mark.unit
class TestFixupChannels:
    """Tests for _NodeChannelNormalizationRuntime._fixup_channels()."""

    def test_fixup_channels_delegates_to_state_owner(
        self,
        channel_state: _NodeChannelState,
        runtime: _NodeChannelNormalizationRuntime,
    ) -> None:
        """_fixup_channels should let the state owner manage normalization locking."""
        channel_state.normalize = MagicMock()  # type: ignore[method-assign]

        runtime._fixup_channels()

        channel_state.normalize.assert_called_once_with()


@pytest.mark.unit
class TestFillChannelsLocked:
    """Tests for _NodeChannelNormalizationRuntime._fill_channels_locked()."""

    def test_fill_channels_locked_with_none_channels_returns_early(
        self, channel_state: _NodeChannelState
    ) -> None:
        """When channels is None, _fill_channels_locked should return early."""
        channel_state.channels = None
        runtime = _NodeChannelNormalizationRuntime(channel_state)

        # Should not raise and should return without error
        runtime._fill_channels_locked()

        # Channels should still be None
        assert channel_state.channels is None

    def test_fill_channels_locked_with_full_list_no_changes(
        self, channel_state: _NodeChannelState
    ) -> None:
        """When channel list is already at MAX_CHANNELS, no changes should occur."""
        channel_state.channels = []
        for i in range(MAX_CHANNELS):
            ch = channel_pb2.Channel()
            ch.index = i
            ch.role = channel_pb2.Channel.Role.PRIMARY
            channel_state.channels.append(ch)

        original_channels = list(channel_state.channels)
        runtime = _NodeChannelNormalizationRuntime(channel_state)

        runtime._fill_channels_locked()

        # List length should not change
        assert len(channel_state.channels) == MAX_CHANNELS
        # Original channels should be preserved
        for i, ch in enumerate(channel_state.channels):
            assert ch is original_channels[i]

    def test_fill_channels_locked_fills_partial_list_with_disabled(
        self, channel_state: _NodeChannelState
    ) -> None:
        """Partial channel list should be filled with DISABLED channels."""
        initial_count = 3
        channel_state.channels = []
        for i in range(initial_count):
            ch = channel_pb2.Channel()
            ch.index = i
            ch.role = channel_pb2.Channel.Role.PRIMARY
            channel_state.channels.append(ch)

        runtime = _NodeChannelNormalizationRuntime(channel_state)
        runtime._fill_channels_locked()

        # Should now have MAX_CHANNELS total
        assert len(channel_state.channels) == MAX_CHANNELS

        # First channels should be unchanged (PRIMARY)
        for i in range(initial_count):
            assert channel_state.channels[i].role == channel_pb2.Channel.Role.PRIMARY

        # Remaining channels should be DISABLED
        for i in range(initial_count, MAX_CHANNELS):
            assert channel_state.channels[i].role == channel_pb2.Channel.Role.DISABLED
            assert channel_state.channels[i].index == i

    def test_fill_channels_locked_reindexes_existing_channels_before_append(
        self, channel_state: _NodeChannelState
    ) -> None:
        """Existing channels should be reindexed before DISABLED channels are appended."""
        first = channel_pb2.Channel(index=3, role=channel_pb2.Channel.Role.PRIMARY)
        second = channel_pb2.Channel(index=9, role=channel_pb2.Channel.Role.SECONDARY)
        channel_state.channels = [first, second]
        runtime = _NodeChannelNormalizationRuntime(channel_state)

        runtime._fill_channels_locked()

        assert channel_state.channels[0].index == 0
        assert channel_state.channels[1].index == 1
        for i in range(2, MAX_CHANNELS):
            assert channel_state.channels[i].index == i

    def test_fill_channels_locked_with_empty_list_fills_all_disabled(
        self, channel_state: _NodeChannelState
    ) -> None:
        """Empty channel list should be filled entirely with DISABLED channels."""
        channel_state.channels = []
        runtime = _NodeChannelNormalizationRuntime(channel_state)

        runtime._fill_channels_locked()

        # Should have MAX_CHANNELS all DISABLED
        assert len(channel_state.channels) == MAX_CHANNELS
        for i, ch in enumerate(channel_state.channels):
            assert ch.role == channel_pb2.Channel.Role.DISABLED
            assert ch.index == i


@pytest.mark.unit
class TestFillChannels:
    """Tests for _NodeChannelNormalizationRuntime._fill_channels()."""

    def test_fill_channels_delegates_to_state_owner(
        self,
        channel_state: _NodeChannelState,
        runtime: _NodeChannelNormalizationRuntime,
    ) -> None:
        """_fill_channels should let the state owner manage fill locking."""
        channel_state.fill = MagicMock()  # type: ignore[method-assign]

        runtime._fill_channels()

        channel_state.fill.assert_called_once_with()


@pytest.mark.unit
class TestIntegration:
    """Combined workflow tests for fixup and fill behavior."""

    def test_fixup_channels_full_workflow(
        self, channel_state: _NodeChannelState, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Full workflow: truncate, reindex, and fill with DISABLED channels."""
        # Create more channels than MAX_CHANNELS with wrong indexes
        num_channels = MAX_CHANNELS + 2
        channel_state.channels = []
        for i in range(num_channels):
            ch = channel_pb2.Channel()
            ch.index = 100 + i  # Wrong indexes
            ch.role = channel_pb2.Channel.Role.SECONDARY
            channel_state.channels.append(ch)

        runtime = _NodeChannelNormalizationRuntime(channel_state)

        with caplog.at_level(logging.WARNING):
            runtime._fixup_channels()

        # Should be truncated to MAX_CHANNELS
        assert len(channel_state.channels) == MAX_CHANNELS

        # All channels should have correct sequential indexes
        for i, ch in enumerate(channel_state.channels):
            assert ch.index == i

        # Warning should have been logged
        assert any(
            "Truncating channel list" in record.message for record in caplog.records
        )

    def test_fixup_channels_with_partial_list(
        self, channel_state: _NodeChannelState
    ) -> None:
        """Partial channel list should be reindexed and filled."""
        initial_count = 2
        channel_state.channels = []
        for i in range(initial_count):
            ch = channel_pb2.Channel()
            ch.index = 50 + i  # Wrong indexes
            ch.role = channel_pb2.Channel.Role.PRIMARY
            channel_state.channels.append(ch)

        runtime = _NodeChannelNormalizationRuntime(channel_state)
        runtime._fixup_channels()

        # Should be filled to MAX_CHANNELS
        assert len(channel_state.channels) == MAX_CHANNELS

        # All channels should have correct sequential indexes
        for i, ch in enumerate(channel_state.channels):
            assert ch.index == i

        # First channels should be PRIMARY
        for i in range(initial_count):
            assert channel_state.channels[i].role == channel_pb2.Channel.Role.PRIMARY

        # Remaining should be DISABLED
        for i in range(initial_count, MAX_CHANNELS):
            assert channel_state.channels[i].role == channel_pb2.Channel.Role.DISABLED
