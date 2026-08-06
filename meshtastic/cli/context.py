"""Internal connected-CLI execution context.

This module deliberately owns no CLI command behavior. It provides the small,
explicit state contract shared by the connected action runtimes so command
handlers do not need to reach back into ``meshtastic.__main__`` globals.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass, field
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, NoReturn, Protocol

if TYPE_CHECKING:
    from meshtastic.mesh_interface import MeshInterface


class CliExit(Protocol):
    """Callable contract for terminating CLI execution with an optional status."""

    def __call__(self, message: str, return_value: int = 1) -> NoReturn:
        """Report *message* and terminate with *return_value*."""


def _terminate_cli(cli_exit: CliExit, message: str, return_value: int = 1) -> NoReturn:
    """Invoke a CLI exit seam and fail closed if an injected seam returns.

    Parameters
    ----------
    cli_exit : CliExit
        Exit callable supplied by the entrypoint or a test seam.
    message : str
        User-facing failure message.
    return_value : int
        Process exit status forwarded to ``cli_exit``.

    Raises
    ------
    AssertionError
        If a non-conforming injected ``cli_exit`` returns instead of terminating.

    Notes
    -----
    Production ``cli_exit`` implementations are non-returning. Keeping this guard
    in one place makes extracted action runtimes fail closed even when tests or
    downstream compatibility seams inject a returning callable.
    """
    cli_exit(message, return_value)
    raise AssertionError("cli_exit returned unexpectedly") from None


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
    stop_processing : bool
        Whether action dispatch should return immediately after the current group.
    cleanup_callbacks : list[Callable[[], None]]
        Resource cleanup callbacks to run after connected actions complete.
    """

    close_now: bool = False
    wait_for_ack_nak: bool = False
    skip_ack_wait: bool = False
    stop_processing: bool = False
    cleanup_callbacks: list[Callable[[], None]] = field(default_factory=list)


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
