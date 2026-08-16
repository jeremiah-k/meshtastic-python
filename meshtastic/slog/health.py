"""Health tracking primitives for structured and power logging."""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class SlogHealthSnapshot:
    """Immutable snapshot of current slog degradation and failure history.

    Attributes
    ----------
    degraded : bool
        ``True`` when at least one logging component is currently degraded.
    degraded_components : tuple[str, ...]
        Sorted component names whose most recent operation failed.
    failure_counts : tuple[tuple[str, int], ...]
        Sorted cumulative failure counts by component.
    active_errors : tuple[tuple[str, str], ...]
        Sorted current error messages for degraded components.
    """

    degraded: bool = False
    degraded_components: tuple[str, ...] = ()
    failure_counts: tuple[tuple[str, int], ...] = ()
    active_errors: tuple[tuple[str, str], ...] = ()


class _SlogHealthTracker:
    """Thread-safe mutable health state for one logging component owner."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._failure_counts: dict[str, int] = {}
        self._active_errors: dict[str, str] = {}

    def _record_failure(self, component: str, error: BaseException) -> None:
        """Record a failed component operation.

        Parameters
        ----------
        component : str
            Stable internal component identifier.
        error : BaseException
            Failure associated with the component operation.
        """
        with self._lock:
            self._failure_counts[component] = self._failure_counts.get(component, 0) + 1
            self._active_errors[component] = str(error)

    def _record_success(self, component: str) -> None:
        """Mark a component recovered after a successful operation.

        Parameters
        ----------
        component : str
            Stable internal component identifier.
        """
        with self._lock:
            self._active_errors.pop(component, None)

    def _snapshot(self) -> SlogHealthSnapshot:
        """Return an immutable point-in-time health snapshot.

        Returns
        -------
        SlogHealthSnapshot
            Current degraded components and cumulative failure counters.
        """
        with self._lock:
            degraded_components = tuple(sorted(self._active_errors))
            return SlogHealthSnapshot(
                degraded=bool(degraded_components),
                degraded_components=degraded_components,
                failure_counts=tuple(sorted(self._failure_counts.items())),
                active_errors=tuple(sorted(self._active_errors.items())),
            )


def _get_health_tracker(owner: Any) -> _SlogHealthTracker:
    """Return an owner's tracker, lazily creating one for legacy test doubles.

    Parameters
    ----------
    owner : Any
        Logging object expected to store its tracker in ``_health``.

    Returns
    -------
    _SlogHealthTracker
        Existing or newly-created tracker.
    """
    tracker = getattr(owner, "_health", None)
    if isinstance(tracker, _SlogHealthTracker):
        return tracker
    tracker = _SlogHealthTracker()
    owner._health = tracker
    return tracker


def _merge_health_snapshots(*snapshots: SlogHealthSnapshot) -> SlogHealthSnapshot:
    """Merge independent slog health snapshots without losing failure history.

    Parameters
    ----------
    *snapshots : SlogHealthSnapshot
        Snapshots whose component namespaces are expected to be independent.

    Returns
    -------
    SlogHealthSnapshot
        Aggregated current degradation and cumulative failure counts.
    """
    failure_counts: dict[str, int] = {}
    active_errors: dict[str, str] = {}
    for snapshot in snapshots:
        for component, count in snapshot.failure_counts:
            failure_counts[component] = failure_counts.get(component, 0) + count
        active_errors.update(snapshot.active_errors)
    degraded_components = tuple(sorted(active_errors))
    return SlogHealthSnapshot(
        degraded=bool(degraded_components),
        degraded_components=degraded_components,
        failure_counts=tuple(sorted(failure_counts.items())),
        active_errors=tuple(sorted(active_errors.items())),
    )
