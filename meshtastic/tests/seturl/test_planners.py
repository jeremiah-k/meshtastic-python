"""Tests for _SetUrlAddOnlyPlanner and _SetUrlReplacePlanner."""

import logging
from typing import NoReturn
from unittest.mock import MagicMock

import pytest

from meshtastic.node_runtime.seturl_runtime import (
    _SetUrlAddOnlyPlanner,
    _SetUrlParsedInput,
    _SetUrlReplacePlanner,
)
from meshtastic.protobuf import apponly_pb2, channel_pb2, localonly_pb2
from meshtastic.tests.seturl.conftest import (
    _make_channel,
    _make_channel_set_with_lora,
    _make_raise_error_side_effect,
)


class TestSetUrlAddOnlyPlanner:
    """Tests for _SetUrlAddOnlyPlanner."""

    @pytest.mark.unit
    def test_capture_original_lora_snapshot_without_lora_returns_none(
        self, mock_local_node: MagicMock
    ) -> None:
        """capture_original_lora_snapshot returns None when no LoRa update."""
        channel_set = apponly_pb2.ChannelSet()
        settings = channel_set.settings.add()
        settings.name = "test"

        parsed_input = _SetUrlParsedInput(
            channel_set=channel_set,
            has_lora_update=False,
        )

        admin_context = MagicMock()
        planner = _SetUrlAddOnlyPlanner(
            mock_local_node,
            parsed_input=parsed_input,
            admin_context=admin_context,
        )

        result = planner.capture_original_lora_snapshot()

        assert result is None

    @pytest.mark.unit
    def test_capture_original_lora_snapshot_with_lora_returns_snapshot(
        self, mock_local_node: MagicMock
    ) -> None:
        """capture_original_lora_snapshot returns snapshot when LoRa update."""
        mock_local_node.localConfig.lora.hop_limit = 5

        channel_set = _make_channel_set_with_lora("test")

        parsed_input = _SetUrlParsedInput(
            channel_set=channel_set,
            has_lora_update=True,
        )

        admin_context = MagicMock()
        planner = _SetUrlAddOnlyPlanner(
            mock_local_node,
            parsed_input=parsed_input,
            admin_context=admin_context,
        )

        result = planner.capture_original_lora_snapshot()

        assert result is not None
        assert result.hop_limit == 5

    @pytest.mark.unit
    def test_capture_original_lora_snapshot_without_loaded_lora_raises_error(
        self, mock_local_node: MagicMock
    ) -> None:
        """capture_original_lora_snapshot raises error when LoRa not loaded."""
        mock_local_node.localConfig = localonly_pb2.LocalConfig()

        mock_local_node._raise_interface_error = MagicMock(
            side_effect=_make_raise_error_side_effect()
        )

        channel_set = _make_channel_set_with_lora("test")

        parsed_input = _SetUrlParsedInput(
            channel_set=channel_set,
            has_lora_update=True,
        )

        admin_context = MagicMock()
        planner = _SetUrlAddOnlyPlanner(
            mock_local_node,
            parsed_input=parsed_input,
            admin_context=admin_context,
        )

        with pytest.raises(ValueError, match="LoRa config must be loaded"):
            planner.capture_original_lora_snapshot()


