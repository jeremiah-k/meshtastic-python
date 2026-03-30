"""Admin-channel write path metadata for setURL transactions."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from meshtastic.node import Node


@dataclass(frozen=True)
class _SetUrlAdminContext:
    """Admin-channel write path metadata resolved before planning/execution."""

    admin_write_node: "Node"
    admin_index_for_write: int
    named_admin_index_for_write: int | None
    has_admin_write_node_named_admin: bool
