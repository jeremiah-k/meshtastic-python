"""Tests for _SetUrlTransactionCoordinator."""

from typing import NoReturn
from unittest.mock import MagicMock

import pytest

from meshtastic.node_runtime.seturl_runtime import (
    _SetUrlParsedInput,
    _SetUrlTransactionCoordinator,
)
from meshtastic.protobuf import apponly_pb2


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
        assert coordinator._rollback_engine is not None

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
