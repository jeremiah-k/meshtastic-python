"""Post-configure value-aware verification helpers."""

from __future__ import annotations

import base64
import logging
from typing import Any

import meshtastic.util
from meshtastic.protobuf import apponly_pb2, channel_pb2

logger = logging.getLogger(__name__)


def _is_repeated_field(field_desc: Any) -> bool:
    is_repeated = getattr(field_desc, "is_repeated", None)
    if isinstance(is_repeated, bool):
        return is_repeated
    label = getattr(field_desc, "label", None)
    label_repeated = getattr(field_desc, "LABEL_REPEATED", None)
    return label is not None and label == label_repeated


def _coerce_element(field_desc: Any, element: Any) -> Any:
    # TYPE_STRING: preserve as-is (do not apply fromStr)
    if _is_string_field(field_desc):
        return element
    # Enum fields: convert enum name to enum number
    if field_desc.enum_type is not None and isinstance(element, str):
        enum_val = field_desc.enum_type.values_by_name.get(element)
        if enum_val is not None:
            return enum_val.number
        logger.debug(
            "Unknown enum name %r for repeated element in field; treating as mismatch.",
            element,
        )
        return element
    # Non-string, non-enum fields: apply fromStr for coercion
    # Note: TYPE_STRING fields are already handled above and returned unchanged
    if isinstance(element, str) and field_desc.enum_type is None:
        return meshtastic.util.fromStr(element)
    return element


def _is_string_field(field_desc: Any) -> bool:
    field_type = getattr(field_desc, "type", None)
    type_string = getattr(field_desc, "TYPE_STRING", None)
    return (
        field_type is not None and type_string is not None and field_type == type_string
    )


def _verify_requested_fields(
    yaml_dict: dict[str, Any],
    proto_message: Any,
    section_path: str,
) -> list[str]:
    """Compare YAML-requested field values against a protobuf message.

    Recursively walks *yaml_dict*, converting each camelCase key to
    snake_case and looking up the corresponding field on *proto_message*.
    For leaf values, enum strings are resolved to their numeric form and
    non-enum strings are coerced via ``meshtastic.util.fromStr()``.
    Repeated fields are compared as lists; non-repeated fields that
    receive a list use only the first element.

    Parameters
    ----------
    yaml_dict : dict[str, Any]
        Mapping of camelCase field names to the YAML-requested values.
    proto_message : Any
        Protocol buffer message whose current field values are the
        source of truth for comparison.
    section_path : str
        Dot-separated path prefix used in mismatch reports (e.g.
        ``"Config.lora"``).

    Returns
    -------
    list[str]
        Dot-separated paths of fields whose requested value does not
        match the protobuf value.  Empty list means all fields match.
    """
    mismatches: list[str] = []
    for key, yaml_value in yaml_dict.items():
        snake_key = meshtastic.util.camel_to_snake(key)
        field_desc = proto_message.DESCRIPTOR.fields_by_name.get(snake_key)
        if field_desc is None:
            mismatches.append(f"{section_path}.{key}")
            continue
        if isinstance(yaml_value, dict):
            sub_msg = getattr(proto_message, snake_key)
            mismatches.extend(
                _verify_requested_fields(
                    yaml_value, sub_msg, f"{section_path}.{snake_key}"
                )
            )
        else:
            actual = getattr(proto_message, snake_key)
            if _is_repeated_field(field_desc):
                yaml_list = (
                    yaml_value
                    if isinstance(yaml_value, (list, tuple))
                    else [yaml_value]
                )
                coerced = [_coerce_element(field_desc, el) for el in yaml_list]
                if list(coerced) != list(actual):
                    mismatches.append(f"{section_path}.{snake_key}")
            else:
                scalar: Any = yaml_value
                if field_desc.enum_type is not None and isinstance(yaml_value, str):
                    enum_val = field_desc.enum_type.values_by_name.get(yaml_value)
                    if enum_val is not None:
                        scalar = enum_val.number
                    else:
                        logger.debug(
                            "Unknown enum name %r for field %s.%s; treating as mismatch.",
                            yaml_value,
                            section_path,
                            snake_key,
                        )
                        scalar = yaml_value
                if (
                    isinstance(yaml_value, str)
                    and field_desc.enum_type is None
                    and not _is_string_field(field_desc)
                ):
                    scalar = meshtastic.util.fromStr(yaml_value)
                if isinstance(scalar, (list, tuple)):
                    logger.debug(
                        "YAML provided a list for non-repeated field %s.%s; using first element.",
                        section_path,
                        snake_key,
                    )
                    scalar = scalar[0] if scalar else scalar
                if scalar != actual:
                    mismatches.append(f"{section_path}.{snake_key}")
    return mismatches


