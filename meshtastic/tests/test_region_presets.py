"""Tests for firmware-declared region/preset compatibility metadata."""

from types import MappingProxyType

import pytest

from meshtastic.protobuf import config_pb2, mesh_pb2
from meshtastic.region_presets import RegionPresetInfo, decode_region_preset_map


@pytest.mark.unit
def test_decode_region_preset_map_flattens_groups_and_deduplicates_presets() -> None:
    mapping = mesh_pb2.LoRaRegionPresetMap()
    group = mapping.groups.add()
    group.presets.extend(
        [
            config_pb2.Config.LoRaConfig.LONG_FAST,
            config_pb2.Config.LoRaConfig.LONG_FAST,
            config_pb2.Config.LoRaConfig.LONG_SLOW,
        ]
    )
    group.default_preset = config_pb2.Config.LoRaConfig.LONG_FAST
    group.licensed_only = True
    region_group = mapping.region_groups.add()
    region_group.region = config_pb2.Config.LoRaConfig.US
    region_group.group_index = 0

    decoded = decode_region_preset_map(mapping)

    assert isinstance(decoded, MappingProxyType)
    assert decoded[config_pb2.Config.LoRaConfig.US] == RegionPresetInfo(
        presets=(
            config_pb2.Config.LoRaConfig.LONG_FAST,
            config_pb2.Config.LoRaConfig.LONG_SLOW,
        ),
        default_preset=config_pb2.Config.LoRaConfig.LONG_FAST,
        licensed_only=True,
    )


@pytest.mark.unit
def test_decode_region_preset_map_skips_malformed_entries_without_restricting() -> None:
    mapping = mesh_pb2.LoRaRegionPresetMap()
    invalid_default = mapping.groups.add()
    invalid_default.presets.append(config_pb2.Config.LoRaConfig.LONG_SLOW)
    invalid_default.default_preset = config_pb2.Config.LoRaConfig.LONG_FAST

    bad_group = mapping.region_groups.add()
    bad_group.region = config_pb2.Config.LoRaConfig.US
    bad_group.group_index = 99
    bad_default = mapping.region_groups.add()
    bad_default.region = config_pb2.Config.LoRaConfig.EU_868
    bad_default.group_index = 0

    assert dict(decode_region_preset_map(mapping)) == {}
