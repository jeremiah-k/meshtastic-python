"""Internal connected-CLI execution context.

This module deliberately owns no CLI command behavior. It provides the small,
explicit state contract shared by the connected action runtimes so command
handlers do not need to reach back into ``meshtastic.__main__`` globals.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from meshtastic.mesh_interface import MeshInterface


@dataclass(slots=True)
class ActionOutcome:
    """Track connection-lifecycle decisions made by connected CLI actions.

    Attributes
    ----------
    close_now : bool
        Whether the connected interface should be closed after actions finish.
    wait_for_ack_nak : bool
        Whether a remote operation expects the shared final ACK/NAK wait.
    skip_ack_wait : bool
        Whether an action owns its acknowledgment/reboot completion and the
        shared final ACK/NAK wait must be skipped.
    """

    close_now: bool = False
    wait_for_ack_nak: bool = True
    skip_ack_wait: bool = False


@dataclass(slots=True)
class CliContext:
    """State shared by connected CLI action handlers.

    Parameters
    ----------
    interface : MeshInterface
        Connected interface on which actions operate.
    args : argparse.Namespace
        Parsed command-line arguments for this invocation.
    get_node_kwargs : dict[str, Any]
        Keyword arguments historically forwarded to ``MeshInterface.getNode``.
    outcome : ActionOutcome
        Mutable lifecycle outcome accumulated across compatible actions.
    """

    interface: MeshInterface
    args: argparse.Namespace
    get_node_kwargs: dict[str, Any]
    outcome: ActionOutcome = field(default_factory=ActionOutcome)

    @property
    def destination(self) -> str:
        """Return the selected destination address.

        Returns
        -------
        str
            Destination value from parsed CLI arguments.
        """

        return str(self.args.dest)
