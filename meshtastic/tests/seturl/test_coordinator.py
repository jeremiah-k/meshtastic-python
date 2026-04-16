"""Tests for _SetUrlTransactionCoordinator."""

from typing import NoReturn
from unittest.mock import MagicMock, patch

import pytest

from meshtastic.node_runtime.seturl.coordinator import (
    LORA_CONFIG_RECONNECT_SETTLE_SECONDS,
)
from meshtastic.node_runtime.seturl.execution import _ReplaceAllStage
from meshtastic.node_runtime.seturl.planner import _SetUrlReplacePlanner
from meshtastic.node_runtime.seturl_runtime import (
    _compute_remaining_channel_writes,
    _SetUrlParsedInput,
    _SetUrlTransactionCoordinator,
)
from meshtastic.protobuf import apponly_pb2, channel_pb2, localonly_pb2
from meshtastic.tests.seturl.conftest import _make_channel


def _make_reload_mock(node, channels):
    """Create a mock _bounded_config_reload that restores channels and returns True."""

    def _mock_reload(self_coord, iface, deadline):
        node.channels = channels
        return True

    return _mock_reload


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

        with (
            patch("meshtastic.node_runtime.seturl.coordinator.time.sleep"),
            patch.object(
                _SetUrlTransactionCoordinator,
                "_bounded_config_reload",
                _make_reload_mock(mock_local_node_with_reconnect, [desired_ch]),
            ),
        ):
            coordinator._apply_replace_all()
        assert call_count == 1

    @pytest.mark.unit
    def test_replace_all_resumes_partial_writes(
        self, mock_local_node_with_reconnect: MagicMock
    ) -> None:
        """Replace-all resumes and writes only remaining channels."""
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

        restored_channels = [
            desired_0,
            _make_channel(1, channel_pb2.Channel.Role.DISABLED),
        ]
        with (
            patch("meshtastic.node_runtime.seturl.coordinator.time.sleep"),
            patch.object(
                _SetUrlTransactionCoordinator,
                "_bounded_config_reload",
                _make_reload_mock(mock_local_node_with_reconnect, restored_channels),
            ),
        ):
            coordinator._apply_replace_all()
        assert call_count == 2

    @pytest.mark.unit
    def test_replace_all_resumes_when_no_channels_converged_yet(
        self, mock_local_node_with_reconnect: MagicMock
    ) -> None:
        """Resume retry should pass an explicit empty skip set when nothing converged."""
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

        with (
            patch("meshtastic.node_runtime.seturl.coordinator.time.sleep"),
            patch.object(
                _SetUrlTransactionCoordinator,
                "_bounded_config_reload",
                _make_reload_mock(mock_local_node_with_reconnect, [old_ch]),
            ),
        ):
            coordinator._apply_replace_all()
        assert call_count == 2
        assert observed_skip_sets[0] is None
        assert observed_skip_sets[1] == set()

    @pytest.mark.unit
    def test_replace_all_exhausted_retries_raises(
        self, mock_local_node_with_reconnect: MagicMock
    ) -> None:
        """Replace-all raises after exhausting all resume attempts."""
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

        # Simulate time passage so that config reload timeout is reached quickly
        monotonic_time = [0.0]

        def _mock_monotonic():
            return monotonic_time[0]

        def _mock_sleep(duration):
            monotonic_time[0] += duration

        with (
            patch(
                "meshtastic.node_runtime.seturl.coordinator.time.monotonic",
                side_effect=_mock_monotonic,
            ),
            patch(
                "meshtastic.node_runtime.seturl.coordinator.time.sleep",
                side_effect=_mock_sleep,
            ),
            pytest.raises(RuntimeError, match="persistent failure"),
        ):
            coordinator._apply_replace_all()

    @pytest.mark.unit
    def test_replace_all_reconnect_failure_raises_original(
        self, mock_local_node_with_reconnect: MagicMock
    ) -> None:
        """Replace-all raises original error when reconnect fails."""
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

        with (
            patch("meshtastic.node_runtime.seturl.coordinator.time.sleep"),
            pytest.raises(RuntimeError, match="transport disconnect"),
        ):
            coordinator._apply_replace_all()
        mock_iface.connect.assert_called()

    @pytest.mark.unit
    def test_lora_config_failure_settles_before_reconnect(
        self, mock_local_node_with_reconnect: MagicMock
    ) -> None:
        """LoRa-stage failure adds settle delay before reconnect attempt."""
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

        def _fail_at_lora(
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
                state.stage = _ReplaceAllStage.LORA_CONFIG
                raise RuntimeError("simulated LoRa-stage disconnect")

        coordinator._execution_engine.executeReplaceAll = _fail_at_lora  # type: ignore[method-assign]

        sleep_durations: list[float] = []

        def _track_sleep(duration):
            sleep_durations.append(duration)

        with (
            patch(
                "meshtastic.node_runtime.seturl.coordinator.time.sleep",
                side_effect=_track_sleep,
            ),
            patch.object(
                _SetUrlTransactionCoordinator,
                "_bounded_config_reload",
                _make_reload_mock(mock_local_node_with_reconnect, [desired_ch]),
            ),
        ):
            coordinator._apply_replace_all()

        assert LORA_CONFIG_RECONNECT_SETTLE_SECONDS in sleep_durations
        assert call_count == 1

    @pytest.mark.unit
    def test_reconnect_flap_during_stability_window_recovers(
        self, mock_local_node_with_reconnect: MagicMock
    ) -> None:
        """Replace-all catches a flapping reconnect during stability window and retries."""
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
        connect_call_count = 0
        sleep_call_count = 0
        iface = mock_local_node_with_reconnect.iface

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

        def _connect_with_recovery():
            nonlocal connect_call_count
            connect_call_count += 1
            if connect_call_count >= 1:
                iface.isConnected.set()

        iface.connect = MagicMock(side_effect=_connect_with_recovery)

        def _sleep_with_flap(duration):
            nonlocal sleep_call_count
            sleep_call_count += 1
            if sleep_call_count == 1:
                iface.isConnected.clear()

        with (
            patch(
                "meshtastic.node_runtime.seturl.coordinator.time.sleep",
                side_effect=_sleep_with_flap,
            ),
            patch.object(
                _SetUrlTransactionCoordinator,
                "_bounded_config_reload",
                _make_reload_mock(mock_local_node_with_reconnect, [desired_ch]),
            ),
        ):
            coordinator._apply_replace_all()

        assert call_count == 1
        assert connect_call_count >= 1

    @pytest.mark.unit
    def test_bounded_failure_transport_never_stabilizes(
        self, mock_local_node_with_reconnect: MagicMock
    ) -> None:
        """Replace-all fails when transport flaps repeatedly through all sub-attempts."""
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

        sleep_call_count = 0

        def _flap_sleep(duration):
            nonlocal sleep_call_count
            sleep_call_count += 1
            mock_local_node_with_reconnect.iface.isConnected.clear()

        with (
            patch(
                "meshtastic.node_runtime.seturl.coordinator.time.sleep",
                side_effect=_flap_sleep,
            ),
            pytest.raises(RuntimeError, match="persistent failure"),
        ):
            coordinator._apply_replace_all()

        assert sleep_call_count >= 1

    @pytest.mark.unit
    def test_config_reload_timeout_returns_none(
        self, mock_local_node_with_reconnect: MagicMock
    ) -> None:
        """_reconnect_and_compute_remaining returns None when config reload times out."""
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

        mock_local_node_with_reconnect.channels = [
            _make_channel(0, channel_pb2.Channel.Role.PRIMARY, "old"),
        ]
        plan = _SetUrlReplacePlanner(
            mock_local_node_with_reconnect,
            parsed_input=parsed_input,
            admin_context=coordinator._admin_context,
        ).build_plan()

        mock_local_node_with_reconnect.channels = None
        mock_local_node_with_reconnect.iface.isConnected.set()
        mock_local_node_with_reconnect.iface.myInfo = None

        monotonic_time = [0.0]

        def _mock_monotonic():
            return monotonic_time[0]

        def _mock_sleep(duration):
            monotonic_time[0] += duration

        with (
            patch(
                "meshtastic.node_runtime.seturl.coordinator.time.monotonic",
                side_effect=_mock_monotonic,
            ),
            patch(
                "meshtastic.node_runtime.seturl.coordinator.time.sleep",
                side_effect=_mock_sleep,
            ),
        ):
            result = coordinator._reconnect_and_compute_remaining(plan)

        assert result is None

    @pytest.mark.unit
    def test_reconnect_handles_missing_isconnected_event(
        self, mock_local_node_with_reconnect: MagicMock
    ) -> None:
        """Reconnect path should degrade safely when iface.isConnected is unavailable."""
        channel_set = apponly_pb2.ChannelSet()
        settings = channel_set.settings.add()
        settings.name = "desired"
        settings.psk = b"\x01"

        parsed_input = _SetUrlParsedInput(
            channel_set=channel_set,
            has_lora_update=False,
        )
        coordinator = _SetUrlTransactionCoordinator(
            mock_local_node_with_reconnect,
            parsed_input=parsed_input,
        )

        mock_local_node_with_reconnect.channels = [
            _make_channel(0, channel_pb2.Channel.Role.PRIMARY, "old"),
        ]
        plan = _SetUrlReplacePlanner(
            mock_local_node_with_reconnect,
            parsed_input=parsed_input,
            admin_context=coordinator._admin_context,
        ).build_plan()

        mock_local_node_with_reconnect.iface.isConnected = None
        result = coordinator._reconnect_and_compute_remaining(plan)
        assert result is None

    @pytest.mark.unit
    def test_compute_remaining_channel_writes_detects_differences(self) -> None:
        """_compute_remaining_channel_writes returns indices that differ."""
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
