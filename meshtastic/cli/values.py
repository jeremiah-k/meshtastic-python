"""Pure value parsing and destination helpers for the Meshtastic CLI."""

from __future__ import annotations

import argparse
from typing import Any

import meshtastic.util
from meshtastic import BROADCAST_ADDR, LOCAL_ADDR
from meshtastic.protobuf import config_pb2


def parse_modem_preset_name(value: str) -> str:
    """Normalize and validate a modem preset against the active schema."""
    normalized = value.strip().replace("-", "_").upper()
    try:
        config_pb2.Config.LoRaConfig.ModemPreset.Value(normalized)
    except ValueError as exc:
        choices = ", ".join(config_pb2.Config.LoRaConfig.ModemPreset.keys())
        raise argparse.ArgumentTypeError(
            f"Unknown modem preset {value!r}. Available presets: {choices}"
        ) from exc
    return normalized


def looks_like_integer_literal(value: str) -> bool:
    """Return whether ``value`` begins like a supported signed integer literal."""
    stripped = value.strip()
    if not stripped:
        return False
    if stripped[0] in "+-":
        stripped = stripped[1:]
    return bool(stripped) and stripped[0].isdigit()


def parse_integer_literal(value: str) -> int:
    """Parse decimal, hexadecimal, or binary integer text."""
    stripped = value.strip()
    if not stripped:
        raise ValueError("empty integer literal")
    unsigned = stripped[1:] if stripped[0] in "+-" else stripped
    if unsigned.lower().startswith(("0x", "0b")):
        return int(stripped, 0)
    return int(stripped, 10)


def parse_bitfield_value(flag_type: Any, raw_value: Any) -> int:
    """Parse an integer or comma-separated protobuf flag-name bitfield."""
    if isinstance(raw_value, int):
        value = raw_value
    elif isinstance(raw_value, str):
        stripped = raw_value.strip()
        if looks_like_integer_literal(stripped):
            try:
                value = parse_integer_literal(stripped)
            except ValueError as exc:
                raise ValueError(
                    f"Invalid numeric bitfield value {raw_value!r}. Expected decimal, "
                    "hex with 0x prefix, binary with 0b prefix, or comma-separated flag names."
                ) from exc
        else:
            flag_names = [name.strip() for name in stripped.split(",") if name.strip()]
            value = meshtastic.util.flagsFromList(flag_type, flag_names)
    else:
        raise ValueError(
            f"Invalid bitfield value {raw_value!r}. Expected integer, numeric string, or flag names."
        )

    if value < 0:
        raise ValueError(
            f"Invalid bitfield value {raw_value!r}. Expected a non-negative integer."
        )
    return value


def _parse_destination_node_number(value: str) -> int | None:
    """Parse decimal, ``!hex``, or ``0xhex`` node-number forms."""
    if value.isdecimal():
        return int(value)
    normalized = value.casefold()
    if normalized.startswith("!"):
        hex_part = normalized[1:]
    elif normalized.startswith("0x"):
        hex_part = normalized[2:]
    else:
        return None
    if not hex_part:
        return None
    try:
        return int(hex_part, 16)
    except ValueError:
        return None


def is_local_destination(interface: Any, destination: str) -> bool:
    """Return whether a destination identifies the directly connected local node."""
    destination_value = str(destination).strip()
    if destination_value in (BROADCAST_ADDR, LOCAL_ADDR):
        return True

    try:
        my_info = interface.myInfo
        if my_info is None:
            return False
        my_node_num = int(my_info.my_node_num)
    except (AttributeError, TypeError, ValueError):
        return False

    return _parse_destination_node_number(destination_value) == my_node_num
