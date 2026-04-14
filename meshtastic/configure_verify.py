"""Post-configure value-aware verification helpers."""

from __future__ import annotations

import base64
import logging
from typing import Any

import meshtastic.util
from meshtastic.protobuf import apponly_pb2

logger = logging.getLogger(__name__)


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
            coerced = yaml_value
            if field_desc.enum_type is not None and isinstance(yaml_value, str):
                enum_val = field_desc.enum_type.values_by_name.get(yaml_value)
                if enum_val is not None:
                    coerced = enum_val.number
                else:
                    logger.debug(
                        "Unknown enum name %r for field %s.%s; treating as mismatch.",
                        yaml_value,
                        section_path,
                        snake_key,
                    )
                    coerced = yaml_value
            if isinstance(yaml_value, str) and field_desc.enum_type is None:
                coerced = meshtastic.util.fromStr(yaml_value)
            if field_desc.is_repeated:
                if not isinstance(coerced, (list, tuple)):
                    coerced = [coerced]
                if list(coerced) != list(actual):
                    mismatches.append(f"{section_path}.{snake_key}")
            else:
                if isinstance(coerced, (list, tuple)):
                    logger.debug(
                        "YAML provided a list for non-repeated field %s.%s; using first element.",
                        section_path,
                        snake_key,
                    )
                    coerced = coerced[0] if coerced else coerced
                if coerced != actual:
                    mismatches.append(f"{section_path}.{snake_key}")
    return mismatches


def _verify_channel_url_match(
    requested_url: str,
    device_url: str,
) -> bool:
    def _parse_channel_set(url: str) -> apponly_pb2.ChannelSet | None:
        try:
            b64 = url.split("#")[-1]
            b64 += "=" * ((4 - len(b64) % 4) % 4)
            raw = base64.b64decode(b64, altchars=b"-_")
            cs = apponly_pb2.ChannelSet()
            cs.ParseFromString(raw)
            return cs
        except Exception:
            return None

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

    req_cs = _parse_channel_set(requested_url)
    dev_cs = _parse_channel_set(device_url)
    if req_cs is None or dev_cs is None:
        return False

    req_names = [s.name for s in req_cs.settings]
    dev_names = [s.name for s in dev_cs.settings]
    if len(req_names) != len(set(req_names)):
        logger.warning(
            "Channel URL verification: duplicate channel names in requested URL "
            "(%s); cannot verify unambiguously.",
            ", ".join(req_names),
        )
        return False
    if len(dev_names) != len(set(dev_names)):
        logger.warning(
            "Channel URL verification: duplicate channel names in device URL "
            "(%s); cannot verify unambiguously.",
            ", ".join(dev_names),
        )
        return False

    req_lookup: dict[str, Any] = {s.name: s for s in req_cs.settings}
    dev_lookup: dict[str, Any] = {s.name: s for s in dev_cs.settings}
    req_name_set = set(req_lookup.keys())
    dev_name_set = set(dev_lookup.keys())
    if req_name_set != dev_name_set:
        missing_on_device = req_name_set - dev_name_set
        extra_on_device = dev_name_set - req_name_set
        parts: list[str] = []
        if missing_on_device:
            parts.append(f"missing on device: {sorted(missing_on_device)}")
        if extra_on_device:
            parts.append(f"extra on device: {sorted(extra_on_device)}")
        logger.warning(
            "Channel URL verification: channel name sets do not match (%s).",
            "; ".join(parts),
        )
        return False
    for name, req_settings in req_lookup.items():
        dev_settings = dev_lookup[name]
        if not _settings_match(req_settings, dev_settings):
            return False
    return True
