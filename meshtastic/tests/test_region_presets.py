"""Tests for firmware-declared region/preset compatibility metadata."""

from types import MappingProxyType

import pytest

from meshtastic.mesh_interface import MeshInterface
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


@pytest.mark.unit
def test_mesh_interface_region_preset_state_and_aliases() -> None:
    info = RegionPresetInfo(
        presets=(
            config_pb2.Config.LoRaConfig.LONG_FAST,
            config_pb2.Config.LoRaConfig.LONG_SLOW,
        ),
        default_preset=config_pb2.Config.LoRaConfig.LONG_FAST,
        licensed_only=False,
    )

    with MeshInterface(noProto=True) as interface:
        assert interface.regionPresetMap is None
        assert dict(interface.regionPresets) == {}
        interface.regionPresets = MappingProxyType(
            {config_pb2.Config.LoRaConfig.US: info}
        )

        assert interface.getRegionPresetInfo(config_pb2.Config.LoRaConfig.US) == info
        assert interface.get_region_preset_info(config_pb2.Config.LoRaConfig.US) == info
        assert (
            interface.getAllowedModemPresets(config_pb2.Config.LoRaConfig.US)
            == info.presets
        )
        assert (
            interface.get_allowed_modem_presets(config_pb2.Config.LoRaConfig.US)
            == info.presets
        )
        assert (
            interface.getRegionPresetInfo(config_pb2.Config.LoRaConfig.EU_868) is None
        )
        assert (
            interface.getAllowedModemPresets(config_pb2.Config.LoRaConfig.EU_868)
            is None
        )
        assert (
            interface.getRegionPresetInfo(str(config_pb2.Config.LoRaConfig.US)) == info
        )  # type: ignore[arg-type]
        assert interface.getRegionPresetInfo(None) is None  # type: ignore[arg-type]
        assert interface.getRegionPresetInfo("not-a-region") is None  # type: ignore[arg-type]
        with pytest.raises(TypeError):
            interface.regionPresets[config_pb2.Config.LoRaConfig.EU_868] = info  # type: ignore[index]


@pytest.mark.unit
def test_decode_region_preset_map_skips_empty_groups_and_last_region_entry_wins() -> (
    None
):
    mapping = mesh_pb2.LoRaRegionPresetMap()
    mapping.groups.add()  # empty group must not become an empty deny-list

    first = mapping.groups.add()
    first.presets.append(config_pb2.Config.LoRaConfig.LONG_FAST)
    first.default_preset = config_pb2.Config.LoRaConfig.LONG_FAST

    second = mapping.groups.add()
    second.presets.append(config_pb2.Config.LoRaConfig.LONG_SLOW)
    second.default_preset = config_pb2.Config.LoRaConfig.LONG_SLOW

    empty = mapping.region_groups.add()
    empty.region = config_pb2.Config.LoRaConfig.EU_868
    empty.group_index = 0
    earlier = mapping.region_groups.add()
    earlier.region = config_pb2.Config.LoRaConfig.US
    earlier.group_index = 1
    later = mapping.region_groups.add()
    later.region = config_pb2.Config.LoRaConfig.US
    later.group_index = 2

    decoded = decode_region_preset_map(mapping)

    assert config_pb2.Config.LoRaConfig.EU_868 not in decoded
    assert decoded[config_pb2.Config.LoRaConfig.US].presets == (
        config_pb2.Config.LoRaConfig.LONG_SLOW,
    )
