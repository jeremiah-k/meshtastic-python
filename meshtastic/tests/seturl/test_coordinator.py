"""Tests for _SetUrlTransactionCoordinator."""

from typing import NoReturn
from unittest.mock import MagicMock

import pytest

from meshtastic.node_runtime.seturl_runtime import (
    _SetUrlParsedInput,
    _SetUrlTransactionCoordinator,
)
from meshtastic.protobuf import apponly_pb2, channel_pb2, localonly_pb2
from meshtastic.tests.seturl.conftest import _make_channel


class TestSetUrlTransactionCoordinator:
    """Tests for _SetUrlTransactionCoordinator."""

    @pytest.mark.unit
    def test_init_with_none_localnode_raises_error(
        self, mock_local_node: MagicMock, mock_iface: MagicMock
    ) -> None:
        """__init__ with None localNode raises ValueError."""
        mock_iface.localNode = None

        def raise_error(msg: str) -> NoReturn:
            raise ValueError(msg)

        mock_local_node._raise_interface_error = MagicMock(side_effect=raise_error)

        channel_set = apponly_pb2.ChannelSet()
        settings = channel_set.settings.add()
        settings.name = "test"

        parsed_input = _SetUrlParsedInput(
            channel_set=channel_set,
            has_lora_update=False,
        )

        with pytest.raises(ValueError, match="Interface localNode not initialized"):
            _SetUrlTransactionCoordinator(mock_local_node, parsed_input=parsed_input)

    @pytest.mark.unit
    def test_init_creates_components(self, mock_local_node: MagicMock) -> None:
        """__init__ creates cache manager, execution engine, and rollback engine."""
        channel_set = apponly_pb2.ChannelSet()
        settings = channel_set.settings.add()
        settings.name = "test"

        parsed_input = _SetUrlParsedInput(
            channel_set=channel_set,
            has_lora_update=False,
        )

        coordinator = _SetUrlTransactionCoordinator(
            mock_local_node, parsed_input=parsed_input
        )

        assert coordinator._cache_manager is not None
        assert coordinator._execution_engine is not None
        assert not hasattr(coordinator, "_rollback_engine")

    @pytest.mark.unit
    def test_init_resolves_admin_context(self, mock_local_node: MagicMock) -> None:
        """_resolve_admin_context returns admin context with correct values."""
        mock_local_node._get_admin_channel_index.return_value = 1
        mock_local_node._get_named_admin_channel_index.return_value = 2

        channel_set = apponly_pb2.ChannelSet()
        settings = channel_set.settings.add()
        settings.name = "test"

        parsed_input = _SetUrlParsedInput(
            channel_set=channel_set,
            has_lora_update=False,
        )

        coordinator = _SetUrlTransactionCoordinator(
            mock_local_node, parsed_input=parsed_input
        )

        assert coordinator._admin_context.admin_index_for_write == 1
        assert coordinator._admin_context.named_admin_index_for_write == 2
        assert coordinator._admin_context.has_admin_write_node_named_admin is True

    @pytest.mark.unit
    def test_add_only_failure_with_lora_started_clears_both_caches(
        self, mock_local_node: MagicMock
    ) -> None:
        """Add-only partial failure with lora_write_started clears both channel and LoRa caches."""
        mock_local_node.channels = [
            _make_channel(0, channel_pb2.Channel.Role.PRIMARY, "primary"),
            _make_channel(1, channel_pb2.Channel.Role.DISABLED),
        ]
        mock_local_node.localConfig = localonly_pb2.LocalConfig()
        mock_local_node.localConfig.lora.hop_limit = 5

        channel_set = apponly_pb2.ChannelSet()
        settings = channel_set.settings.add()
        settings.name = "newchannel"
        settings.psk = b"\x01"
        channel_set.lora_config.hop_limit = 3

        parsed_input = _SetUrlParsedInput(
            channel_set=channel_set,
            has_lora_update=True,
        )

        coordinator = _SetUrlTransactionCoordinator(
            mock_local_node, parsed_input=parsed_input
        )

        def _failing_execute(*, parsed_input, admin_context, plan, state):
            state.written_indices.append(1)
            state.lora_write_started = True
            raise RuntimeError("simulated write timeout")

        coordinator._execution_engine.executeAddOnly = _failing_execute

        with pytest.raises(RuntimeError, match="simulated write timeout"):
            coordinator._apply_add_only()

        assert mock_local_node.channels is None
        assert not mock_local_node.localConfig.HasField("lora")

    @pytest.mark.unit
    def test_add_only_failure_without_lora_started_does_not_clear_lora(
        self, mock_local_node: MagicMock
    ) -> None:
        """Add-only partial failure without lora_write_started preserves LoRa cache."""
        mock_local_node.channels = [
            _make_channel(0, channel_pb2.Channel.Role.PRIMARY, "primary"),
            _make_channel(1, channel_pb2.Channel.Role.DISABLED),
        ]
        mock_local_node.localConfig = localonly_pb2.LocalConfig()
        mock_local_node.localConfig.lora.hop_limit = 5

        channel_set = apponly_pb2.ChannelSet()
        settings = channel_set.settings.add()
        settings.name = "newchannel"
        settings.psk = b"\x01"

        parsed_input = _SetUrlParsedInput(
            channel_set=channel_set,
            has_lora_update=False,
        )

        coordinator = _SetUrlTransactionCoordinator(
            mock_local_node, parsed_input=parsed_input
        )

        def _failing_execute(*, parsed_input, admin_context, plan, state):
            state.written_indices.append(1)
            state.lora_write_started = False
            raise RuntimeError("simulated write timeout")

        coordinator._execution_engine.executeAddOnly = _failing_execute

        with pytest.raises(RuntimeError, match="simulated write timeout"):
            coordinator._apply_add_only()

        assert mock_local_node.channels is None
        assert mock_local_node.localConfig.lora.hop_limit == 5
