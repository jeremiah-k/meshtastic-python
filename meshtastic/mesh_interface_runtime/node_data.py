"""Node data processing utilities for filtering, sorting, and extraction."""

from typing import Any


def extract_node_field_value(node_dict: dict[str, Any], field_path: str) -> Any:
    """Retrieve a nested value from a dictionary using a dotted key path.

    Parameters
    ----------
    node_dict : dict[str, Any]
        Dictionary to traverse.
    field_path : str
        Dotted path (e.g., "a.b.c"). Non-dotted paths are treated
        as a single-level lookup on node_dict.

    Returns
    -------
    Any
        The value found at the given path, or `None` if any intermediate
        key is missing or an intermediate value is not a dictionary.
    """
    if not isinstance(node_dict, dict):
        return None
    if "." not in field_path:
        return node_dict.get(field_path)
    keys = field_path.split(".")
    value: Any = node_dict
    for key in keys:
        if isinstance(value, dict):
            value = value.get(key)
        else:
            return None
    return value


def get_default_show_fields() -> list[str]:
    """Return the default list of fields to display in showNodes output."""
    return [
        "N",
        "user.longName",
        "user.id",
        "user.shortName",
        "user.hwModel",
        "user.publicKey",
        "user.role",
        "position.latitude",
        "position.longitude",
        "position.altitude",
        "deviceMetrics.batteryLevel",
        "deviceMetrics.channelUtilization",
        "deviceMetrics.airUtilTx",
        "snr",
        "hopsAway",
        "channel",
        "isFavorite",
        "lastHeard",
        "since",
    ]


def filter_nodes(
    nodes: list[dict[str, Any]],
    include_self: bool,
    local_node_num: int,
) -> list[dict[str, Any]]:
    """Filter nodes based on include_self option.

    Parameters
    ----------
    nodes : list[dict[str, Any]]
        List of node dictionaries.
    include_self : bool
        If False, filter out the local node.
    local_node_num : int
        The local node's number for comparison.

    Returns
    -------
    list[dict[str, Any]]
        Filtered list of nodes.
    """
    if include_self:
        return nodes
    return [node for node in nodes if node["num"] != local_node_num]


def sort_nodes(nodes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Sort nodes by lastHeard timestamp in descending order.

    Parameters
    ----------
    nodes : list[dict[str, Any]]
        List of node dictionaries.

    Returns
    -------
    list[dict[str, Any]]
        Sorted list of nodes (newest first).
    """
    return sorted(
        nodes,
        key=lambda r: r.get("lastHeard") or 0,
        reverse=True,
    )
