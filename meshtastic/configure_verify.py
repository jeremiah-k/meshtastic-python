"""Post-configure value-aware verification helpers."""

from __future__ import annotations

import base64
import logging
from typing import Any

import meshtastic.util
from meshtastic.protobuf import apponly_pb2

logger = logging.getLogger(__name__)


def _is_repeated_field(field_desc: Any) -> bool:
    is_repeated = getattr(field_desc, "is_repeated", None)
    if isinstance(is_repeated, bool):
        return is_repeated
    label = getattr(field_desc, "label", None)
    label_repeated = getattr(field_desc, "LABEL_REPEATED", None)
    return label is not None and label == label_repeated


def _coerce_element(field_desc: Any, element: Any) -> Any:
    if field_desc.enum_type is not None and isinstance(element, str):
        enum_val = field_desc.enum_type.values_by_name.get(element)
        if enum_val is not None:
            return enum_val.number
        logger.debug(
            "Unknown enum name %r for repeated element in field; treating as mismatch.",
            element,
        )
        return element
    if isinstance(element, str) and field_desc.enum_type is None:
        return meshtastic.util.fromStr(element)
    return element


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
                if isinstance(yaml_value, str) and field_desc.enum_type is None:
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

    def _has_duplicate_names(names: list[str], *, source_label: str) -> bool:
        if len(names) == len(set(names)):
            return False
        logger.warning(
            "Channel URL verification: duplicate channel names in %s URL "
            "(%s); cannot verify unambiguously.",
            source_label,
            ", ".join(names),
        )
        return True

    def _lora_config_match(req: apponly_pb2.ChannelSet, dev: apponly_pb2.ChannelSet) -> bool:
        req_has_lora = req.HasField("lora_config")
        dev_has_lora = dev.HasField("lora_config")
        if req_has_lora != dev_has_lora:
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
            logger.warning(
                "Channel URL verification: lora_config differs between requested and device URLs."
            )
            return False
        return True

    req_cs = _parse_channel_set(requested_url)
    dev_cs = _parse_channel_set(device_url)
    if req_cs is None or dev_cs is None:
        return False

    req_names = [s.name for s in req_cs.settings]
    dev_names = [s.name for s in dev_cs.settings]
    if _has_duplicate_names(req_names, source_label="requested") or _has_duplicate_names(
        dev_names, source_label="device"
    ):
        return False

    req_lookup: dict[str, Any] = {s.name: s for s in req_cs.settings}
    dev_lookup: dict[str, Any] = {s.name: s for s in dev_cs.settings}
    req_name_set = set(req_lookup.keys())
    dev_name_set = set(dev_lookup.keys())
    name_sets_match = req_name_set == dev_name_set
    if not name_sets_match:
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

    lora_match = _lora_config_match(req_cs, dev_cs)
    settings_match = all(
        _settings_match(req_settings, dev_lookup[name])
        for name, req_settings in req_lookup.items()
    )
    return name_sets_match and lora_match and settings_match
