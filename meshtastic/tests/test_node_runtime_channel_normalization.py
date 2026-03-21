"""Unit tests for channel_normalization_runtime module."""

import threading
from unittest.mock import MagicMock

import pytest

from meshtastic.node_runtime.channel_normalization_runtime import (
    _NodeChannelNormalizationRuntime,
)
from meshtastic.node_runtime.shared import MAX_CHANNELS
from meshtastic.protobuf import channel_pb2


@pytest.fixture
def mock_node():
    """Provide a minimal mock Node with channels list and lock."""
    node = MagicMock()
    node.channels = []
    node._channels_lock = threading.RLock()
    return node


@pytest.fixture
def runtime(mock_node):
    """Provide a _NodeChannelNormalizationRuntime instance with mock node."""
    return _NodeChannelNormalizationRuntime(mock_node)


@pytest.mark.unit
class TestFixupChannelsLocked:
    """Tests for _NodeChannelNormalizationRuntime.fixup_channels_locked()."""

    def test_fixup_channels_locked_with_none_channels_returns_early(self, mock_node):
        """When channels is None, fixup_channels_locked should return early."""
        mock_node.channels = None
        runtime = _NodeChannelNormalizationRuntime(mock_node)

        # Should not raise and should return without error
        runtime.fixup_channels_locked()

        # Channels should still be None (no modification)
        assert mock_node.channels is None

    def test_fixup_channels_locked_truncates_when_exceeds_max(self, mock_node, caplog):
        """When channels exceed MAX_CHANNELS, should truncate and log warning."""
        # Create more channels than MAX_CHANNELS
        num_channels = MAX_CHANNELS + 3
        mock_node.channels = []
        for i in range(num_channels):
            ch = channel_pb2.Channel()
            ch.index = i
            ch.role = channel_pb2.Channel.Role.PRIMARY
            mock_node.channels.append(ch)

        runtime = _NodeChannelNormalizationRuntime(mock_node)

        with caplog.at_level("WARNING"):
            runtime.fixup_channels_locked()

        # Should have truncated to MAX_CHANNELS
        assert len(mock_node.channels) == MAX_CHANNELS

        # Should have logged a warning about truncation
        assert any(
            "Truncating channel list" in record.message for record in caplog.records
        )
        assert any(
            f"from {num_channels} to {MAX_CHANNELS}" in record.message
            for record in caplog.records
        )

    def test_fixup_channels_locked_reindexes_all_channels(self, mock_node):
        """All channels should have their index field set correctly."""
        # Create channels with incorrect indexes
        mock_node.channels = []
        for i in range(3):
            ch = channel_pb2.Channel()
            ch.index = 99 + i  # Wrong index
            ch.role = channel_pb2.Channel.Role.PRIMARY
            mock_node.channels.append(ch)

        runtime = _NodeChannelNormalizationRuntime(mock_node)
        runtime.fixup_channels_locked()

        # All channels should now have correct sequential indexes
        for i, ch in enumerate(mock_node.channels):
            assert ch.index == i

    def test_fixup_channels_locked_calls_fill_channels_locked(self, mock_node):
        """fixup_channels_locked should call fill_channels_locked at the end."""
        runtime = _NodeChannelNormalizationRuntime(mock_node)
        runtime.fill_channels_locked = MagicMock()

        runtime.fixup_channels_locked()

        runtime.fill_channels_locked.assert_called_once()


@pytest.mark.unit
class TestFixupChannels:
    """Tests for _NodeChannelNormalizationRuntime.fixup_channels()."""

    def test_fixup_channels_acquires_lock_and_calls_locked_version(
        self, mock_node, runtime
    ):
        """fixup_channels should acquire lock and call fixup_channels_locked."""
        original_lock = mock_node._channels_lock
        mock_node._channels_lock = MagicMock()
        mock_node._channels_lock.__enter__ = MagicMock(
            return_value=original_lock.__enter__()
        )
        mock_node._channels_lock.__exit__ = MagicMock(
            return_value=original_lock.__exit__
        )

        runtime.fixup_channels()

        # Lock should have been acquired
        mock_node._channels_lock.__enter__.assert_called_once()
        mock_node._channels_lock.__exit__.assert_called_once()


