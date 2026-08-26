"""Regression tests for repeated-submessage ``--set`` preference values.

The generic ``--set`` path lives in :mod:`meshtastic.cli.preference_runtime`
and historically only understood scalar and repeated-scalar protobuf fields.
These tests pin the new repeated-submessage behaviour, which lets a user
populate fields such as ``module.mesh_beacon.broadcast_targets`` with a JSON
array literal in a single ``--set`` token.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from meshtastic.__main__ import setPref
from meshtastic.protobuf import config_pb2, localonly_pb2, module_config_pb2


@pytest.fixture(autouse=True)
def _quiet_mt_config(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Force ``setPref`` to use the public CLI printer and bypass preflight."""
    # Direct ``setPref`` exercises the same diagnostic surface as ``--set``;
    # we deliberately avoid ``reset_mt_config`` because these tests don't
    # touch argparse state.
    import meshtastic.cli.preference_runtime as pr

    token = pr.CONFIGURE_PREFLIGHT_MODE.set(False)
    monkeypatch.setattr(pr, "CONFIGURE_PREFLIGHT_MODE", pr.CONFIGURE_PREFLIGHT_MODE)
    try:
        yield
    finally:
        pr.CONFIGURE_PREFLIGHT_MODE.reset(token)


@pytest.mark.unit
def test_json_array_builds_repeated_broadcast_targets() -> None:
    """A JSON array of objects populates ``broadcast_targets`` via ParseDict."""
    config = localonly_pb2.LocalModuleConfig()
    payload = (
        '[{"preset":"SHORT_FAST","region":"US","channel_index":1},'
        '{"preset":"MEDIUM_FAST","channel_index":2}]'
    )

    assert setPref(config, "mesh_beacon.broadcast_targets", payload) is True

    targets = config.mesh_beacon.broadcast_targets
    assert len(targets) == 2
    assert targets[0].preset == config_pb2.Config.LoRaConfig.ModemPreset.SHORT_FAST
    assert targets[0].region == config_pb2.Config.LoRaConfig.RegionCode.US
    assert targets[0].channel_index == 1
    assert targets[1].preset == config_pb2.Config.LoRaConfig.ModemPreset.MEDIUM_FAST
    assert targets[1].channel_index == 2
    # Defaults are applied for omitted fields, so region == 0 (UNSET) for the
    # second entry which did not specify one.
    assert targets[1].region == config_pb2.Config.LoRaConfig.RegionCode.Value("UNSET")


@pytest.mark.unit
def test_empty_array_clears_repeated_broadcast_targets() -> None:
    """An empty JSON array clears an already-populated repeated-submessage field."""
    config = localonly_pb2.LocalModuleConfig()
    seed = '[{"preset":"SHORT_FAST","channel_index":1}]'
    assert setPref(config, "mesh_beacon.broadcast_targets", seed) is True
    assert len(config.mesh_beacon.broadcast_targets) == 1

    assert setPref(config, "mesh_beacon.broadcast_targets", "[]") is True

    assert len(config.mesh_beacon.broadcast_targets) == 0


@pytest.mark.unit
@pytest.mark.parametrize(
    ("payload", "expected_substring"),
    (
        ("{not valid json", "Invalid value"),
        ('"just a string"', "Invalid value"),
        ('{"preset":"SHORT_FAST"}', "Invalid value"),
        ('["not an object"]', "is not a JSON object"),
    ),
)
def test_malformed_json_uses_same_validation_surface(
    payload: str,
    expected_substring: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Bad JSON payloads report through the standard ``setPref`` diagnostic."""
    config = localonly_pb2.LocalModuleConfig()
    seed = '[{"preset":"SHORT_FAST","channel_index":7}]'
    assert setPref(config, "mesh_beacon.broadcast_targets", seed) is True
    before = config.SerializeToString()

    assert setPref(config, "mesh_beacon.broadcast_targets", payload) is False

    out, err = capsys.readouterr()
    combined = out + err
    assert expected_substring in combined
    # The repeated-submessage field must not be cleared or partially mutated.
    assert config.SerializeToString() == before


@pytest.mark.unit
def test_unknown_key_inside_element_reports_element_diagnostic(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """An unknown key inside an object surfaces a per-element diagnostic."""
    config = localonly_pb2.LocalModuleConfig()
    seed = '[{"preset":"SHORT_FAST","channel_index":7}]'
    assert setPref(config, "mesh_beacon.broadcast_targets", seed) is True
    before = config.SerializeToString()

    assert (
        setPref(
            config,
            "mesh_beacon.broadcast_targets",
            '[{"bogus":"foo"}]',
        )
        is False
    )

    out, err = capsys.readouterr()
    combined = out + err
    assert "element 0" in combined
    assert config.SerializeToString() == before


@pytest.mark.unit
def test_repeated_scalar_field_accepts_comma_separated_string() -> None:
    """A comma-separated token for a repeated scalar field parses per-element."""
    config = localonly_pb2.LocalConfig()

    assert setPref(config, "lora.ignore_incoming", "11, 22, 33") is True

    assert list(config.lora.ignore_incoming) == [11, 22, 33]


@pytest.mark.unit
def test_repeated_scalar_ignores_empty_comma_elements() -> None:
    """Accidental duplicate separators do not synthesize invalid scalar values."""
    config = localonly_pb2.LocalConfig()

    assert setPref(config, "lora.ignore_incoming", "11,, 22, ,33") is True

    assert list(config.lora.ignore_incoming) == [11, 22, 33]


@pytest.mark.unit
def test_repeated_scalar_list_path_still_works() -> None:
    """Passing a Python list to a repeated scalar field keeps historical semantics."""
    config = localonly_pb2.LocalConfig()

    assert setPref(config, "lora.ignore_incoming", ["4", "8"]) is True

    assert list(config.lora.ignore_incoming) == [4, 8]


@pytest.mark.unit
def test_non_repeated_field_unchanged_by_repeated_message_support() -> None:
    """Setting a non-repeated field still routes through the scalar path."""
    config = localonly_pb2.LocalConfig()

    assert setPref(config, "lora.hop_limit", "5") is True

    assert config.lora.hop_limit == 5
    # Golden value for "the same field set with the historical contract".
    golden = localonly_pb2.LocalConfig()
    golden.lora.hop_limit = 5
    assert config.lora == golden.lora


@pytest.mark.unit
def test_broadcast_target_round_trip_against_real_protos() -> None:
    """Round-trip a BroadcastTarget list through the real ModuleConfig protobuf."""
    target = module_config_pb2.ModuleConfig.MeshBeaconConfig.BroadcastTarget
    preset_enum = config_pb2.Config.LoRaConfig.ModemPreset
    region_enum = config_pb2.Config.LoRaConfig.RegionCode
    payload = (
        "["
        '{"preset":"LONG_FAST","region":"US","channel_index":1},'
        '{"preset":"SHORT_FAST","region":"EU_433","channel_index":3}'
        "]"
    )

    config = localonly_pb2.LocalModuleConfig()

    assert setPref(config, "mesh_beacon.broadcast_targets", payload) is True

    bt = list(config.mesh_beacon.broadcast_targets)
    assert len(bt) == 2
    assert isinstance(bt[0], target)
    assert bt[0].preset == preset_enum.Value("LONG_FAST")

    assert bt[0].region == region_enum.US
    assert bt[0].channel_index == 1
    assert bt[1].preset == preset_enum.SHORT_FAST
    assert bt[1].region == region_enum.EU_433
    assert bt[1].channel_index == 3
