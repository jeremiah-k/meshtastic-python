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
from meshtastic.protobuf import apponly_pb2, channel_pb2, mesh_pb2
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
        self,
        execution_engine: _SetUrlExecutionEngine,
        mock_local_node: MagicMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """execute_add_only with LoRa update sends admin message."""
        sleep_calls: list[float] = []
        monkeypatch.setattr(
            "meshtastic.node_runtime.seturl.execution.time.sleep",
            lambda seconds: sleep_calls.append(seconds),
        )
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
        assert 2.0 in sleep_calls

    @pytest.mark.unit
    def test_execute_add_only_with_lora_update_skipped_send_raises(
        self,
        execution_engine: _SetUrlExecutionEngine,
        mock_local_node: MagicMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """execute_add_only should abort when LoRa send is not started."""
        sleep_calls: list[float] = []
        monkeypatch.setattr(
            "meshtastic.node_runtime.seturl.execution.time.sleep",
            lambda seconds: sleep_calls.append(seconds),
        )
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
        assert 2.0 not in sleep_calls

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
            staged_channels=[staged],
            staged_channels_by_index={0: staged},
            deferred_new_named_admin_channel=None,
            deferred_new_named_admin_index=None,
            deferred_previous_admin_slot_channel=None,
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
            staged_channels=[staged],
            staged_channels_by_index={0: staged},
            deferred_new_named_admin_channel=None,
            deferred_new_named_admin_index=None,
            deferred_previous_admin_slot_channel=None,
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
            staged_channels=[staged],
            staged_channels_by_index={0: staged},
            deferred_new_named_admin_channel=None,
            deferred_new_named_admin_index=None,
            deferred_previous_admin_slot_channel=None,
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
    def test_execute_replace_all_skip_channel_indices(
        self,
        execution_engine: _SetUrlExecutionEngine,
        mock_local_node: MagicMock,
    ) -> None:
        """execute_replace_all skips channels in skip_channel_indices."""
        mock_local_node.channels = [
            _make_channel(0, channel_pb2.Channel.Role.PRIMARY, "already_done"),
            _make_channel(1, channel_pb2.Channel.Role.SECONDARY, "needs_write"),
            _make_channel(2, channel_pb2.Channel.Role.DISABLED),
        ]

        staged_0 = _make_channel(0, channel_pb2.Channel.Role.PRIMARY, "already_done")
        staged_1 = _make_channel(1, channel_pb2.Channel.Role.SECONDARY, "needs_write")
        staged_disabled = channel_pb2.Channel(
            index=2, role=channel_pb2.Channel.Role.DISABLED
        )

        channel_set = apponly_pb2.ChannelSet()
        settings_0 = channel_set.settings.add()
        settings_0.name = "already_done"
        settings_0.psk = b"\x01"
        settings_1 = channel_set.settings.add()
        settings_1.name = "needs_write"
        settings_1.psk = b"\x02"

        parsed_input = _SetUrlParsedInput(
            channel_set=channel_set,
            has_lora_update=False,
        )

        admin_context = MagicMock()
        admin_context.admin_index_for_write = 0
        admin_context.has_admin_write_node_named_admin = False

        plan = _SetUrlReplacePlan(
            max_channels=3,
            replace_original_channels_ref=mock_local_node.channels,
            replace_original_channels_fingerprint=_channels_fingerprint(
                mock_local_node.channels
            ),
            staged_channels=[staged_0, staged_1, staged_disabled],
            staged_channels_by_index={
                0: staged_0,
                1: staged_1,
                2: staged_disabled,
            },
            deferred_new_named_admin_channel=None,
            deferred_new_named_admin_index=None,
            deferred_previous_admin_slot_channel=None,
        )

        state = _SetUrlReplaceExecutionState()

        execution_engine.executeReplaceAll(
            parsed_input=parsed_input,
            admin_context=admin_context,
            plan=plan,
            state=state,
            skip_channel_indices={0},
        )

        written_indices = [
            call.args[0].index
            for call in mock_local_node._write_channel_snapshot.call_args_list
        ]
        assert 0 not in written_indices
        assert 1 in written_indices
        assert 2 in written_indices

    @pytest.mark.unit
    def test_execute_replace_all_skip_lora(
        self,
        execution_engine: _SetUrlExecutionEngine,
        mock_local_node: MagicMock,
    ) -> None:
        """execute_replace_all skips LoRa write when skip_lora=True."""
        mock_local_node.channels = [
            _make_channel(0, channel_pb2.Channel.Role.PRIMARY, "primary"),
        ]
        channel_set = _make_channel_set_with_lora("primary")
        parsed_input = _SetUrlParsedInput(
            channel_set=channel_set,
            has_lora_update=True,
        )
        admin_context = MagicMock()
        admin_context.admin_index_for_write = 0
        admin_context.has_admin_write_node_named_admin = False
        staged = _make_channel(0, channel_pb2.Channel.Role.PRIMARY, "primary")
        plan = _SetUrlReplacePlan(
            max_channels=1,
            replace_original_channels_ref=mock_local_node.channels,
            replace_original_channels_fingerprint=_channels_fingerprint(
                mock_local_node.channels
            ),
            staged_channels=[staged],
            staged_channels_by_index={0: staged},
            deferred_new_named_admin_channel=None,
            deferred_new_named_admin_index=None,
            deferred_previous_admin_slot_channel=None,
        )
        state = _SetUrlReplaceExecutionState()

        execution_engine.executeReplaceAll(
            parsed_input=parsed_input,
            admin_context=admin_context,
            plan=plan,
            state=state,
            skip_lora=True,
        )

        assert state.lora_write_started is False
        mock_local_node._send_admin.assert_not_called()

    @pytest.mark.unit
    def test_execute_replace_all_skip_set_skips_fingerprint_check(
        self,
        execution_engine: _SetUrlExecutionEngine,
        mock_local_node: MagicMock,
    ) -> None:
        """execute_replace_all with skip_channel_indices bypasses fingerprint pre-check."""
        original_channels = [
            _make_channel(0, channel_pb2.Channel.Role.PRIMARY, "old"),
        ]
        mock_local_node.channels = [
            _make_channel(0, channel_pb2.Channel.Role.PRIMARY, "changed"),
        ]

        staged = _make_channel(0, channel_pb2.Channel.Role.PRIMARY, "newprimary")
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

        plan = _SetUrlReplacePlan(
            max_channels=1,
            replace_original_channels_ref=original_channels,
            replace_original_channels_fingerprint=_channels_fingerprint(
                original_channels
            ),
            staged_channels=[staged],
            staged_channels_by_index={0: staged},
            deferred_new_named_admin_channel=None,
            deferred_new_named_admin_index=None,
            deferred_previous_admin_slot_channel=None,
        )

        state = _SetUrlReplaceExecutionState()
        execution_engine.executeReplaceAll(
            parsed_input=parsed_input,
            admin_context=admin_context,
            plan=plan,
            state=state,
            skip_channel_indices=set(),
        )

        mock_local_node._write_channel_snapshot.assert_called_once()
        assert 0 in state.written_channel_indices
