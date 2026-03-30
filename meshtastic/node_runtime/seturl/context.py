"""Admin-channel write path metadata for setURL transactions."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from meshtastic.node import Node


@dataclass(frozen=True)
class _SetUrlAdminContext:
    """Admin-channel write path metadata resolved before planning/execution.

    Attributes
    ----------
    admin_write_node : Node
        The node that will handle admin channel writes for this transaction.
    admin_index_for_write : int
        The admin index to use for write operations.
    named_admin_index_for_write : int | None
        The named admin index for write operations, if available.
    has_admin_write_node_named_admin : bool
        Whether the admin write node has an associated named admin index.
    """

    admin_write_node: Node
    admin_index_for_write: int
    named_admin_index_for_write: int | None
    has_admin_write_node_named_admin: bool
