"""Shared Node runtime constants and helpers."""

# Validation error messages for setOwner.
EMPTY_LONG_NAME_MSG: str = (
    "Long Name cannot be empty or contain only whitespace characters"
)
EMPTY_SHORT_NAME_MSG: str = (
    "Short Name cannot be empty or contain only whitespace characters"
)
# Maximum length for long_name (per protobuf definition in mesh.options).
MAX_LONG_NAME_LEN: int = 40
# Maximum length for owner short_name.
MAX_SHORT_NAME_LEN: int = 4
# Maximum text length for ringtone messages.
MAX_RINGTONE_LENGTH: int = 230
# Maximum text length for canned-message payloads.
MAX_CANNED_MESSAGE_LENGTH: int = 200
# Maximum number of channels a node can hold.
MAX_CHANNELS: int = 8
# Protobuf factory-reset fields are integer-typed; use the explicit sentinel
# value instead of boolean assignment to avoid firmware-side coercion issues.
FACTORY_RESET_REQUEST_VALUE: int = 1
# Extra wait used only when getMetadata() runs under redirected stdout for
# historical callers that parse printed metadata lines.
METADATA_STDOUT_COMPAT_WAIT_SECONDS: float = 1.0
NAMED_ADMIN_CHANNEL_NAME: str = "admin"


def _is_named_admin_channel_name(channel_name: str) -> bool:
    """Return whether a channel name designates the special named admin channel."""
    return channel_name.lower() == NAMED_ADMIN_CHANNEL_NAME


def _ordered_admin_indexes(*indexes: int | None) -> list[int]:
    """Return unique non-None admin channel indexes, preserving input order."""
    ordered: list[int] = []
    for index in indexes:
        if index is None or index in ordered:
            continue
        ordered.append(index)
    return ordered


# Compatibility aliases for internal callers that import without underscore prefix
is_named_admin_channel_name = _is_named_admin_channel_name
ordered_admin_indexes = _ordered_admin_indexes
