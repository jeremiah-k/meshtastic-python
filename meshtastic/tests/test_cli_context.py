"""Tests for the internal connected CLI execution context."""

from argparse import Namespace
from types import SimpleNamespace
from typing import cast

import pytest

from meshtastic.cli.context import ActionOutcome, CliContext
from meshtastic.mesh_interface import MeshInterface


@pytest.mark.unit
def test_action_outcome_defaults_are_explicit() -> None:
    """Action outcomes should expose conservative lifecycle defaults."""
    outcome = ActionOutcome()

    assert outcome.close_now is False
    assert outcome.wait_for_ack_nak is True
    assert outcome.skip_ack_wait is False


@pytest.mark.unit
def test_cli_context_exposes_destination_and_shared_outcome() -> None:
    """Handlers should share one explicit lifecycle outcome through the context."""
    interface = cast(MeshInterface, SimpleNamespace())
    args = Namespace(dest="!12345678")
    outcome = ActionOutcome(close_now=True, wait_for_ack_nak=False)
    context = CliContext(
        interface=interface,
        args=args,
        get_node_kwargs={"timeout": 10.0},
        outcome=outcome,
    )

    assert context.destination == "!12345678"
    assert context.outcome is outcome
    assert context.get_node_kwargs == {"timeout": 10.0}


@pytest.mark.unit
def test_cli_context_destination_normalizes_compatibility_doubles() -> None:
    """Destination access should tolerate non-string compatibility test doubles."""
    context = CliContext(
        interface=cast(MeshInterface, SimpleNamespace()),
        args=Namespace(dest=1234),
        get_node_kwargs={},
    )

    assert context.destination == "1234"
