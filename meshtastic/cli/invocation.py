"""Invocation-scoped CLI state with legacy ``mt_config`` compatibility support."""

from __future__ import annotations

import argparse
import contextvars
from contextlib import contextmanager
from dataclasses import dataclass
from typing import IO, Iterator

_CURRENT_INVOCATION: contextvars.ContextVar[CliInvocation | None] = (
    contextvars.ContextVar("cli_invocation", default=None)
)


@dataclass(slots=True)
class CliInvocation:
    """State owned by one top-level CLI invocation.

    Parameters
    ----------
    args : argparse.Namespace
        Parsed command-line arguments for this invocation.
    parser : argparse.ArgumentParser
        Parser that produced ``args`` and owns usage/error reporting.
    channel_index : int | None
        Selected channel index, if one was explicitly chosen.
    camel_case : bool
        Whether user-facing configuration field names use camelCase.
    logfile : IO[str] | None
        Active serial debug output stream owned by this invocation.
    """

    args: argparse.Namespace
    parser: argparse.ArgumentParser
    channel_index: int | None = None
    camel_case: bool = False
    logfile: IO[str] | None = None


@contextmanager
def activate_invocation(invocation: CliInvocation) -> Iterator[CliInvocation]:
    """Expose ``invocation`` as current state for the duration of one CLI flow."""
    token = _CURRENT_INVOCATION.set(invocation)
    try:
        yield invocation
    finally:
        _CURRENT_INVOCATION.reset(token)


def get_current_invocation() -> CliInvocation | None:
    """Return the active CLI invocation, if execution is invocation-scoped."""
    return _CURRENT_INVOCATION.get()
