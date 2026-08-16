"""CLI protobuf preference resolution, validation, and mutation runtime.

The public CLI entrypoint keeps historical helper names as compatibility shims,
while this module owns the actual preference conversion and mutation behavior.
"""

from __future__ import annotations

import binascii
import contextlib
import contextvars
import logging
from collections.abc import Callable, Iterator
from typing import Any

from google.protobuf.descriptor import FieldDescriptor

import meshtastic.util
from meshtastic.cli.values import parse_bitfield_value
from meshtastic.protobuf import config_pb2

logger = logging.getLogger(__name__)

CONFIGURE_PREFLIGHT_MODE: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "configure_preflight_mode", default=False
)
SET_PREF_VALUE_ERRORS_FATAL: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "set_pref_value_errors_fatal", default=False
)
PREF_VALIDATION_REPORTER: contextvars.ContextVar[Callable[[str], None] | None] = (
    contextvars.ContextVar("pref_validation_reporter", default=None)
)

PREFERENCE_FIELD_ALIASES: dict[str, str] = {
    "display.use_12_hour": "display.use_12h_clock",
    "display.use12_hour": "display.use_12h_clock",
    "display.use12h_clock": "display.use_12h_clock",
    "display.use12_h_clock": "display.use_12h_clock",
}
SECRET_PREF_FIELDS: frozenset[str] = frozenset(
    {
        "psk",
        "channel_psk",
        "private_key",
        "public_key",
        "admin_key",
        "session_passkey",
        "secret",
        "api_key",
        "auth_token",
    }
)
SECRET_PREF_PATHS: frozenset[str] = frozenset(
    {
        "network.wifi_ssid",
        "network.wifi_psk",
        "mqtt.username",
        "mqtt.password",
        "bluetooth.fixed_pin",
        "security.session_passkey",
    }
)
REDACTED_PREF_VALUE = "<redacted>"
SET_VALUE_REJECTED_MESSAGE = "value rejected by validation"

BITFIELD_ENUMS = {
    "network.enabled_protocols": config_pb2.Config.NetworkConfig.ProtocolFlags,
    "position.position_flags": config_pb2.Config.PositionConfig.PositionFlags,
}


class PreferenceValueError(ValueError):
    """Raised when fatal CLI preference assignment rejects a scalar value."""


@contextlib.contextmanager
def fatal_preference_value_errors() -> Iterator[None]:
    """Temporarily make scalar preference validation failures fatal."""
    token = SET_PREF_VALUE_ERRORS_FATAL.set(True)
    try:
        yield
    finally:
        SET_PREF_VALUE_ERRORS_FATAL.reset(token)


def split_compound_name(comp_name: str) -> list[str]:
    """Split a dotted preference path, preserving the historical two-part minimum."""
    name = comp_name.split(".")
    if len(name) < 2:
        name.append(comp_name)
    return name


def normalize_pref_name(comp_name: str) -> str:
    """Normalize a preference path to canonical snake_case and apply aliases."""
    canonical = ".".join(
        meshtastic.util.camel_to_snake(part.strip()) for part in comp_name.split(".")
    )
    normalized = PREFERENCE_FIELD_ALIASES.get(canonical, canonical)
    if normalized != canonical:
        logger.debug(
            "Using compatibility alias for config field %s -> %s",
            comp_name,
            normalized,
        )
    return normalized


def is_secret_pref(name: str) -> bool:
    """Return whether a preference path is classified as secret-bearing."""
    normalized = normalize_pref_name(name)
    field_name = normalized.rsplit(".", maxsplit=1)[-1]
    return normalized in SECRET_PREF_PATHS or field_name in SECRET_PREF_FIELDS


def redact_pref_value(name: str, value: str) -> str:
    """Return a redacted placeholder for secret-bearing preference paths."""
    return REDACTED_PREF_VALUE if is_secret_pref(name) else value


