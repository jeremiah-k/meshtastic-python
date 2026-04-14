"""Tests for _SetUrlTransactionCoordinator."""

from typing import NoReturn
from unittest.mock import MagicMock

import pytest

from meshtastic.node_runtime.seturl_runtime import (
    _SetUrlParsedInput,
    _SetUrlTransactionCoordinator,
    _compute_remaining_channel_writes,
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
        """__init__ creates cache manager and execution engine (no rollback engine)."""
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

        coordinator._execution_engine.executeAddOnly = _failing_execute  # type: ignore[method-assign]

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

        coordinator._execution_engine.executeAddOnly = _failing_execute  # type: ignore[method-assign]

        with pytest.raises(RuntimeError, match="simulated write timeout"):
            coordinator._apply_add_only()

        assert mock_local_node.channels is None
        assert mock_local_node.localConfig.lora.hop_limit == 5

    @pytest.mark.unit
    def test_replace_all_resumes_and_finds_convergence(
        self, mock_local_node_with_reconnect: MagicMock
    ) -> None:
        """Replace-all resumes after failure and finds device already converged."""
        from meshtastic.tests.seturl.conftest import _make_channel

        desired_ch = _make_channel(
            0, channel_pb2.Channel.Role.PRIMARY, "desired", b"\x01"
        )
        mock_local_node_with_reconnect.channels = [desired_ch]

        channel_set = apponly_pb2.ChannelSet()
        settings = channel_set.settings.add()
        settings.name = "desired"
        settings.psk = b"\x01"

        parsed_input = _SetUrlParsedInput(
            channel_set=channel_set,
            has_lora_update=False,
        )

        coordinator = _SetUrlTransactionCoordinator(
            mock_local_node_with_reconnect, parsed_input=parsed_input
        )

        call_count = 0

        def _failing_then_check(
            *,
            parsed_input,
            admin_context,
            plan,
            state,
            skip_channel_indices=None,
            skip_lora=False,
        ):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                state.written_channel_indices.append(0)
                raise RuntimeError("simulated transport disconnect")

        coordinator._execution_engine.executeReplaceAll = _failing_then_check  # type: ignore[method-assign]

        def _restore_channels():
            mock_local_node_with_reconnect.channels = [desired_ch]

        mock_local_node_with_reconnect.iface.waitForConfig.side_effect = (
            _restore_channels
        )

        coordinator._apply_replace_all()
        assert call_count == 1

    @pytest.mark.unit
    def test_replace_all_resumes_partial_writes(
        self, mock_local_node_with_reconnect: MagicMock
    ) -> None:
        """Replace-all resumes and writes only remaining channels."""
        from meshtastic.tests.seturl.conftest import _make_channel

        desired_0 = _make_channel(0, channel_pb2.Channel.Role.PRIMARY, "ch0", b"\x01")

        mock_local_node_with_reconnect.channels = [
            desired_0,
            _make_channel(1, channel_pb2.Channel.Role.DISABLED),
        ]

        channel_set = apponly_pb2.ChannelSet()
        settings_0 = channel_set.settings.add()
        settings_0.name = "ch0"
        settings_0.psk = b"\x01"
        settings_1 = channel_set.settings.add()
        settings_1.name = "ch1"
        settings_1.psk = b"\x02"

        parsed_input = _SetUrlParsedInput(
            channel_set=channel_set,
            has_lora_update=False,
        )

        coordinator = _SetUrlTransactionCoordinator(
            mock_local_node_with_reconnect, parsed_input=parsed_input
        )

        call_count = 0

        def _first_fails_second_succeeds(
            *,
            parsed_input,
            admin_context,
            plan,
            state,
            skip_channel_indices=None,
            skip_lora=False,
        ):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                state.written_channel_indices.append(0)
                raise RuntimeError("simulated disconnect after channel 0")
            assert skip_channel_indices is not None
            assert 0 in skip_channel_indices
            assert 1 not in skip_channel_indices

        coordinator._execution_engine.executeReplaceAll = _first_fails_second_succeeds  # type: ignore[method-assign]

        def _restore_channels():
            mock_local_node_with_reconnect.channels = [
                desired_0,
                _make_channel(1, channel_pb2.Channel.Role.DISABLED),
            ]

        mock_local_node_with_reconnect.iface.waitForConfig.side_effect = (
            _restore_channels
        )

        coordinator._apply_replace_all()
        assert call_count == 2

    @pytest.mark.unit
    def test_replace_all_resumes_when_no_channels_converged_yet(
        self, mock_local_node_with_reconnect: MagicMock
    ) -> None:
        """Resume retry should pass an explicit empty skip set when nothing converged."""
        from meshtastic.tests.seturl.conftest import _make_channel

        old_ch = _make_channel(0, channel_pb2.Channel.Role.PRIMARY, "old", b"\x02")
        mock_local_node_with_reconnect.channels = [old_ch]

        channel_set = apponly_pb2.ChannelSet()
        settings = channel_set.settings.add()
        settings.name = "new"
        settings.psk = b"\x01"

        parsed_input = _SetUrlParsedInput(
            channel_set=channel_set,
            has_lora_update=False,
        )

        coordinator = _SetUrlTransactionCoordinator(
            mock_local_node_with_reconnect, parsed_input=parsed_input
        )

        call_count = 0
        observed_skip_sets: list[set[int] | None] = []

        def _first_fails_second_succeeds(
            *,
            parsed_input,
            admin_context,
            plan,
            state,
            skip_channel_indices=None,
            skip_lora=False,
        ):
            nonlocal call_count
            call_count += 1
            observed_skip_sets.append(skip_channel_indices)
            if call_count == 1:
                raise RuntimeError("simulated disconnect before writes")
            assert skip_channel_indices == set()

        coordinator._execution_engine.executeReplaceAll = _first_fails_second_succeeds  # type: ignore[method-assign]

        def _restore_channels() -> None:
            mock_local_node_with_reconnect.channels = [old_ch]

        mock_local_node_with_reconnect.iface.waitForConfig.side_effect = (
            _restore_channels
        )

        coordinator._apply_replace_all()
        assert call_count == 2
        assert observed_skip_sets[0] is None
        assert observed_skip_sets[1] == set()

    @pytest.mark.unit
    def test_replace_all_exhausted_retries_raises(
        self, mock_local_node_with_reconnect: MagicMock
    ) -> None:
        """Replace-all raises after exhausting all resume attempts."""
        from meshtastic.tests.seturl.conftest import _make_channel

        mock_local_node_with_reconnect.channels = [
            _make_channel(0, channel_pb2.Channel.Role.PRIMARY, "old"),
        ]

        channel_set = apponly_pb2.ChannelSet()
        settings = channel_set.settings.add()
        settings.name = "new"
        settings.psk = b"\x01"

        parsed_input = _SetUrlParsedInput(
            channel_set=channel_set,
            has_lora_update=False,
        )

        coordinator = _SetUrlTransactionCoordinator(
            mock_local_node_with_reconnect, parsed_input=parsed_input
        )

        def _always_fail(
            *,
            parsed_input,
            admin_context,
            plan,
            state,
            skip_channel_indices=None,
            skip_lora=False,
        ):
            state.written_channel_indices = []
            raise RuntimeError("persistent failure")

        coordinator._execution_engine.executeReplaceAll = _always_fail  # type: ignore[method-assign]

        with pytest.raises(RuntimeError, match="persistent failure"):
            coordinator._apply_replace_all()

    @pytest.mark.unit
    def test_replace_all_reconnect_failure_raises_original(
        self, mock_local_node_with_reconnect: MagicMock
    ) -> None:
        """Replace-all raises original error when reconnect fails."""
        from meshtastic.tests.seturl.conftest import _make_channel

        mock_local_node_with_reconnect.channels = [
            _make_channel(0, channel_pb2.Channel.Role.PRIMARY, "old"),
        ]
        mock_iface = mock_local_node_with_reconnect.iface
        mock_iface.isConnected.clear()

        channel_set = apponly_pb2.ChannelSet()
        settings = channel_set.settings.add()
        settings.name = "new"
        settings.psk = b"\x01"

        parsed_input = _SetUrlParsedInput(
            channel_set=channel_set,
            has_lora_update=False,
        )

        coordinator = _SetUrlTransactionCoordinator(
            mock_local_node_with_reconnect, parsed_input=parsed_input
        )

        def _fail_once(
            *,
            parsed_input,
            admin_context,
            plan,
            state,
            skip_channel_indices=None,
            skip_lora=False,
        ):
            raise RuntimeError("transport disconnect")

        coordinator._execution_engine.executeReplaceAll = _fail_once  # type: ignore[method-assign]

        with pytest.raises(RuntimeError, match="transport disconnect"):
            coordinator._apply_replace_all()

    @pytest.mark.unit
    def test_compute_remaining_channel_writes_detects_differences(self) -> None:
        """_compute_remaining_channel_writes returns indices that differ."""
        from meshtastic.tests.seturl.conftest import _make_channel

        actual = [
            _make_channel(0, channel_pb2.Channel.Role.PRIMARY, "same", b"\x01"),
            _make_channel(1, channel_pb2.Channel.Role.SECONDARY, "old", b"\x02"),
            _make_channel(2, channel_pb2.Channel.Role.DISABLED),
        ]
        desired_by_index = {
            0: _make_channel(0, channel_pb2.Channel.Role.PRIMARY, "same", b"\x01"),
            1: _make_channel(1, channel_pb2.Channel.Role.SECONDARY, "new", b"\x03"),
            2: channel_pb2.Channel(index=2, role=channel_pb2.Channel.Role.DISABLED),
        }
        remaining = _compute_remaining_channel_writes(actual, desired_by_index)
        assert remaining == {1}