@pytest.mark.unit
class TestFillChannelsLocked:
    """Tests for _NodeChannelNormalizationRuntime.fill_channels_locked()."""

    def test_fill_channels_locked_with_none_channels_returns_early(self, mock_node):
        """When channels is None, fill_channels_locked should return early."""
        mock_node.channels = None
        runtime = _NodeChannelNormalizationRuntime(mock_node)

        # Should not raise and should return without error
        runtime.fill_channels_locked()

        # Channels should still be None
        assert mock_node.channels is None

    def test_fill_channels_locked_with_full_list_no_changes(self, mock_node):
        """When channel list is already at MAX_CHANNELS, no changes should occur."""
        mock_node.channels = []
        for i in range(MAX_CHANNELS):
            ch = channel_pb2.Channel()
            ch.index = i
            ch.role = channel_pb2.Channel.Role.PRIMARY
            mock_node.channels.append(ch)

        original_channels = list(mock_node.channels)
        runtime = _NodeChannelNormalizationRuntime(mock_node)

        runtime.fill_channels_locked()

        # List length should not change
        assert len(mock_node.channels) == MAX_CHANNELS
        # Original channels should be preserved
        for i, ch in enumerate(mock_node.channels):
            assert ch is original_channels[i]

    def test_fill_channels_locked_fills_partial_list_with_disabled(self, mock_node):
        """Partial channel list should be filled with DISABLED channels."""
        initial_count = 3
        mock_node.channels = []
        for i in range(initial_count):
            ch = channel_pb2.Channel()
            ch.index = i
            ch.role = channel_pb2.Channel.Role.PRIMARY
            mock_node.channels.append(ch)

        runtime = _NodeChannelNormalizationRuntime(mock_node)
        runtime.fill_channels_locked()

        # Should now have MAX_CHANNELS total
        assert len(mock_node.channels) == MAX_CHANNELS

        # First channels should be unchanged (PRIMARY)
        for i in range(initial_count):
            assert mock_node.channels[i].role == channel_pb2.Channel.Role.PRIMARY

        # Remaining channels should be DISABLED
        for i in range(initial_count, MAX_CHANNELS):
            assert mock_node.channels[i].role == channel_pb2.Channel.Role.DISABLED
            assert mock_node.channels[i].index == i

    def test_fill_channels_locked_with_empty_list_fills_all_disabled(self, mock_node):
        """Empty channel list should be filled entirely with DISABLED channels."""
        mock_node.channels = []
        runtime = _NodeChannelNormalizationRuntime(mock_node)

        runtime.fill_channels_locked()

        # Should have MAX_CHANNELS all DISABLED
        assert len(mock_node.channels) == MAX_CHANNELS
        for i, ch in enumerate(mock_node.channels):
            assert ch.role == channel_pb2.Channel.Role.DISABLED
            assert ch.index == i


@pytest.mark.unit
class TestFillChannels:
    """Tests for _NodeChannelNormalizationRuntime.fill_channels()."""

    def test_fill_channels_acquires_lock_and_calls_locked_version(
        self, mock_node, runtime
    ):
        """fill_channels should acquire lock and call fill_channels_locked."""
        original_lock = mock_node._channels_lock
        mock_node._channels_lock = MagicMock()
        mock_node._channels_lock.__enter__ = MagicMock(
            return_value=original_lock.__enter__()
        )
        mock_node._channels_lock.__exit__ = MagicMock(
            return_value=original_lock.__exit__
        )

        runtime.fill_channels()

        # Lock should have been acquired
        mock_node._channels_lock.__enter__.assert_called_once()
        mock_node._channels_lock.__exit__.assert_called_once()


@pytest.mark.unit
class TestIntegration:
    """Integration tests for combined fixup and fill behavior."""

    def test_fixup_channels_full_workflow(self, mock_node, caplog):
        """Full workflow: truncate, reindex, and fill with DISABLED channels."""
        # Create more channels than MAX_CHANNELS with wrong indexes
        num_channels = MAX_CHANNELS + 2
        mock_node.channels = []
        for i in range(num_channels):
            ch = channel_pb2.Channel()
            ch.index = 100 + i  # Wrong indexes
            ch.role = channel_pb2.Channel.Role.SECONDARY
            mock_node.channels.append(ch)

        runtime = _NodeChannelNormalizationRuntime(mock_node)

        with caplog.at_level("WARNING"):
            runtime.fixup_channels()

        # Should be truncated to MAX_CHANNELS
        assert len(mock_node.channels) == MAX_CHANNELS

        # All channels should have correct sequential indexes
        for i, ch in enumerate(mock_node.channels):
            assert ch.index == i

        # Warning should have been logged
        assert any(
            "Truncating channel list" in record.message for record in caplog.records
        )

    def test_fixup_channels_with_partial_list(self, mock_node):
        """Partial channel list should be reindexed and filled."""
        initial_count = 2
        mock_node.channels = []
        for i in range(initial_count):
            ch = channel_pb2.Channel()
            ch.index = 50 + i  # Wrong indexes
            ch.role = channel_pb2.Channel.Role.PRIMARY
            mock_node.channels.append(ch)

        runtime = _NodeChannelNormalizationRuntime(mock_node)
        runtime.fixup_channels()

        # Should be filled to MAX_CHANNELS
        assert len(mock_node.channels) == MAX_CHANNELS

        # All channels should have correct sequential indexes
        for i, ch in enumerate(mock_node.channels):
            assert ch.index == i

        # First channels should be PRIMARY
        for i in range(initial_count):
            assert mock_node.channels[i].role == channel_pb2.Channel.Role.PRIMARY

        # Remaining should be DISABLED
        for i in range(initial_count, MAX_CHANNELS):
            assert mock_node.channels[i].role == channel_pb2.Channel.Role.DISABLED