class TestSetUrlReplacePlanner:
    """Tests for _SetUrlReplacePlanner."""

    @pytest.mark.unit
    def test_build_plan_with_valid_channels(self, mock_local_node: MagicMock) -> None:
        """build_plan creates staged channels from URL settings."""
        mock_local_node.channels = [
            _make_channel(0, channel_pb2.Channel.Role.PRIMARY, "old"),
            _make_channel(1, channel_pb2.Channel.Role.DISABLED),
        ]

        channel_set = apponly_pb2.ChannelSet()
        settings = channel_set.settings.add()
        settings.name = "newprimary"
        settings.psk = b"\x01"

        parsed_input = _SetUrlParsedInput(
            channel_set=channel_set,
            has_lora_update=False,
        )

        admin_context = MagicMock()
        admin_context.named_admin_index_for_write = None

        planner = _SetUrlReplacePlanner(
            mock_local_node,
            parsed_input=parsed_input,
            admin_context=admin_context,
        )

        plan = planner.build_plan()

        assert plan.max_channels == 2
        assert len(plan.staged_channels) == 2
        assert plan.staged_channels[0].role == channel_pb2.Channel.Role.PRIMARY
        assert plan.staged_channels[1].role == channel_pb2.Channel.Role.DISABLED

    @pytest.mark.unit
    def test_build_plan_with_lora_requires_loaded_config(
        self, mock_local_node: MagicMock
    ) -> None:
        """build_plan with LoRa update requires loaded LoRa config."""
        mock_local_node.channels = [
            _make_channel(0, channel_pb2.Channel.Role.PRIMARY, "old")
        ]
        mock_local_node.localConfig = localonly_pb2.LocalConfig()

        mock_local_node._raise_interface_error = MagicMock(
            side_effect=_make_raise_error_side_effect()
        )

        channel_set = _make_channel_set_with_lora("test")

        parsed_input = _SetUrlParsedInput(
            channel_set=channel_set,
            has_lora_update=True,
        )

        admin_context = MagicMock()
        admin_context.named_admin_index_for_write = None

        planner = _SetUrlReplacePlanner(
            mock_local_node,
            parsed_input=parsed_input,
            admin_context=admin_context,
        )

        with pytest.raises(
            ValueError, match="LoRa config must be loaded before setURL"
        ):
            planner.build_plan()

    @pytest.mark.unit
    def test_build_plan_with_too_many_channels_logs_warning(
        self, mock_local_node: MagicMock, caplog: pytest.LogCaptureFixture
    ) -> None:
        """build_plan with more URL channels than device slots logs warning."""
        mock_local_node.channels = [
            _make_channel(0, channel_pb2.Channel.Role.PRIMARY, "old"),
        ]

        channel_set = apponly_pb2.ChannelSet()
        for i in range(3):
            settings = channel_set.settings.add()
            settings.name = f"channel{i}"
            settings.psk = b"\x01"

        parsed_input = _SetUrlParsedInput(
            channel_set=channel_set,
            has_lora_update=False,
        )

        admin_context = MagicMock()
        admin_context.named_admin_index_for_write = None

        planner = _SetUrlReplacePlanner(
            mock_local_node,
            parsed_input=parsed_input,
            admin_context=admin_context,
        )

        with caplog.at_level(logging.WARNING):
            plan = planner.build_plan()

        assert "more than 1 channels" in caplog.text
        assert len(plan.staged_channels) == 1

    @pytest.mark.unit
    def test_build_plan_with_multiple_admin_channels_raises_error(
        self, mock_local_node: MagicMock
    ) -> None:
        """build_plan with multiple admin channels raises error."""
        mock_local_node.channels = [
            _make_channel(0, channel_pb2.Channel.Role.PRIMARY, "old"),
            _make_channel(1, channel_pb2.Channel.Role.DISABLED),
        ]

        mock_local_node._raise_interface_error = MagicMock(
            side_effect=_make_raise_error_side_effect()
        )

        channel_set = apponly_pb2.ChannelSet()
        for _i in range(2):
            settings = channel_set.settings.add()
            settings.name = "admin"
            settings.psk = b"\x01"

        parsed_input = _SetUrlParsedInput(
            channel_set=channel_set,
            has_lora_update=False,
        )

        admin_context = MagicMock()
        admin_context.named_admin_index_for_write = None

        planner = _SetUrlReplacePlanner(
            mock_local_node,
            parsed_input=parsed_input,
            admin_context=admin_context,
        )

        with pytest.raises(ValueError, match="multiple channels named 'admin'"):
            planner.build_plan()
