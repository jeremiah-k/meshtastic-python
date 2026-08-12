"""Tests for invocation-owned CLI resource cleanup and transfer semantics."""

from __future__ import annotations

import contextlib
from unittest.mock import MagicMock, create_autospec

import pytest

from meshtastic.cli.context import CliContext
from meshtastic.cli.session_resources import (
    CliSessionResources,
    get_current_session_resources,
)
from meshtastic.mesh_interface import MeshInterface


@pytest.mark.unit
def test_session_cleanup_runs_once_and_context_resets() -> None:
    """Registered cleanup should run once and invocation context must not leak."""
    cleanup = MagicMock()
    assert get_current_session_resources() is None

    with contextlib.ExitStack() as stack:
        session = CliSessionResources(stack)
        session.activate()
        lease = session.register_cleanup(cleanup)
        assert get_current_session_resources() is session
        lease.run()
        lease.run()

    cleanup.assert_called_once_with()
    assert get_current_session_resources() is None


@pytest.mark.unit
def test_cli_context_transfers_failure_cleanup_to_active_session() -> None:
    """Connected resources should move from rollback to invocation ownership."""
    cleanup = MagicMock()
    with contextlib.ExitStack() as stack:
        session = CliSessionResources(stack)
        session.activate()
        context = CliContext(
            interface=create_autospec(MeshInterface, instance=True),
            args=MagicMock(),
            get_node_kwargs={},
        )
        context.outcome.failure_cleanup_callbacks.append(cleanup)

        lease = context.retain_failure_cleanup(cleanup)

        assert lease is not None
        assert context.outcome.failure_cleanup_callbacks == []
        cleanup.assert_not_called()

    cleanup.assert_called_once_with()


@pytest.mark.unit
def test_cli_context_without_session_preserves_failure_cleanup() -> None:
    """Direct connected-action callers should retain historical rollback behavior."""
    cleanup = MagicMock()
    context = CliContext(
        interface=create_autospec(MeshInterface, instance=True),
        args=MagicMock(),
        get_node_kwargs={},
        session_resources=None,
    )
    context.outcome.failure_cleanup_callbacks.append(cleanup)

    assert context.retain_failure_cleanup(cleanup) is None
    assert context.outcome.failure_cleanup_callbacks == [cleanup]
