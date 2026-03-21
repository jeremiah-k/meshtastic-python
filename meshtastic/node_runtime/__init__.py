"""Internal Node runtime owners grouped by responsibility domain."""

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
    is_named_admin_channel_name,
    ordered_admin_indexes,
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
    "is_named_admin_channel_name",
    "ordered_admin_indexes",
]
