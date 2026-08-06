"""Focused tests for connected CLI dispatch lifecycle ownership."""

from __future__ import annotations

import argparse
from types import SimpleNamespace
from typing import cast
from unittest.mock import MagicMock

import pytest

from meshtastic.cli import dispatch
from meshtastic.cli.context import ActionOutcome, CliContext
from meshtastic.mesh_interface import MeshInterface


def _context() -> CliContext:
    """Build a minimal connected CLI context for lifecycle helper tests."""
    args = argparse.Namespace(
        ack=False,
        dest="^all",
        export_config=False,
        seriallog=False,
        wait_to_disconnect=None,
    )
    interface = SimpleNamespace()
    return CliContext(
        interface=cast(MeshInterface, interface),
        args=args,
        get_node_kwargs={},
        outcome=ActionOutcome(),
    )


@pytest.mark.unit
def test_cleanup_connected_resources_runs_in_reverse_order() -> None:
    """Retained resources should unwind in reverse registration order exactly once."""
    context = _context()
    calls: list[str] = []
    context.outcome.cleanup_callbacks.extend(
        [lambda: calls.append("first"), lambda: calls.append("second")]
    )

    error = dispatch._cleanup_connected_resources(context)  # noqa: SLF001

    assert error is None
    assert calls == ["second", "first"]
    assert context.outcome.cleanup_callbacks == []


@pytest.mark.unit
def test_cleanup_connected_resources_returns_first_failure(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Cleanup should continue after failures while retaining the first exception."""
    context = _context()
    first = RuntimeError("first")
    second = ValueError("second")

    def _raise(exc: BaseException) -> None:
        raise exc

    context.outcome.cleanup_callbacks.extend(
        [lambda: _raise(second), lambda: _raise(first)]
    )

    error = dispatch._cleanup_connected_resources(context)  # noqa: SLF001

    assert error is first
    assert "Additional connected-action cleanup failed" in caplog.text
    assert context.outcome.cleanup_callbacks == []


@pytest.mark.unit
def test_dispatch_does_not_suppress_cleanup_base_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A control-flow BaseException raised during cleanup must remain observable."""
    context = _context()
    context.outcome.cleanup_callbacks.append(MagicMock(side_effect=KeyboardInterrupt))
    monkeypatch.setattr(
        dispatch,
        "_print_connection",
        MagicMock(side_effect=ValueError("primary")),
    )

    with pytest.raises(KeyboardInterrupt) as exc_info:
        dispatch.dispatch_connected(context, MagicMock())

    assert isinstance(exc_info.value.__context__, ValueError)
    assert str(exc_info.value.__context__) == "primary"
    assert context.outcome.cleanup_callbacks == []


@pytest.mark.unit
def test_dispatch_preserves_primary_failure_when_cleanup_also_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A cleanup failure must not replace the action failure being unwound."""
    context = _context()
    context.outcome.cleanup_callbacks.append(
        MagicMock(side_effect=RuntimeError("cleanup"))
    )
    monkeypatch.setattr(
        dispatch,
        "_print_connection",
        MagicMock(side_effect=ValueError("primary")),
    )
    hooks = MagicMock()

    with pytest.raises(ValueError, match="primary"):
        dispatch.dispatch_connected(context, hooks)

    assert context.outcome.cleanup_callbacks == []


@pytest.mark.unit
def test_stop_processing_still_runs_final_disconnect_lifecycle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Early action termination should skip later actions but still finalize/close."""
    context = _context()
    interface = MagicMock()
    context.interface = cast(MeshInterface, interface)
    context.args.wait_to_disconnect = 2
    context.outcome.close_now = True

    def _stop(_context: CliContext, _hooks: object) -> None:
        _context.outcome.stop_processing = True

    monkeypatch.setattr(dispatch.device_actions, "_handle_device_actions", _stop)
    later_action = MagicMock()
    monkeypatch.setattr(
        dispatch.messaging_service_actions,
        "_handle_messaging_actions",
        later_action,
    )
    sleep = MagicMock()
    hooks = MagicMock()
    hooks.sleep = sleep

    dispatch.dispatch_connected(context, hooks)

    later_action.assert_not_called()
    sleep.assert_called_once_with(2)
    interface.close.assert_called_once_with()


@pytest.mark.unit
def test_finalize_waits_for_ack_when_requested() -> None:
    """An explicit --ack request must trigger the shared final ACK/NAK wait."""
    context = _context()
    interface = MagicMock()
    node = interface.getNode.return_value
    context.interface = cast(MeshInterface, interface)
    context.args.ack = True
    hooks = MagicMock()

    dispatch._finalize_connected_actions(context, hooks)  # noqa: SLF001

    interface.getNode.assert_called_once_with("^all", False)
    node.iface.waitForAckNak.assert_called_once_with()


@pytest.mark.unit
def test_finalize_skip_ack_wait_takes_precedence() -> None:
    """Actions that own completion must suppress the shared ACK/NAK wait."""
    context = _context()
    interface = MagicMock()
    context.interface = cast(MeshInterface, interface)
    context.args.ack = True
    context.outcome.skip_ack_wait = True

    dispatch._finalize_connected_actions(context, MagicMock())  # noqa: SLF001

    interface.getNode.assert_not_called()


@pytest.mark.unit
def test_finalize_swallows_interface_close_failure(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Transport-close failures during finalization should remain diagnostic-only."""
    import logging

    caplog.set_level(logging.DEBUG, logger=dispatch.__name__)
    context = _context()
    interface = MagicMock()
    interface.close.side_effect = RuntimeError("close failed")
    context.interface = cast(MeshInterface, interface)
    context.outcome.close_now = True

    dispatch._finalize_connected_actions(context, MagicMock())  # noqa: SLF001

    interface.close.assert_called_once_with()
    assert "Error during interface close" in caplog.text
