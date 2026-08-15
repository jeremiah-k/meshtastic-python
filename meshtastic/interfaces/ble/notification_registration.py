"""Immutable values used by BLE notification registration transactions."""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

_NotificationCallback = Callable[[Any, Any], None]


@dataclass(frozen=True, slots=True)
class _OptionalNotificationStart:
    """One optional notification backend start prepared for execution."""

    characteristic: str
    handler: _NotificationCallback
    label: str


@dataclass(frozen=True, slots=True)
class _NotificationRegistrationPlan:
    """Prepared notification starts for one BLE connection session."""

    session_epoch: int
    optional_starts: tuple[_OptionalNotificationStart, ...]
    fromnum_handler: _NotificationCallback
