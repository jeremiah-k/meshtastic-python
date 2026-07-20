"""Structured traceroute results for Meshtastic library callers."""

from __future__ import annotations

from dataclasses import dataclass

__all__ = ["TraceRouteHop", "TraceRouteResult"]


@dataclass(frozen=True, slots=True)
class TraceRouteHop:
    """One node in a traced route.

    ``snr_db`` describes the link from the preceding hop to this hop. It is
    ``None`` for the first hop and whenever the firmware did not provide a
    complete SNR sequence.
    """

    node_num: int
    node_id: str
    snr_db: float | None = None


@dataclass(frozen=True, slots=True)
class TraceRouteResult:
    """A completed traceroute in both available directions.

    ``route_towards`` always starts with the local/source node and ends with
    the destination node. ``route_back`` follows the destination back to the
    source when firmware reports a reverse route; otherwise it is ``None``.
    Incomplete SNR arrays preserve the reported topology with unknown links.
    """

    route_towards: tuple[TraceRouteHop, ...]
    route_back: tuple[TraceRouteHop, ...] | None
    request_id: int | None = None

    @property
    def source(self) -> TraceRouteHop:
        """Return the first hop in the outbound route."""
        return self.route_towards[0]

    @property
    def destination(self) -> TraceRouteHop:
        """Return the final hop in the outbound route."""
        return self.route_towards[-1]