def report_pref_validation(
    message: str, *, cli_print: Callable[..., None]
) -> None:
    """Report one validation diagnostic through the active preflight/output policy."""
    reporter = PREF_VALIDATION_REPORTER.get()
    if reporter is not None:
        reporter(message)
        return
    cli_print(message, force=not CONFIGURE_PREFLIGHT_MODE.get())


def walk_config_path(config: Any, name_parts: list[str]) -> tuple[Any, Any | None]:
    """Return the parent message and descriptor for a dotted config path."""
    if not name_parts:
        return config, None

    normalized_parts = [
        meshtastic.util.camel_to_snake(name_part) for name_part in name_parts
    ]
    config_part = config
    config_type = config.DESCRIPTOR.fields_by_name.get(normalized_parts[0])
    for name_part in normalized_parts[1:-1]:
        if config_type is None or config_type.message_type is None:
            return config_part, None
        config_part = getattr(config_part, config_type.name)
        config_type = config_type.message_type.fields_by_name.get(name_part)
    if (
        len(normalized_parts) > 2
        and config_type is not None
        and config_type.message_type is None
    ):
        return config_part, None
    return config_part, config_type


def resolve_pref(config: Any, comp_name: str) -> bool:
    """Return whether a dotted preference path resolves to a protobuf field."""
    normalized = normalize_pref_name(comp_name)
    name = split_compound_name(normalized)
    snake_name = meshtastic.util.camel_to_snake(name[-1])
    _config_part, config_type = walk_config_path(config, name)
    if config_type and config_type.message_type is not None:
        return config_type.message_type.fields_by_name.get(snake_name) is not None
    return config_type is not None


def protobuf_field_type_label(field: FieldDescriptor) -> str:
    """Return a concise user-facing type label for a protobuf field."""
    integer_types = {
        FieldDescriptor.TYPE_INT32,
        FieldDescriptor.TYPE_INT64,
        FieldDescriptor.TYPE_UINT32,
        FieldDescriptor.TYPE_UINT64,
        FieldDescriptor.TYPE_SINT32,
        FieldDescriptor.TYPE_SINT64,
        FieldDescriptor.TYPE_FIXED32,
        FieldDescriptor.TYPE_FIXED64,
        FieldDescriptor.TYPE_SFIXED32,
        FieldDescriptor.TYPE_SFIXED64,
    }
    if field.type in integer_types:
        return "integer"
    return {
        FieldDescriptor.TYPE_ENUM: "enum name or integer",
        FieldDescriptor.TYPE_FLOAT: "number",
        FieldDescriptor.TYPE_DOUBLE: "number",
        FieldDescriptor.TYPE_BOOL: "boolean",
        FieldDescriptor.TYPE_BYTES: "bytes (hex/base64)",
        FieldDescriptor.TYPE_STRING: "string",
    }.get(field.type, "compatible value")


def reject_pref_value(
    field: FieldDescriptor,
    *,
    field_path: str,
    raw_value: Any,
    cli_print: Callable[..., None],
) -> bool:
    """Report one invalid preference value without exposing secret input."""
    display_value = redact_pref_value(field_path, repr(raw_value))
    message = (
        f"Invalid value {display_value} for {field_path}; "
        f"expected {protobuf_field_type_label(field)}."
    )
    if SET_PREF_VALUE_ERRORS_FATAL.get():
        raise PreferenceValueError(message)
    report_pref_validation(message, cli_print=cli_print)
    return False


def assign_scalar_pref_value(
    target: Any,
    field: FieldDescriptor,
    value: Any,
    *,
    field_path: str,
    raw_value: Any,
    cli_print: Callable[..., None],
) -> bool:
    """Assign one scalar protobuf preference using compatibility conversion rules."""
    try:
        setattr(target, field.name, value)
        return True
    except (TypeError, ValueError, OverflowError):
        if field.type == FieldDescriptor.TYPE_STRING and not isinstance(value, str):
            try:
                setattr(target, field.name, str(value))
                return True
            except (TypeError, ValueError, OverflowError):
                pass
    return reject_pref_value(
        field,
        field_path=field_path,
        raw_value=raw_value,
        cli_print=cli_print,
    )


