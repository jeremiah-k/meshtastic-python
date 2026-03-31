"""Tests for _SetUrlCacheManager."""

import logging
from typing import NoReturn
from unittest.mock import MagicMock

import pytest

from meshtastic.node_runtime.seturl_runtime import (
    _channels_fingerprint,
    _SetUrlCacheManager,
)
from meshtastic.protobuf import channel_pb2, config_pb2
from meshtastic.tests.seturl.conftest import _make_channel


def _setup_raise_error_mock(mock_local_node: MagicMock) -> None:
    """Set up mock to raise ValueError when _raise_interface_error is called."""

    def raise_error(msg: str) -> NoReturn:
        raise ValueError(msg)

    mock_local_node._raise_interface_error = MagicMock(side_effect=raise_error)


class TestSetUrlCacheManager:
    """Tests for _SetUrlCacheManager."""

    @pytest.mark.unit
    def test_apply_add_only_success_with_none_channels_logs_warning(
        self,
        cache_manager: _SetUrlCacheManager,
        mock_local_node: MagicMock,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """apply_add_only_success with None channels logs warning (line 381)."""
        mock_local_node.channels = None
        channels_to_write: list[tuple[channel_pb2.Channel, str]] = [
            (_make_channel(0, channel_pb2.Channel.Role.SECONDARY, "test"), "test")
        ]

        with caplog.at_level(logging.WARNING):
            cache_manager.apply_add_only_success(channels_to_write)

        assert "Channel cache unavailable after successful addOnly apply" in caplog.text

    @pytest.mark.unit
    def test_apply_add_only_success_with_out_of_range_index_invalidates_cache(
        self,
        cache_manager: _SetUrlCacheManager,
        mock_local_node: MagicMock,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """apply_add_only_success with out-of-range index invalidates cache (lines 388-393)."""
        mock_local_node.channels = [
            _make_channel(0, channel_pb2.Channel.Role.PRIMARY, "primary"),
        ]
        channels_to_write: list[tuple[channel_pb2.Channel, str]] = [
            (_make_channel(5, channel_pb2.Channel.Role.SECONDARY, "test"), "test")
        ]

        with caplog.at_level(logging.WARNING):
            cache_manager.apply_add_only_success(channels_to_write)

        assert "out of range during addOnly cache update" in caplog.text
        assert mock_local_node.channels is None
        assert mock_local_node.partialChannels == []

    @pytest.mark.unit
    def test_apply_add_only_success_updates_valid_channels(
        self, cache_manager: _SetUrlCacheManager, mock_local_node: MagicMock
    ) -> None:
        """apply_add_only_success updates valid channel indices."""
        primary = _make_channel(0, channel_pb2.Channel.Role.PRIMARY, "primary")
        disabled = _make_channel(1, channel_pb2.Channel.Role.DISABLED)
        mock_local_node.channels = [primary, disabled]

        new_channel = _make_channel(1, channel_pb2.Channel.Role.SECONDARY, "new")
        channels_to_write: list[tuple[channel_pb2.Channel, str]] = [
            (new_channel, "new")
        ]

        cache_manager.apply_add_only_success(channels_to_write)

        assert mock_local_node.channels[1].role == channel_pb2.Channel.Role.SECONDARY
        assert mock_local_node.channels[1].settings.name == "new"

    @pytest.mark.unit
    def test_apply_replace_channel_write_with_none_channels_raises_error(
        self, cache_manager: _SetUrlCacheManager, mock_local_node: MagicMock
    ) -> None:
        """apply_replace_channel_write with None channels raises error (line 401)."""
        mock_local_node.channels = None
        staged_channel = _make_channel(0, channel_pb2.Channel.Role.PRIMARY, "test")

        _setup_raise_error_mock(mock_local_node)

        with pytest.raises(ValueError, match="Config or channels not loaded"):
            cache_manager.apply_replace_channel_write(staged_channel)

    @pytest.mark.unit
    def test_apply_replace_channel_write_with_out_of_range_index_raises_error(
        self, cache_manager: _SetUrlCacheManager, mock_local_node: MagicMock
    ) -> None:
        """apply_replace_channel_write with out-of-range index raises error (line 405)."""
        mock_local_node.channels = [
            _make_channel(0, channel_pb2.Channel.Role.PRIMARY, "primary")
        ]
        staged_channel = _make_channel(5, channel_pb2.Channel.Role.SECONDARY, "test")

        _setup_raise_error_mock(mock_local_node)

        with pytest.raises(ValueError, match="out of range"):
            cache_manager.apply_replace_channel_write(staged_channel)

    @pytest.mark.unit
    def test_apply_replace_channel_write_with_negative_index_raises_error(
        self, cache_manager: _SetUrlCacheManager, mock_local_node: MagicMock
    ) -> None:
        """apply_replace_channel_write with negative index raises error."""
        mock_local_node.channels = [
            _make_channel(0, channel_pb2.Channel.Role.PRIMARY, "primary")
        ]
        staged_channel = _make_channel(-1, channel_pb2.Channel.Role.SECONDARY, "test")

        _setup_raise_error_mock(mock_local_node)

        with pytest.raises(ValueError, match="out of range"):
            cache_manager.apply_replace_channel_write(staged_channel)

    @pytest.mark.unit
    def test_apply_replace_channel_write_updates_valid_channel(
        self, cache_manager: _SetUrlCacheManager, mock_local_node: MagicMock
    ) -> None:
        """apply_replace_channel_write updates a valid channel."""
        primary = _make_channel(0, channel_pb2.Channel.Role.PRIMARY, "old")
        mock_local_node.channels = [primary]

        staged_channel = _make_channel(0, channel_pb2.Channel.Role.PRIMARY, "new")

        cache_manager.apply_replace_channel_write(staged_channel)

        assert mock_local_node.channels[0].settings.name == "new"

    @pytest.mark.unit
    def test_apply_replace_channel_write_mismatched_expected_ref_invalidates(
        self, cache_manager: _SetUrlCacheManager, mock_local_node: MagicMock
    ) -> None:
        """apply_replace_channel_write should invalidate cache on channel-cache content mismatch."""
        current_channels = [
            _make_channel(0, channel_pb2.Channel.Role.PRIMARY, "primary")
        ]
        stale_channels_ref = [
            _make_channel(0, channel_pb2.Channel.Role.SECONDARY, "secondary")
        ]
        mock_local_node.channels = current_channels
        staged_channel = _make_channel(0, channel_pb2.Channel.Role.PRIMARY, "new")

        _setup_raise_error_mock(mock_local_node)

        with pytest.raises(
            ValueError,
            match=r"Channel cache changed during replace-all cache update; aborting transaction\.",
        ):
            cache_manager.apply_replace_channel_write(
                staged_channel,
                expected_channels_fingerprint=_channels_fingerprint(stale_channels_ref),
            )

        assert mock_local_node.channels is None
        assert mock_local_node.partialChannels == []

    @pytest.mark.unit
    def test_clear_lora_cache_with_warning_clears_and_logs(
        self,
        cache_manager: _SetUrlCacheManager,
        mock_local_node: MagicMock,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """clear_lora_cache_with_warning clears LoRa cache and logs (lines 445-446)."""
        mock_local_node.localConfig.lora.hop_limit = 5

        with caplog.at_level(logging.WARNING):
            cache_manager.clear_lora_cache_with_warning("Test warning message")

        assert not mock_local_node.localConfig.HasField("lora")
        assert "Test warning message" in caplog.text

    @pytest.mark.unit
    def test_restore_lora_snapshot_restores_config(
        self, cache_manager: _SetUrlCacheManager, mock_local_node: MagicMock
    ) -> None:
        """restore_lora_snapshot restores LoRa config."""
        original_config = config_pb2.Config.LoRaConfig()
        original_config.hop_limit = 7
        original_config.region = config_pb2.Config.LoRaConfig.RegionCode.US

        cache_manager.restore_lora_snapshot(original_config)

        assert mock_local_node.localConfig.lora.hop_limit == 7
        assert (
            mock_local_node.localConfig.lora.region
            == config_pb2.Config.LoRaConfig.RegionCode.US
        )

    @pytest.mark.unit
    def test_apply_lora_success_updates_config(
        self, cache_manager: _SetUrlCacheManager, mock_local_node: MagicMock
    ) -> None:
        """apply_lora_success updates LoRa config in localConfig."""
        lora_config = config_pb2.Config.LoRaConfig()
        lora_config.hop_limit = 4

        cache_manager.apply_lora_success(lora_config)

        assert mock_local_node.localConfig.lora.hop_limit == 4

    @pytest.mark.unit
    def test_invalidate_channel_cache_clears_channels(
        self,
        cache_manager: _SetUrlCacheManager,
        mock_local_node: MagicMock,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """invalidate_channel_cache clears channels and partialChannels."""
        mock_local_node.channels = [
            _make_channel(0, channel_pb2.Channel.Role.PRIMARY, "test")
        ]
        mock_local_node.partialChannels = ["partial"]

        with caplog.at_level(logging.WARNING):
            cache_manager.invalidate_channel_cache("Cache invalidated for testing")

        assert mock_local_node.channels is None
        assert mock_local_node.partialChannels == []
        assert "Cache invalidated for testing" in caplog.text

    @pytest.mark.unit
    def test_restore_replace_channels_snapshot_restores_channels(
        self, cache_manager: _SetUrlCacheManager, mock_local_node: MagicMock
    ) -> None:
        """restore_replace_channels_snapshot restores channel list from snapshot."""
        original_channels = [
            _make_channel(0, channel_pb2.Channel.Role.PRIMARY, "primary"),
            _make_channel(1, channel_pb2.Channel.Role.SECONDARY, "secondary"),
        ]

        cache_manager.restore_replace_channels_snapshot(original_channels)

        assert len(mock_local_node.channels) == 2
        assert mock_local_node.channels[0].settings.name == "primary"
        assert mock_local_node.channels[1].settings.name == "secondary"
        assert mock_local_node.partialChannels == []