def _verify_channel_url_match(
    requested_url: str,
    device_url: str,
) -> bool:
    req_cs = _parse_channel_set(requested_url)
    dev_cs = _parse_channel_set(device_url)
    if req_cs is None or dev_cs is None:
        return False
    return _verify_channel_sets_match(req_cs, dev_cs, emit_warnings=True)


def _verify_channel_url_against_state(
    requested_url: str,
    *,
    device_channels: list[Any] | None,
    device_lora_config: Any | None,
    emit_warnings: bool = True,
) -> bool:
    """Verify requested channel URL against already-loaded device channel/LoRa state."""
    requested_channel_set = _parse_channel_set(requested_url)
    if requested_channel_set is None:
        if emit_warnings:
            logger.warning(
                "Channel URL verification: requested URL could not be parsed."
            )
        return False
    device_channel_set = _build_channel_set_from_state(
        device_channels=device_channels,
        device_lora_config=device_lora_config,
        emit_warnings=emit_warnings,
    )
    if device_channel_set is None:
        return False
    return _verify_channel_sets_match(
        requested_channel_set,
        device_channel_set,
        emit_warnings=emit_warnings,
    )


def _parse_channel_set(url: str) -> apponly_pb2.ChannelSet | None:
    try:
        b64 = url.split("#")[-1]
        b64 += "=" * ((4 - len(b64) % 4) % 4)
        raw = base64.b64decode(b64, altchars=b"-_")
        channel_set = apponly_pb2.ChannelSet()
        channel_set.ParseFromString(raw)
        return channel_set
    except Exception:
        return None


def _build_channel_set_from_state(
    *,
    device_channels: list[Any] | None,
    device_lora_config: Any | None,
    emit_warnings: bool,
) -> apponly_pb2.ChannelSet | None:
    if not device_channels:
        if emit_warnings:
            logger.warning("Channel URL verification: device channels are not loaded.")
        return None

    primary_channel = next(
        (
            channel
            for channel in device_channels
            if channel.role == channel_pb2.Channel.Role.PRIMARY
        ),
        None,
    )
    if primary_channel is None:
        if emit_warnings:
            logger.warning(
                "Channel URL verification: no primary channel in device state."
            )
        return None

    channel_set = apponly_pb2.ChannelSet()
    channel_set.settings.append(primary_channel.settings)
    for channel in device_channels:
        if channel.role == channel_pb2.Channel.Role.SECONDARY:
            channel_set.settings.append(channel.settings)

    if device_lora_config is None:
        if emit_warnings:
            logger.warning(
                "Channel URL verification: device LoRa config is not loaded."
            )
        return None
    try:
        channel_set.lora_config.CopyFrom(device_lora_config)
    except Exception:
        if emit_warnings:
            logger.warning(
                "Channel URL verification: failed to copy device LoRa config.",
                exc_info=True,
            )
        return None
    return channel_set