def _resolved_field(
    config: Any, comp_name: str
) -> tuple[list[str], str, Any, Any, FieldDescriptor] | None:
    """Resolve canonical path metadata required by one preference assignment."""
    name = split_compound_name(comp_name)
    snake_name = meshtastic.util.camel_to_snake(name[-1])
    config_part, config_type = walk_config_path(config, name)
    if config_type is None:
        return None
    if config_type.message_type is not None:
        pref = config_type.message_type.fields_by_name.get(snake_name)
    else:
        pref = config_type
    if pref is None:
        return None
    return name, snake_name, config_part, config_type, pref


def _converted_pref_value(
    *,
    config_type: Any,
    pref: FieldDescriptor,
    raw_value: Any,
    field_path: str,
    cli_print: Callable[..., None],
) -> tuple[bool, Any]:
    """Convert one raw preference value before protobuf assignment."""
    bitfield_enum = None
    if config_type.message_type is not None:
        bitfield_enum = BITFIELD_ENUMS.get(f"{config_type.name}.{pref.name}")
    if bitfield_enum:
        try:
            return True, parse_bitfield_value(bitfield_enum, raw_value)
        except ValueError as exc:
            report_pref_validation(f"ERROR: {exc}", cli_print=cli_print)
            return False, None
    if not isinstance(raw_value, str):
        return True, raw_value
    try:
        return True, meshtastic.util.fromStr(raw_value)
    except (ValueError, binascii.Error):
        reject_pref_value(
            pref,
            field_path=field_path,
            raw_value=raw_value,
            cli_print=cli_print,
        )
        return False, None


def _resolve_enum_value(
    pref: FieldDescriptor,
    value: Any,
    *,
    name: list[str],
    display_name: str,
    cli_print: Callable[..., None],
) -> tuple[bool, Any]:
    """Resolve an enum name to its numeric protobuf value when necessary."""
    enum_type = pref.enum_type
    if enum_type is None or not isinstance(value, str):
        return True, value
    enum_value = enum_type.values_by_name.get(value)
    if enum_value is not None:
        return True, enum_value.number
    report_pref_validation(
        f"{name[0]}.{display_name} does not have an enum called {value}, so you can not set it.",
        cli_print=cli_print,
    )
    report_pref_validation("Choices in sorted order are:", cli_print=cli_print)
    for enum_name in sorted(item.name for item in enum_type.values):
        report_pref_validation(f"    {enum_name}", cli_print=cli_print)
    return False, value


def _assign_repeated_pref_value(
    target: Any,
    pref: FieldDescriptor,
    value: Any,
    *,
    field_path: str,
    raw_value: Any,
    cli_print: Callable[..., None],
) -> tuple[bool, bool]:
    """Apply one repeated-field update transactionally on a message copy."""
    candidate = type(target)()
    candidate.CopyFrom(target)
    candidate_values = getattr(candidate, pref.name)
    try:
        if isinstance(value, list):
            new_values = [
                meshtastic.util.fromStr(item) if isinstance(item, str) else item
                for item in value
            ]
            candidate_values[:] = new_values
        elif value == 0:
            del candidate_values[:]
        else:
            current_values = [x for x in candidate_values if x not in [0, "", b""]]
            if value not in current_values:
                current_values.append(value)
            candidate_values[:] = current_values
    except (TypeError, ValueError, OverflowError, binascii.Error):
        return (
            reject_pref_value(
                pref,
                field_path=field_path,
                raw_value=raw_value,
                cli_print=cli_print,
            ),
            True,
        )

    target.CopyFrom(candidate)
    if isinstance(value, list):
        return True, True
    if not CONFIGURE_PREFLIGHT_MODE.get():
        if value == 0:
            cli_print(f"Clearing {pref.name} list")
        else:
            display_value = redact_pref_value(
                field_path, meshtastic.util.toStr(raw_value)
            )
            cli_print(f"Adding '{display_value}' to the {pref.name} list")
    return True, False


