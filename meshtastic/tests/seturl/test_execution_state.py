"""Tests for execution state classes."""

import pytest

from meshtastic.node_runtime.seturl_runtime import (
    _SetUrlAddOnlyExecutionState,
    _SetUrlReplaceExecutionState,
)


class TestSetUrlAddOnlyExecutionState:
    """Tests for _SetUrlAddOnlyExecutionState."""

    @pytest.mark.unit
    def test_default_state(self) -> None:
        """Default state has empty written_indices and lora_write_started=False."""
        state = _SetUrlAddOnlyExecutionState()

        assert not state.written_indices
        assert state.lora_write_started is False

    @pytest.mark.unit
    def test_state_tracks_writes(self) -> None:
        """State tracks written indices."""
        state = _SetUrlAddOnlyExecutionState()
        state.written_indices.append(0)
        state.written_indices.append(1)

        assert state.written_indices == [0, 1]


class TestSetUrlReplaceExecutionState:
    """Tests for _SetUrlReplaceExecutionState."""

    @pytest.mark.unit
    def test_default_state(self) -> None:
        """Default state has empty tracking lists."""
        state = _SetUrlReplaceExecutionState()

        assert not state.written_channel_indices
        assert state.lora_write_started is False
        assert not state.rollback_admin_indexes_for_write

    @pytest.mark.unit
    def test_state_with_initial_rollback_indexes(self) -> None:
        """State can be initialized with rollback admin indexes."""
        state = _SetUrlReplaceExecutionState(rollback_admin_indexes_for_write=[0, 1])

        assert state.rollback_admin_indexes_for_write == [0, 1]