def _settings_match(req: Any, dev: Any) -> bool:
    checks = [
        req.psk == dev.psk,
        req.name == dev.name,
        req.id == dev.id,
        req.uplink_enabled == dev.uplink_enabled,
        req.downlink_enabled == dev.downlink_enabled,
        req.module_settings.position_precision
        == dev.module_settings.position_precision,
        req.module_settings.is_muted == dev.module_settings.is_muted,
    ]
    return all(checks)


def _has_duplicate_names(
    names: list[str],
    *,
    source_label: str,
    emit_warnings: bool,
) -> bool:
    if len(names) == len(set(names)):
        return False
    if emit_warnings:
        logger.warning(
            "Channel URL verification: duplicate channel names in %s URL "
            "(%s); cannot verify unambiguously.",
            source_label,
            ", ".join(names),
        )
    return True


def _lora_config_match(
    req: apponly_pb2.ChannelSet,
    dev: apponly_pb2.ChannelSet,
    *,
    emit_warnings: bool,
) -> bool:
    req_has_lora = req.HasField("lora_config")
    dev_has_lora = dev.HasField("lora_config")
    if req_has_lora != dev_has_lora:
        if emit_warnings:
            logger.warning(
                "Channel URL verification: lora_config presence mismatch "
                "(requested=%s, device=%s).",
                req_has_lora,
                dev_has_lora,
            )
        return False
    if req_has_lora and (
        req.lora_config.SerializeToString() != dev.lora_config.SerializeToString()
    ):
        if emit_warnings:
            logger.warning(
                "Channel URL verification: lora_config differs between requested and device URLs."
            )
        return False
    return True


def _verify_channel_sets_match(
    requested_channel_set: apponly_pb2.ChannelSet,
    device_channel_set: apponly_pb2.ChannelSet,
    *,
    emit_warnings: bool,
) -> bool:
    # Both sets must have a primary channel entry
    if not requested_channel_set.settings or not device_channel_set.settings:
        if emit_warnings:
            logger.warning(
                "Channel URL verification: missing primary channel entry "
                "(requested=%d, device=%d).",
                len(requested_channel_set.settings),
                len(device_channel_set.settings),
            )
        return False

    # Primary channel must match exactly (fail early if primary differs)
    requested_primary = requested_channel_set.settings[0]
    device_primary = device_channel_set.settings[0]
    if not _settings_match(requested_primary, device_primary):
        if emit_warnings:
            logger.warning(
                "Channel URL verification: primary channel entry differs "
                "(requested primary=%r, device primary=%r).",
                requested_primary.name,
                device_primary.name,
            )
        return False

    requested_names = [settings.name for settings in requested_channel_set.settings]
    device_names = [settings.name for settings in device_channel_set.settings]
    if _has_duplicate_names(
        requested_names,
        source_label="requested",
        emit_warnings=emit_warnings,
    ) or _has_duplicate_names(
        device_names,
        source_label="device",
        emit_warnings=emit_warnings,
    ):
        return False

    requested_lookup: dict[str, Any] = {
        settings.name: settings for settings in requested_channel_set.settings
    }
    device_lookup: dict[str, Any] = {
        settings.name: settings for settings in device_channel_set.settings
    }
    requested_name_set = set(requested_lookup.keys())
    device_name_set = set(device_lookup.keys())
    if requested_name_set != device_name_set:
        missing_on_device = requested_name_set - device_name_set
        extra_on_device = device_name_set - requested_name_set
        parts: list[str] = []
        if missing_on_device:
            parts.append(f"missing on device: {sorted(missing_on_device)}")
        if extra_on_device:
            parts.append(f"extra on device: {sorted(extra_on_device)}")
        if emit_warnings:
            logger.warning(
                "Channel URL verification: channel name sets do not match (%s).",
                "; ".join(parts),
            )
        return False

    if not _lora_config_match(
        requested_channel_set,
        device_channel_set,
        emit_warnings=emit_warnings,
    ):
        return False

    return all(
        _settings_match(requested_settings, device_lookup[name])
        for name, requested_settings in requested_lookup.items()
    )