def set_pref(
    config: Any,
    comp_name: str,
    raw_value: Any,
    *,
    camel_case: bool,
    cli_print: Callable[..., None],
    is_repeated_field: Callable[[FieldDescriptor], bool],
) -> bool:
    """Set one protobuf preference with stable CLI conversion/reporting semantics."""
    normalized = normalize_pref_name(comp_name)
    resolved = _resolved_field(config, normalized)
    if resolved is None:
        return False
    name, snake_name, config_part, config_type, pref = resolved
    display_name = (
        meshtastic.util.snake_to_camel(name[-1]) if camel_case else snake_name
    )
    logger.debug("snake_name:%s", snake_name)
    logger.debug("camel_name:%s", meshtastic.util.snake_to_camel(name[-1]))

    converted, value = _converted_pref_value(
        config_type=config_type,
        pref=pref,
        raw_value=raw_value,
        field_path=normalized,
        cli_print=cli_print,
    )
    if not converted:
        return False
    logger.debug("val:%s", redact_pref_value(normalized, meshtastic.util.toStr(value)))

    if snake_name == "wifi_psk" and len(str(raw_value)) < 8:
        report_pref_validation(
            "Warning: network.wifi_psk must be 8 or more characters.",
            cli_print=cli_print,
        )
        return False

    enum_ok, value = _resolve_enum_value(
        pref,
        value,
        name=name,
        display_name=display_name,
        cli_print=cli_print,
    )
    if not enum_ok:
        return False

    target = (
        getattr(config_part, config_type.name)
        if config_type.message_type is not None
        else config_part
    )
    print_assignment = True
    if is_repeated_field(pref):
        assignment_ok, print_assignment = _assign_repeated_pref_value(
            target,
            pref,
            value,
            field_path=normalized,
            raw_value=raw_value,
            cli_print=cli_print,
        )
    else:
        assignment_ok = assign_scalar_pref_value(
            target,
            pref,
            value,
            field_path=normalized,
            raw_value=raw_value,
            cli_print=cli_print,
        )

    if assignment_ok and print_assignment:
        prefix = (
            f"{'.'.join(name[:-1])}." if config_type.message_type is not None else ""
        )
        display_value = redact_pref_value(
            normalized, meshtastic.util.toStr(raw_value)
        )
        if not CONFIGURE_PREFLIGHT_MODE.get():
            cli_print(f"Set {prefix}{display_name} to {display_value}")
    return assignment_ok


def traverse_config(
    config_root: str,
    config: dict[str, Any],
    interface_config: Any,
    *,
    resolve_pref_fn: Callable[[Any, str], bool],
    set_pref_fn: Callable[[Any, str, Any], bool],
    failed_fields: list[str] | None = None,
) -> bool:
    """Recursively apply one nested configure mapping to a protobuf message."""
    skipped_by_section: dict[str, list[str]] = {}

    def _traverse(root: str, values: dict[str, Any]) -> bool:
        section_root = meshtastic.util.camel_to_snake(root)
        for pref, raw_value in values.items():
            pref_name = f"{section_root}.{pref}"
            if isinstance(raw_value, dict):
                if not _traverse(pref_name, raw_value):
                    return False
                continue
            if not resolve_pref_fn(interface_config, pref_name):
                parts = pref_name.split(".")
                section = parts[0]
                relative = ".".join(parts[1:]) if len(parts) > 1 else pref_name
                skipped_by_section.setdefault(section, []).append(relative)
                continue
            try:
                ok = set_pref_fn(interface_config, pref_name, raw_value)
            except (ValueError, TypeError, OverflowError, binascii.Error):
                if failed_fields is not None:
                    failed_fields.append(pref_name)
                return False
            if not ok:
                if failed_fields is not None:
                    failed_fields.append(pref_name)
                return False
        return True

    success = _traverse(config_root, config)
    if not CONFIGURE_PREFLIGHT_MODE.get():
        for section, fields in skipped_by_section.items():
            logger.warning(
                "Skipping %d unknown field(s) from %s: %s",
                len(fields),
                section,
                ", ".join(fields),
            )
    return success
