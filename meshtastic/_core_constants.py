"""Internal leaf constants shared by Meshtastic runtime components.

This module intentionally contains no imports from other ``meshtastic`` modules.
Public compatibility is provided by re-exports from :mod:`meshtastic`.
"""

__all__ = (
    "BROADCAST_ADDR",
    "BROADCAST_NUM",
    "DECODE_ERROR_KEY",
    "LAST_DISCONNECT_SOURCE_TYPE_ERROR",
    "LOCAL_ADDR",
    "NODELESS_WANT_CONFIG_ID",
    "OUR_APP_VERSION",
)


LOCAL_ADDR = "^local"
"""Special destination identifier for the local node."""

BROADCAST_NUM: int = 0xFFFFFFFF
"""Numeric broadcast node identifier."""

BROADCAST_ADDR = "^all"
"""Special destination identifier for broadcast traffic."""

OUR_APP_VERSION: int = 20300
"""Numeric client capability version shared with Meshtastic applications."""

NODELESS_WANT_CONFIG_ID = 69420
"""Configuration request id that suppresses non-local node-info streaming."""

DECODE_ERROR_KEY = "error"
"""Dictionary key used to retain protobuf decode failures in packet data."""

LAST_DISCONNECT_SOURCE_TYPE_ERROR = (
    "_last_disconnect_source must be a str or None, got {type_name}"
)
"""Stable validation message for disconnect-source compatibility views."""
