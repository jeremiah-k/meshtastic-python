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
    req_lookup: dict[str, Any] = {s.name: s for s in req_cs.settings}
    dev_lookup: dict[str, Any] = {s.name: s for s in dev_cs.settings}
    for name, req_settings in req_lookup.items():
        dev_settings = dev_lookup.get(name)
        if dev_settings is None:
            return False
        if not _settings_match(req_settings, dev_settings):
            return False
    return True
