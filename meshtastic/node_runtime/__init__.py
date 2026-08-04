"""Internal convenience exports for Node runtime implementations.

The ``node_runtime`` package is an implementation detail and its ``__all__``
list is not public API. Runtime compatibility guarantees are limited to entries
in ``meshtastic/_runtime_compatibility.json`` and documented in
``COMPATIBILITY.md``.
"""

from .shared import (
    EMPTY_LONG_NAME_MSG,
    EMPTY_SHORT_NAME_MSG,
    FACTORY_RESET_REQUEST_VALUE,
    MAX_CANNED_MESSAGE_LENGTH,
    MAX_CHANNELS,
    MAX_LONG_NAME_LEN,
    MAX_RINGTONE_LENGTH,
    MAX_SHORT_NAME_LEN,
    METADATA_STDOUT_COMPAT_WAIT_SECONDS,
    NAMED_ADMIN_CHANNEL_NAME,
    isNamedAdminChannelName,
    orderedAdminIndexes,
)

__all__ = [
    "EMPTY_LONG_NAME_MSG",
    "EMPTY_SHORT_NAME_MSG",
    "FACTORY_RESET_REQUEST_VALUE",
    "MAX_CANNED_MESSAGE_LENGTH",
    "MAX_CHANNELS",
    "MAX_LONG_NAME_LEN",
    "MAX_RINGTONE_LENGTH",
    "MAX_SHORT_NAME_LEN",
    "METADATA_STDOUT_COMPAT_WAIT_SECONDS",
    "NAMED_ADMIN_CHANNEL_NAME",
    "isNamedAdminChannelName",
    "orderedAdminIndexes",
]
