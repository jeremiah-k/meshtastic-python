"""Invocation-scoped ownership for CLI resources and long-running services."""

from __future__ import annotations

import contextvars
from collections.abc import Callable
from contextlib import AbstractContextManager, ExitStack
from dataclasses import dataclass, field
from typing import TypeVar

_T = TypeVar("_T")
_CURRENT_SESSION_RESOURCES: contextvars.ContextVar[CliSessionResources | None] = (
    contextvars.ContextVar("cli_session_resources", default=None)
)


@dataclass(slots=True)
class SessionCleanup:
    """One idempotent cleanup registered with an invocation resource stack."""

    callback: Callable[[], None]
    _armed: bool = field(default=True, init=False)

    def run(self) -> None:
        """Run the cleanup at most once."""
        if not self._armed:
            return
        self._armed = False
        self.callback()


class CliSessionResources:
    """Register resources against the ``common()`` invocation ``ExitStack``."""

    def __init__(self, stack: ExitStack) -> None:
        """Bind session ownership to an existing invocation stack."""
        self._stack = stack
        self._activated = False

    def activate(self) -> None:
        """Expose this owner to connected action contexts until stack unwind."""
        if self._activated:
            return
        token = _CURRENT_SESSION_RESOURCES.set(self)
        self._activated = True

        def _reset_context() -> None:
            _CURRENT_SESSION_RESOURCES.reset(token)
            self._activated = False

        self._stack.callback(_reset_context)

    def enter_context(self, resource: AbstractContextManager[_T]) -> _T:
        """Enter a context-managed resource under invocation ownership."""
        return self._stack.enter_context(resource)

    def register_cleanup(self, callback: Callable[[], None]) -> SessionCleanup:
        """Register an idempotent cleanup and return its execution lease."""
        cleanup = SessionCleanup(callback)
        self._stack.callback(cleanup.run)
        return cleanup


def get_current_session_resources() -> CliSessionResources | None:
    """Return the active invocation resource owner, if one exists."""
    return _CURRENT_SESSION_RESOURCES.get()
