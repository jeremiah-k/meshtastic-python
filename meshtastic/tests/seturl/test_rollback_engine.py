"""Tests for _SetUrlRollbackEngine."""

import logging
from unittest.mock import MagicMock

import pytest

from meshtastic.node_runtime.seturl_runtime import (
    _channels_fingerprint,
    _SetUrlAddOnlyExecutionState,
    _SetUrlAddOnlyPlan,
    _SetUrlReplaceExecutionState,
    _SetUrlReplacePlan,
    _SetUrlRollbackEngine,
)
from meshtastic.protobuf import admin_pb2, channel_pb2, config_pb2
from meshtastic.tests.seturl.conftest import _make_channel


class _WriteFailure(RuntimeError):
    """Intentional write failure sentinel."""

    def __init__(self) -> None:
        super().__init__("Write failed")


class _LoRaRollbackFailure(RuntimeError):
    """Intentional LoRa rollback failure sentinel."""

    def __init__(self) -> None:
        super().__init__("LoRa rollback failed")


class TestSetUrlRollbackEngine:
    """Tests for _SetUrlRollbackEngine."""

    @pytest.mark.unit
    def test_rollback_add_only_with_channel_failure_attempts_next_admin_index(
        self,
        rollback_engine: _SetUrlRollbackEngine,
        mock_local_node: MagicMock,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """_rollback_add_only with channel failure attempts next admin index."""
        original_channel = _make_channel(1, channel_pb2.Channel.Role.DISABLED)
        mock_local_node.channels = [
            _make_channel(0, channel_pb2.Channel.Role.PRIMARY, "primary"),
            original_channel,
        ]

        plan = _SetUrlAddOnlyPlan(
            ignored_channel_names=[],
            channels_to_write=[],
            deferred_add_only_admin_channel=None,
            deferred_add_only_admin_index=2,
            original_channels_ref=[],
            original_channels_fingerprint=(),
            original_channels_by_index={1: original_channel},
            original_lora_config=None,
        )

        state = _SetUrlAddOnlyExecutionState(
            written_indices=[1], lora_write_started=False
        )

        admin_context = MagicMock()
        admin_context.admin_index_for_write = 0

        failed_admin_indexes: list[int] = []

        def write_side_effect(
            _channel: channel_pb2.Channel, *, adminIndex: int
        ) -> None:
            if adminIndex == 0:
                failed_admin_indexes.append(adminIndex)
                raise _WriteFailure()

        mock_local_node._write_channel_snapshot.side_effect = write_side_effect

        with caplog.at_level(logging.WARNING):
            rollback_engine._rollback_add_only(
                admin_context=admin_context,
                plan=plan,
                state=state,
            )

        assert failed_admin_indexes == [0]
        call_args_list = mock_local_node._write_channel_snapshot.call_args_list
        admin_indexes_called = [
            call.kwargs.get("adminIndex") for call in call_args_list
        ]
        assert 0 in admin_indexes_called
        assert 2 in admin_indexes_called

    @pytest.mark.unit
    def test_rollback_add_only_with_lora_failure_clears_cache_with_warning(
        self,
        rollback_engine: _SetUrlRollbackEngine,
        mock_local_node: MagicMock,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """_rollback_add_only with LoRa failure clears cache with warning (lines 712-727)."""
        original_channel = _make_channel(1, channel_pb2.Channel.Role.DISABLED)
        mock_local_node.channels = [
            _make_channel(0, channel_pb2.Channel.Role.PRIMARY, "primary"),
            original_channel,
        ]

        original_lora = config_pb2.Config.LoRaConfig()
        original_lora.hop_limit = 3

        plan = _SetUrlAddOnlyPlan(
            ignored_channel_names=[],
            channels_to_write=[],
            deferred_add_only_admin_channel=None,
            deferred_add_only_admin_index=None,
            original_channels_ref=[],
            original_channels_fingerprint=(),
            original_channels_by_index={1: original_channel},
            original_lora_config=original_lora,
        )

        state = _SetUrlAddOnlyExecutionState(
            written_indices=[1], lora_write_started=True
        )

        admin_context = MagicMock()
        admin_context.admin_index_for_write = 0

        def write_side_effect(
            _channel: channel_pb2.Channel, *, adminIndex: int | None = None
        ) -> None:
            _ = adminIndex
            pass

        def send_admin_side_effect(
            _msg: admin_pb2.AdminMessage, *, adminIndex: int | None = None
        ) -> None:
            _ = adminIndex
            raise _LoRaRollbackFailure()

        mock_local_node._write_channel_snapshot.side_effect = write_side_effect
        mock_local_node._send_admin.side_effect = send_admin_side_effect

        with caplog.at_level(logging.WARNING):
            rollback_engine._rollback_add_only(
                admin_context=admin_context,
                plan=plan,
                state=state,
            )

        assert "Rollback of LoRa config failed" in caplog.text
        assert "LoRa config cache cleared" in caplog.text

    @pytest.mark.unit
    def test_rollback_add_only_skips_snapshot_restore_when_send_not_started(
        self,
        rollback_engine: _SetUrlRollbackEngine,
        mock_local_node: MagicMock,
    ) -> None:
        """_rollback_add_only should not restore LoRa snapshot when rollback send is skipped."""
        original_channel = _make_channel(1, channel_pb2.Channel.Role.DISABLED)
        mock_local_node.channels = [
            _make_channel(0, channel_pb2.Channel.Role.PRIMARY, "primary"),
            original_channel,
        ]
        original_lora = config_pb2.Config.LoRaConfig()
        original_lora.hop_limit = 3
        plan = _SetUrlAddOnlyPlan(
            ignored_channel_names=[],
            channels_to_write=[],
            deferred_add_only_admin_channel=None,
            deferred_add_only_admin_index=None,
            original_channels_ref=[],
            original_channels_fingerprint=(),
            original_channels_by_index={1: original_channel},
            original_lora_config=original_lora,
        )
        state = _SetUrlAddOnlyExecutionState(
            written_indices=[],
            lora_write_started=True,
        )
        admin_context = MagicMock()
        admin_context.admin_index_for_write = 0
        mock_local_node._send_admin.return_value = None
        rollback_engine._cache_manager.restore_lora_snapshot = MagicMock()  # type: ignore[method-assign]

        rollback_engine._rollback_add_only(
            admin_context=admin_context,
            plan=plan,
            state=state,
        )

        rollback_engine._cache_manager.restore_lora_snapshot.assert_not_called()

    @pytest.mark.unit
    def test_rollback_replace_all_with_channel_failure_logs_warning(
        self,
        rollback_engine: _SetUrlRollbackEngine,
        mock_local_node: MagicMock,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """_rollback_replace_all with channel failure logs warning (lines 773-790)."""
        original_channel = _make_channel(
            0, channel_pb2.Channel.Role.PRIMARY, "original"
        )
        mock_local_node.channels = [original_channel]

        plan = _SetUrlReplacePlan(
            max_channels=1,
            replace_original_channels_ref=mock_local_node.channels,
            replace_original_channels_fingerprint=_channels_fingerprint(
                mock_local_node.channels
            ),
            replace_original_channels_snapshot=[original_channel],
            replace_original_channels_by_index={0: original_channel},
            staged_channels=[],
            staged_channels_by_index={},
            deferred_new_named_admin_channel=None,
            deferred_new_named_admin_index=None,
            deferred_previous_admin_slot_channel=None,
            replace_original_lora_config=None,
        )

        state = _SetUrlReplaceExecutionState(
            written_channel_indices=[0],
            lora_write_started=False,
            rollback_admin_indexes_for_write=[0],
        )

        admin_context = MagicMock()
        admin_context.admin_index_for_write = 0

        mock_local_node._write_channel_snapshot.side_effect = Exception(
            "Rollback failed"
        )

        with caplog.at_level(logging.WARNING):
            rollback_engine._rollback_replace_all(
                admin_context=admin_context,
                plan=plan,
                state=state,
            )

        assert "Rollback of channel index" in caplog.text
        assert "failed after replace-all" in caplog.text

    @pytest.mark.unit
    def test_rollback_replace_all_with_lora_failure_clears_cache_with_warning(
        self,
        rollback_engine: _SetUrlRollbackEngine,
        mock_local_node: MagicMock,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """_rollback_replace_all with LoRa failure clears cache with warning (lines 825-851)."""
        original_channel = _make_channel(
            0, channel_pb2.Channel.Role.PRIMARY, "original"
        )
        mock_local_node.channels = [original_channel]

        original_lora = config_pb2.Config.LoRaConfig()
        original_lora.hop_limit = 3

        plan = _SetUrlReplacePlan(
            max_channels=1,
            replace_original_channels_ref=mock_local_node.channels,
            replace_original_channels_fingerprint=_channels_fingerprint(
                mock_local_node.channels
            ),
            replace_original_channels_snapshot=[original_channel],
            replace_original_channels_by_index={0: original_channel},
            staged_channels=[],
            staged_channels_by_index={},
            deferred_new_named_admin_channel=None,
            deferred_new_named_admin_index=None,
            deferred_previous_admin_slot_channel=None,
            replace_original_lora_config=original_lora,
        )

        state = _SetUrlReplaceExecutionState(
            written_channel_indices=[],
            lora_write_started=True,
            rollback_admin_indexes_for_write=[0],
        )

        admin_context = MagicMock()
        admin_context.admin_index_for_write = 0

        mock_local_node._send_admin.side_effect = Exception("LoRa rollback failed")

        with caplog.at_level(logging.WARNING):
            rollback_engine._rollback_replace_all(
                admin_context=admin_context,
                plan=plan,
                state=state,
            )

        assert "Rollback of LoRa config failed" in caplog.text
        assert "LoRa config cache cleared after rollback failure" in caplog.text

    @pytest.mark.unit
    def test_rollback_replace_all_skips_snapshot_restore_when_send_not_started(
        self,
        rollback_engine: _SetUrlRollbackEngine,
        mock_local_node: MagicMock,
    ) -> None:
        """_rollback_replace_all should not restore LoRa snapshot when rollback send is skipped."""
        original_channel = _make_channel(
            0, channel_pb2.Channel.Role.PRIMARY, "original"
        )
        mock_local_node.channels = [original_channel]
        original_lora = config_pb2.Config.LoRaConfig()
        original_lora.hop_limit = 3
        plan = _SetUrlReplacePlan(
            max_channels=1,
            replace_original_channels_ref=mock_local_node.channels,
            replace_original_channels_fingerprint=_channels_fingerprint(
                mock_local_node.channels
            ),
            replace_original_channels_snapshot=[original_channel],
            replace_original_channels_by_index={0: original_channel},
            staged_channels=[],
            staged_channels_by_index={},
            deferred_new_named_admin_channel=None,
            deferred_new_named_admin_index=None,
            deferred_previous_admin_slot_channel=None,
            replace_original_lora_config=original_lora,
        )
        state = _SetUrlReplaceExecutionState(
            written_channel_indices=[],
            lora_write_started=True,
            rollback_admin_indexes_for_write=[0],
        )
        admin_context = MagicMock()
        admin_context.admin_index_for_write = 0
        mock_local_node._send_admin.return_value = None
        rollback_engine._cache_manager.restore_lora_snapshot = MagicMock()  # type: ignore[method-assign]

        rollback_engine._rollback_replace_all(
            admin_context=admin_context,
            plan=plan,
            state=state,
        )

        rollback_engine._cache_manager.restore_lora_snapshot.assert_not_called()

    @pytest.mark.unit
    def test_rollback_replace_all_without_snapshot_clears_cache(
        self,
        rollback_engine: _SetUrlRollbackEngine,
        mock_local_node: MagicMock,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """_rollback_replace_all without snapshot clears cache (lines 844-847)."""
        original_channel = _make_channel(
            0, channel_pb2.Channel.Role.PRIMARY, "original"
        )
        mock_local_node.channels = [original_channel]

        plan = _SetUrlReplacePlan(
            max_channels=1,
            replace_original_channels_ref=mock_local_node.channels,
            replace_original_channels_fingerprint=_channels_fingerprint(
                mock_local_node.channels
            ),
            replace_original_channels_snapshot=[original_channel],
            replace_original_channels_by_index={0: original_channel},
            staged_channels=[],
            staged_channels_by_index={},
            deferred_new_named_admin_channel=None,
            deferred_new_named_admin_index=None,
            deferred_previous_admin_slot_channel=None,
            replace_original_lora_config=None,
        )

        state = _SetUrlReplaceExecutionState(
            written_channel_indices=[],
            lora_write_started=True,
            rollback_admin_indexes_for_write=[0],
        )

        admin_context = MagicMock()
        admin_context.admin_index_for_write = 0

        with caplog.at_level(logging.WARNING):
            rollback_engine._rollback_replace_all(
                admin_context=admin_context,
                plan=plan,
                state=state,
            )

        assert (
            "LoRa config cache cleared after replace-all failure without rollback snapshot"
            in caplog.text
        )

    @pytest.mark.unit
    def test_rollback_replace_all_successful_restores_snapshot(
        self,
        rollback_engine: _SetUrlRollbackEngine,
        mock_local_node: MagicMock,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """_rollback_replace_all with successful rollback restores snapshot."""
        snapshot_channel = _make_channel(
            0, channel_pb2.Channel.Role.PRIMARY, "original"
        )
        live_channel = _make_channel(0, channel_pb2.Channel.Role.PRIMARY, "modified")
        mock_local_node.channels = [live_channel]

        plan = _SetUrlReplacePlan(
            max_channels=1,
            replace_original_channels_ref=mock_local_node.channels,
            replace_original_channels_fingerprint=_channels_fingerprint(
                mock_local_node.channels
            ),
            replace_original_channels_snapshot=[snapshot_channel],
            replace_original_channels_by_index={0: snapshot_channel},
            staged_channels=[],
            staged_channels_by_index={},
            deferred_new_named_admin_channel=None,
            deferred_new_named_admin_index=None,
            deferred_previous_admin_slot_channel=None,
            replace_original_lora_config=None,
        )

        state = _SetUrlReplaceExecutionState(
            written_channel_indices=[0],
            lora_write_started=False,
            rollback_admin_indexes_for_write=[0],
        )

        admin_context = MagicMock()
        admin_context.admin_index_for_write = 0

        with caplog.at_level(logging.WARNING):
            rollback_engine._rollback_replace_all(
                admin_context=admin_context,
                plan=plan,
                state=state,
            )

        assert len(mock_local_node.channels) == 1
        assert mock_local_node.channels[0].settings.name == "original"
