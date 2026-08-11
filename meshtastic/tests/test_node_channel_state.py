"""Behavioral tests for the owned Node channel-state boundary."""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, cast
from unittest.mock import MagicMock, patch

import pytest

from meshtastic.node import Node
from meshtastic.node_runtime.channel_state import _NodeChannelState
from meshtastic.node_runtime.shared import MAX_CHANNELS
from meshtastic.protobuf import channel_pb2


def _channel(index: int, name: str = "") -> channel_pb2.Channel:
    """Build a channel fixture value."""
    channel = channel_pb2.Channel(
        index=index,
        role=(
            channel_pb2.Channel.Role.PRIMARY
            if index == 0
            else channel_pb2.Channel.Role.SECONDARY
        ),
    )
    channel.settings.name = name
    return channel


@pytest.mark.unit
def test_snapshot_channels_copies_while_owner_lock_is_held() -> None:
    """Snapshots should be detached and synchronized by the state owner."""
    state = _NodeChannelState()
    original = _channel(0, "primary")
    state.channels = [original]
    lock = MagicMock()
    lock_active = False

    def _enter() -> None:
        nonlocal lock_active
        lock_active = True

    def _exit(*_args: object) -> None:
        nonlocal lock_active
        lock_active = False

    lock.__enter__ = MagicMock(side_effect=_enter)
    lock.__exit__ = MagicMock(side_effect=_exit)
    state._replace_lock(lock)  # noqa: SLF001
    original_copy = state._copy_channel  # noqa: SLF001

    def _copy_while_locked(
        channel: channel_pb2.Channel,
    ) -> channel_pb2.Channel:
        assert lock_active
        return original_copy(channel)

    state._copy_channel = _copy_while_locked  # type: ignore[method-assign]  # noqa: SLF001

    snapshot = state.snapshot_channels()

    lock.__enter__.assert_called_once()
    lock.__exit__.assert_called_once()
    assert snapshot[0] is not original
    assert snapshot[0] == original


@pytest.mark.unit
def test_copy_lookup_is_detached_before_owner_lock_releases() -> None:
    """Copy lookups should perform protobuf copying inside the lock boundary."""
    state = _NodeChannelState()
    original = _channel(0, "primary")
    state.channels = [original]
    events: list[str] = []

    class _LockProbe:
        def __enter__(self) -> None:
            events.append("enter")

        def __exit__(self, *_args: object) -> None:
            events.append("exit")

    state._replace_lock(_LockProbe())  # noqa: SLF001
    original_copy = state._copy_channel  # noqa: SLF001

    def _copy_with_probe(channel: channel_pb2.Channel) -> channel_pb2.Channel:
        assert events == ["enter"]
        events.append("copy")
        return original_copy(channel)

    state._copy_channel = _copy_with_probe  # type: ignore[method-assign]  # noqa: SLF001

    copied = state.get_copy_by_index(0)

    assert copied is not original
    assert events == ["enter", "copy", "exit"]


@pytest.mark.unit
def test_replace_with_copies_normalizes_without_mutating_inputs() -> None:
    """Replacing state should copy inputs, reindex them, and fill disabled slots."""
    first = _channel(7, "primary")
    second = _channel(9, "secondary")
    state = _NodeChannelState()

    state.replace_with_copies([first, second])

    assert state.channels is not None
    assert len(state.channels) == MAX_CHANNELS
    assert state.channels[0] is not first
    assert state.channels[1] is not second
    assert [channel.index for channel in state.channels] == list(range(MAX_CHANNELS))
    assert first.index == 7
    assert second.index == 9
    assert all(
        channel.role == channel_pb2.Channel.Role.DISABLED
        for channel in state.channels[2:]
    )


@pytest.mark.unit
def test_install_partial_channels_normalizes_complete_cache() -> None:
    """A completed channel download should install and normalize staged responses."""
    state = _NodeChannelState()
    response = _channel(7, "downloaded")

    assert state.append_partial_if_new(response) is True
    assert state.append_partial_if_new(response) is False
    state.install_partial_channels()

    assert state.channels is not None
    assert len(state.channels) == MAX_CHANNELS
    assert state.channels[0] is response
    assert state.channels[0].index == 0
    assert state.channels[0].settings.name == "downloaded"


@pytest.mark.unit
def test_reset_for_download_clears_complete_and_partial_caches() -> None:
    """A fresh download should atomically clear all existing channel state."""
    state = _NodeChannelState()
    state.channels = [_channel(0)]
    state.partial_channels = [_channel(1)]

    assert state.has_channels() is True

    state.reset_for_download()

    assert state.has_channels() is False
    assert state.channels is None
    assert state.partial_channels == []


@pytest.mark.unit
def test_normalization_operations_are_safe_without_cached_channels() -> None:
    """Normalization entrypoints should be harmless before channels are loaded."""
    state = _NodeChannelState()

    state.normalize()
    state.fill()

    assert state.channels is None


@pytest.mark.unit
def test_node_legacy_channel_attributes_share_one_state_owner() -> None:
    """Historical Node attributes should remain live views over the owned state."""
    node = cast(Any, object.__new__(Node))
    channels = [_channel(0)]
    partial = [_channel(1)]

    node.channels = channels
    node.partialChannels = partial

    state = node._get_channel_state()
    assert node.channels is channels
    assert node.partialChannels is partial
    assert state.channels is channels
    assert state.partial_channels is partial


@pytest.mark.unit
def test_node_legacy_lock_assignment_replaces_state_owner_lock() -> None:
    """Historical lock instrumentation should still target the state-owner lock."""
    node = cast(Any, object.__new__(Node))
    replacement = MagicMock()

    node._channels_lock = replacement

    assert node._channels_lock is replacement
    assert node._get_channel_state().lock is replacement


@pytest.mark.unit
def test_node_channel_state_bootstrap_migrates_raw_legacy_fields() -> None:
    """Raw state restored from the legacy representation should migrate intact."""
    node = cast(Any, object.__new__(Node))
    channels = [_channel(0, "primary")]
    partial = [_channel(1, "secondary")]
    legacy_lock = MagicMock()
    node.__dict__.update(
        channels=channels,
        partialChannels=partial,
        _channels_lock=legacy_lock,
    )

    state = node._get_channel_state()

    assert state.channels is channels
    assert state.partial_channels is partial
    assert state.lock is legacy_lock
    assert node._channel_state is state
    assert not {"channels", "partialChannels", "_channels_lock"} & node.__dict__.keys()


@pytest.mark.unit
def test_node_channel_state_bootstrap_is_singleton_under_concurrency() -> None:
    """Concurrent bootstrap calls should publish exactly one channel-state owner."""
    node = cast(Any, object.__new__(Node))
    original_state_type = _NodeChannelState
    creation_count = 0
    creation_count_lock = threading.Lock()
    start_barrier = threading.Barrier(8, timeout=10)

    def _create_state() -> _NodeChannelState:
        nonlocal creation_count
        with creation_count_lock:
            creation_count += 1
        time.sleep(0.01)
        return original_state_type()

    def _get_state() -> _NodeChannelState:
        start_barrier.wait()
        return cast(_NodeChannelState, node._get_channel_state())

    def _get_state_for_worker(_worker_index: int) -> _NodeChannelState:
        return _get_state()

    with (
        patch("meshtastic.node._NodeChannelState", side_effect=_create_state),
        ThreadPoolExecutor(max_workers=8) as executor,
    ):
        states = list(executor.map(_get_state_for_worker, range(8)))

    assert creation_count == 1
    assert all(state is states[0] for state in states)
