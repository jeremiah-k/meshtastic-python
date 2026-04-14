"""Tests for _SetUrlExecutionEngine."""

from unittest.mock import MagicMock

import pytest

from meshtastic.node_runtime.seturl_runtime import (
    _channels_fingerprint,
    _SetUrlAddOnlyExecutionState,
    _SetUrlAddOnlyPlan,
    _SetUrlExecutionEngine,
    _SetUrlParsedInput,
    _SetUrlReplaceExecutionState,
    _SetUrlReplacePlan,
)
from meshtastic.protobuf import apponly_pb2, channel_pb2, config_pb2, mesh_pb2
from meshtastic.tests.seturl.conftest import (
    _make_channel,
    _make_channel_set_with_lora,
    _make_raise_error_side_effect,
)


class TestSetUrlExecutionEngine:
    """Tests for _SetUrlExecutionEngine."""

    @pytest.mark.unit
    def test_execute_add_only_writes_channels(
        self, execution_engine: _SetUrlExecutionEngine, mock_local_node: MagicMock
    ) -> None:
        """execute_add_only writes channels to device."""
        channel_set = apponly_pb2.ChannelSet()
        settings = channel_set.settings.add()
        settings.name = "newchannel"
        settings.psk = b"\x01"

        parsed_input = _SetUrlParsedInput(
            channel_set=channel_set,
            has_lora_update=False,
        )

        admin_context = MagicMock()
        admin_context.admin_index_for_write = 0
        admin_context.has_admin_write_node_named_admin = False

        new_channel = _make_channel(1, channel_pb2.Channel.Role.SECONDARY, "newchannel")
        plan = _SetUrlAddOnlyPlan(
            ignored_channel_names=[],
            channels_to_write=[(new_channel, "newchannel")],
            deferred_add_only_admin_channel=None,
            deferred_add_only_admin_index=None,
            original_channels_ref=[],
            original_channels_fingerprint=(),
            original_channels_by_index={},
            original_lora_config=None,
        )

        state = _SetUrlAddOnlyExecutionState()

        execution_engine.executeAddOnly(
            parsed_input=parsed_input,
            admin_context=admin_context,
            plan=plan,
            state=state,
        )

        mock_local_node._write_channel_snapshot.assert_called_once()
        assert 1 in state.written_indices

    @pytest.mark.unit
    def test_execute_add_only_with_lora_update_sends_admin(
        self, execution_engine: _SetUrlExecutionEngine, mock_local_node: MagicMock
    ) -> None:
        """execute_add_only with LoRa update sends admin message."""
        channel_set = _make_channel_set_with_lora("test")

        parsed_input = _SetUrlParsedInput(
            channel_set=channel_set,
            has_lora_update=True,
        )

        admin_context = MagicMock()
        admin_context.admin_index_for_write = 0
        admin_context.has_admin_write_node_named_admin = False

        new_channel = _make_channel(1, channel_pb2.Channel.Role.SECONDARY, "test")
        plan = _SetUrlAddOnlyPlan(
            ignored_channel_names=[],
            channels_to_write=[(new_channel, "test")],
            deferred_add_only_admin_channel=None,
            deferred_add_only_admin_index=None,
            original_channels_ref=[],
            original_channels_fingerprint=(),
            original_channels_by_index={},
            original_lora_config=config_pb2.Config.LoRaConfig(),
        )

        state = _SetUrlAddOnlyExecutionState()

        execution_engine.executeAddOnly(
            parsed_input=parsed_input,
            admin_context=admin_context,
            plan=plan,
            state=state,
        )

        mock_local_node.ensureSessionKey.assert_called()
        mock_local_node._send_admin.assert_called()
        assert state.lora_write_started is True

    @pytest.mark.unit
    def test_execute_add_only_with_lora_update_skipped_send_raises(
        self, execution_engine: _SetUrlExecutionEngine, mock_local_node: MagicMock
    ) -> None:
        """execute_add_only should abort when LoRa send is not started."""
        channel_set = _make_channel_set_with_lora("test")
        parsed_input = _SetUrlParsedInput(
            channel_set=channel_set,
            has_lora_update=True,
        )
        admin_context = MagicMock()
        admin_context.admin_index_for_write = 0
        admin_context.has_admin_write_node_named_admin = False
        plan = _SetUrlAddOnlyPlan(
            ignored_channel_names=[],
            channels_to_write=[],
            deferred_add_only_admin_channel=None,
            deferred_add_only_admin_index=None,
            original_channels_ref=[],
            original_channels_fingerprint=(),
            original_channels_by_index={},
            original_lora_config=config_pb2.Config.LoRaConfig(),
        )
        state = _SetUrlAddOnlyExecutionState()
        mock_local_node._send_admin.return_value = None

        mock_local_node._raise_interface_error = MagicMock(
            side_effect=_make_raise_error_side_effect()
        )

        with pytest.raises(ValueError, match="LoRa config update was not started"):
            execution_engine.executeAddOnly(
                parsed_input=parsed_input,
                admin_context=admin_context,
                plan=plan,
                state=state,
            )
        assert state.lora_write_started is False

    @pytest.mark.unit
    def test_execute_replace_all_writes_channels(
        self,
        execution_engine: _SetUrlExecutionEngine,
        mock_local_node: MagicMock,
    ) -> None:
        """execute_replace_all writes staged channels."""
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
        admin_context.admin_index_for_write = 0
        admin_context.has_admin_write_node_named_admin = False

        staged = _make_channel(0, channel_pb2.Channel.Role.PRIMARY, "newprimary")
        plan = _SetUrlReplacePlan(
            max_channels=2,
            replace_original_channels_ref=mock_local_node.channels,
            replace_original_channels_fingerprint=_channels_fingerprint(
                mock_local_node.channels
            ),
            replace_original_channels_snapshot=[],
            replace_original_channels_by_index={},
            staged_channels=[staged],
            staged_channels_by_index={0: staged},
            deferred_new_named_admin_channel=None,
            deferred_new_named_admin_index=None,
            deferred_previous_admin_slot_channel=None,
            replace_original_lora_config=None,
        )

        state = _SetUrlReplaceExecutionState()

        execution_engine.executeReplaceAll(
            parsed_input=parsed_input,
            admin_context=admin_context,
            plan=plan,
            state=state,
        )

        mock_local_node._write_channel_snapshot.assert_called()
        assert 0 in state.written_channel_indices

    @pytest.mark.unit
    def test_execute_replace_all_with_lora_update_skipped_send_raises(
        self,
        execution_engine: _SetUrlExecutionEngine,
        mock_local_node: MagicMock,
    ) -> None:
        """execute_replace_all should abort when LoRa send is not started."""
        mock_local_node.channels = [
            _make_channel(0, channel_pb2.Channel.Role.PRIMARY, "old"),
        ]
        channel_set = _make_channel_set_with_lora("newprimary")
        parsed_input = _SetUrlParsedInput(
            channel_set=channel_set,
            has_lora_update=True,
        )
        admin_context = MagicMock()
        admin_context.admin_index_for_write = 0
        admin_context.has_admin_write_node_named_admin = False
        staged = _make_channel(0, channel_pb2.Channel.Role.PRIMARY, "newprimary")
        plan = _SetUrlReplacePlan(
            max_channels=1,
            replace_original_channels_ref=mock_local_node.channels,
            replace_original_channels_fingerprint=_channels_fingerprint(
                mock_local_node.channels
            ),
            replace_original_channels_snapshot=[],
            replace_original_channels_by_index={},
            staged_channels=[staged],
            staged_channels_by_index={0: staged},
            deferred_new_named_admin_channel=None,
            deferred_new_named_admin_index=None,
            deferred_previous_admin_slot_channel=None,
            replace_original_lora_config=None,
        )
        state = _SetUrlReplaceExecutionState()
        mock_local_node._send_admin.return_value = None

        mock_local_node._raise_interface_error = MagicMock(
            side_effect=_make_raise_error_side_effect()
        )

        with pytest.raises(ValueError, match="LoRa config update was not started"):
            execution_engine.executeReplaceAll(
                parsed_input=parsed_input,
                admin_context=admin_context,
                plan=plan,
                state=state,
            )
        assert state.lora_write_started is False

    @pytest.mark.unit
    def test_execute_replace_all_with_lora_update_success(
        self,
        execution_engine: _SetUrlExecutionEngine,
        mock_local_node: MagicMock,
    ) -> None:
        """execute_replace_all with LoRa update sends admin message and sets lora_write_started."""
        mock_local_node.channels = [
            _make_channel(0, channel_pb2.Channel.Role.PRIMARY, "old"),
        ]
        channel_set = _make_channel_set_with_lora("newprimary")
        parsed_input = _SetUrlParsedInput(
            channel_set=channel_set,
            has_lora_update=True,
        )
        admin_context = MagicMock()
        admin_context.admin_index_for_write = 0
        admin_context.has_admin_write_node_named_admin = False
        staged = _make_channel(0, channel_pb2.Channel.Role.PRIMARY, "newprimary")
        plan = _SetUrlReplacePlan(
            max_channels=1,
            replace_original_channels_ref=mock_local_node.channels,
            replace_original_channels_fingerprint=_channels_fingerprint(
                mock_local_node.channels
            ),
            replace_original_channels_snapshot=[],
            replace_original_channels_by_index={},
            staged_channels=[staged],
            staged_channels_by_index={0: staged},
            deferred_new_named_admin_channel=None,
            deferred_new_named_admin_index=None,
            deferred_previous_admin_slot_channel=None,
            replace_original_lora_config=config_pb2.Config.LoRaConfig(),
        )
        state = _SetUrlReplaceExecutionState()
        mock_local_node.ensureSessionKey = MagicMock()
        mock_local_node._send_admin = MagicMock(return_value=mesh_pb2.MeshPacket())

        execution_engine.executeReplaceAll(
            parsed_input=parsed_input,
            admin_context=admin_context,
            plan=plan,
            state=state,
        )

        mock_local_node.ensureSessionKey.assert_called()
        mock_local_node._send_admin.assert_called()
        assert state.lora_write_started is True

    @pytest.mark.unit
    def test_post_write_fallback_admin_index_returns_named_admin(self) -> None:
        """_post_write_fallback_admin_index returns named admin channel index."""
        staged_admin = _make_channel(1, channel_pb2.Channel.Role.SECONDARY, "admin")
        staged_primary = _make_channel(0, channel_pb2.Channel.Role.PRIMARY, "primary")

        plan = _SetUrlReplacePlan(
            max_channels=2,
            replace_original_channels_ref=[],
            replace_original_channels_fingerprint=(),
            replace_original_channels_snapshot=[],
            replace_original_channels_by_index={},
            staged_channels=[staged_primary, staged_admin],
            staged_channels_by_index={0: staged_primary, 1: staged_admin},
            deferred_new_named_admin_channel=None,
            deferred_new_named_admin_index=None,
            deferred_previous_admin_slot_channel=None,
            replace_original_lora_config=None,
        )

        result = _SetUrlExecutionEngine._post_write_fallback_admin_index(plan)

        assert result == 1

    @pytest.mark.unit
    def test_post_write_fallback_admin_index_defaults_to_zero(self) -> None:
        """_post_write_fallback_admin_index returns 0 when no named admin."""
        staged_primary = _make_channel(0, channel_pb2.Channel.Role.PRIMARY, "primary")

        plan = _SetUrlReplacePlan(
            max_channels=1,
            replace_original_channels_ref=[],
            replace_original_channels_fingerprint=(),
            replace_original_channels_snapshot=[],
            replace_original_channels_by_index={},
            staged_channels=[staged_primary],
            staged_channels_by_index={0: staged_primary},
            deferred_new_named_admin_channel=None,
            deferred_new_named_admin_index=None,
            deferred_previous_admin_slot_channel=None,
            replace_original_lora_config=None,
        )

        result = _SetUrlExecutionEngine._post_write_fallback_admin_index(plan)

        assert result == 0
