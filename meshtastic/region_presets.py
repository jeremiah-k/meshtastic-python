"""Helpers for firmware-declared LoRa region and modem-preset compatibility."""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping

from meshtastic.protobuf import mesh_pb2


@dataclass(frozen=True, slots=True)
class RegionPresetInfo:
    """One firmware-declared region compatibility profile."""

    presets: tuple[int, ...]
    default_preset: int
    licensed_only: bool


def decode_region_preset_map(
    mapping: mesh_pb2.LoRaRegionPresetMap,
) -> Mapping[int, RegionPresetInfo]:
    """Flatten the grouped wire format into an immutable region lookup.

    Malformed entries are ignored rather than converted into restrictions:
    an absent/invalid entry means the client has no compatibility information
    for that region and must retain its unconstrained legacy behavior.
    """

    decoded: dict[int, RegionPresetInfo] = {}
    groups = mapping.groups
    for region_group in mapping.region_groups:
        group_index = int(region_group.group_index)
        if group_index < 0 or group_index >= len(groups):
            continue
        group = groups[group_index]
        presets = tuple(dict.fromkeys(int(value) for value in group.presets))
        default_preset = int(group.default_preset)
        if not presets or default_preset not in presets:
            continue
        decoded[int(region_group.region)] = RegionPresetInfo(
            presets=presets,
            default_preset=default_preset,
            licensed_only=bool(group.licensed_only),
        )
    return MappingProxyType(decoded)
