from __future__ import annotations

import base64

import pytest

from meshtastic.configure_verify import (
    _verify_channel_url_match,
    _verify_requested_fields,
)
from meshtastic.protobuf import apponly_pb2, channel_pb2, config_pb2, localonly_pb2


def _make_channel_url(settings_list: list[channel_pb2.ChannelSettings]) -> str:
    cs = apponly_pb2.ChannelSet()
    for s in settings_list:
        cs.settings.add().CopyFrom(s)
    raw = cs.SerializeToString()
    b64 = base64.b64encode(raw, altchars=b"-_").decode().rstrip("=")
    return f"https://meshtastic.org/e/#{b64}"


@pytest.mark.unit
def test_verify_fields_exact_scalar_match() -> None:
    proto = localonly_pb2.LocalConfig()
    proto.lora.hop_limit = 3
    assert _verify_requested_fields({"hop_limit": 3}, proto.lora, "lora") == []


@pytest.mark.unit
def test_verify_fields_enum_string_match() -> None:
    proto = localonly_pb2.LocalConfig()
    proto.lora.region = config_pb2.Config.LoRaConfig.RegionCode.Value("US")
    assert _verify_requested_fields({"region": "US"}, proto.lora, "lora") == []


@pytest.mark.unit
def test_verify_fields_enum_mismatch() -> None:
    proto = localonly_pb2.LocalConfig()
    proto.lora.region = config_pb2.Config.LoRaConfig.RegionCode.Value("US")
    result = _verify_requested_fields({"region": "EU_868"}, proto.lora, "lora")
    assert result == ["lora.region"]


@pytest.mark.unit
def test_verify_fields_missing_descriptor() -> None:
    proto = localonly_pb2.LocalConfig()
    result = _verify_requested_fields({"nonexistent_field": 42}, proto.lora, "lora")
    assert result == ["lora.nonexistent_field"]


@pytest.mark.unit
def test_verify_fields_nested_dict() -> None:
    proto = localonly_pb2.LocalConfig()
    proto.device.serial_enabled = True
    assert (
        _verify_requested_fields({"device": {"serial_enabled": True}}, proto, "") == []
    )


@pytest.mark.unit
def test_verify_fields_bytes_match() -> None:
    proto = localonly_pb2.LocalConfig()
    proto.security.private_key = b"\x01\x02"
    assert (
        _verify_requested_fields(
            {"private_key": "base64:AQI="}, proto.security, "security"
        )
        == []
    )


@pytest.mark.unit
def test_verify_fields_value_mismatch() -> None:
    proto = localonly_pb2.LocalConfig()
    proto.lora.hop_limit = 3
    result = _verify_requested_fields({"hop_limit": 5}, proto.lora, "lora")
    assert result == ["lora.hop_limit"]


@pytest.mark.unit
def test_verify_fields_repeated_match() -> None:
    proto = localonly_pb2.LocalConfig()
    proto.security.admin_key.extend([b"\x01", b"\x02"])
    assert (
        _verify_requested_fields(
            {"admin_key": [b"\x01", b"\x02"]}, proto.security, "security"
        )
        == []
    )


@pytest.mark.unit
def test_verify_fields_bool_coercion() -> None:
    proto = localonly_pb2.LocalConfig()
    proto.bluetooth.enabled = True
    assert (
        _verify_requested_fields({"enabled": "true"}, proto.bluetooth, "bluetooth")
        == []
    )


@pytest.mark.unit
def test_channel_url_matching_urls() -> None:
    s = channel_pb2.ChannelSettings()
    s.name = "test"
    s.psk = b"\x01\x02\x03"
    url = _make_channel_url([s])
    assert _verify_channel_url_match(url, url) is True


@pytest.mark.unit
def test_channel_url_different_psk() -> None:
    s1 = channel_pb2.ChannelSettings()
    s1.name = "test"
    s1.psk = b"\x01\x02\x03"
    s2 = channel_pb2.ChannelSettings()
    s2.name = "test"
    s2.psk = b"\xff\xff\xff"
    assert (
        _verify_channel_url_match(_make_channel_url([s1]), _make_channel_url([s2]))
        is False
    )


@pytest.mark.unit
def test_channel_url_device_has_extra_channels() -> None:
    s1 = channel_pb2.ChannelSettings()
    s1.name = "test"
    s1.psk = b"\x01\x02\x03"
    s2 = channel_pb2.ChannelSettings()
    s2.name = "extra"
    s2.psk = b"\xaa\xbb"
    assert (
        _verify_channel_url_match(_make_channel_url([s1]), _make_channel_url([s1, s2]))
        is False
    )


@pytest.mark.unit
def test_channel_url_missing_channel_in_device() -> None:
    s1 = channel_pb2.ChannelSettings()
    s1.name = "test"
    s1.psk = b"\x01\x02\x03"
    s2 = channel_pb2.ChannelSettings()
    s2.name = "extra"
    s2.psk = b"\xaa\xbb"
    assert (
        _verify_channel_url_match(_make_channel_url([s1, s2]), _make_channel_url([s1]))
        is False
    )


@pytest.mark.unit
def test_channel_url_invalid_requested_url() -> None:
    s = channel_pb2.ChannelSettings()
    s.name = "test"
    s.psk = b"\x01"
    assert _verify_channel_url_match("not-a-valid-url", _make_channel_url([s])) is False


@pytest.mark.unit
def test_channel_url_invalid_device_url() -> None:
    s = channel_pb2.ChannelSettings()
    s.name = "test"
    s.psk = b"\x01"
    assert _verify_channel_url_match(_make_channel_url([s]), "garbage") is False


@pytest.mark.unit
def test_verify_fields_repeated_scalar_coerced_to_list() -> None:
    proto = localonly_pb2.LocalConfig()
    proto.security.admin_key.append(b"\x01")
    result = _verify_requested_fields(
        {"admin_key": b"\x01"}, proto.security, "security"
    )
    assert result == []


@pytest.mark.unit
def test_verify_fields_repeated_list_order_mismatch() -> None:
    proto = localonly_pb2.LocalConfig()
    proto.security.admin_key.extend([b"\x01", b"\x02"])
    result = _verify_requested_fields(
        {"admin_key": [b"\x02", b"\x01"]}, proto.security, "security"
    )
    assert result == ["security.admin_key"]


@pytest.mark.unit
def test_verify_fields_non_repeated_given_list_uses_first() -> None:
    proto = localonly_pb2.LocalConfig()
    proto.lora.hop_limit = 3
    result = _verify_requested_fields({"hop_limit": [3, 5]}, proto.lora, "lora")
    assert result == []


@pytest.mark.unit
def test_verify_fields_non_repeated_given_wrong_list() -> None:
    proto = localonly_pb2.LocalConfig()
    proto.lora.hop_limit = 3
    result = _verify_requested_fields({"hop_limit": [5]}, proto.lora, "lora")
    assert result == ["lora.hop_limit"]


@pytest.mark.unit
def test_verify_fields_invalid_enum_name_treated_as_mismatch() -> None:
    proto = localonly_pb2.LocalConfig()
    proto.lora.region = config_pb2.Config.LoRaConfig.RegionCode.Value("US")
    result = _verify_requested_fields({"region": "INVALID_REGION"}, proto.lora, "lora")
    assert result == ["lora.region"]


@pytest.mark.unit
def test_channel_url_name_mismatch() -> None:
    s1 = channel_pb2.ChannelSettings()
    s1.name = "alpha"
    s1.psk = b"\x01\x02\x03"
    s2 = channel_pb2.ChannelSettings()
    s2.name = "alpha"
    s2.psk = b"\x01\x02\x03"
    s2.uplink_enabled = False
    s3 = channel_pb2.ChannelSettings()
    s3.name = "alpha"
    s3.psk = b"\x01\x02\x03"
    s3.uplink_enabled = True
    assert (
        _verify_channel_url_match(_make_channel_url([s2]), _make_channel_url([s3]))
        is False
    )


@pytest.mark.unit
def test_channel_url_uplink_mismatch() -> None:
    s1 = channel_pb2.ChannelSettings()
    s1.name = "test"
    s1.psk = b"\x01"
    s1.uplink_enabled = True
    s2 = channel_pb2.ChannelSettings()
    s2.name = "test"
    s2.psk = b"\x01"
    s2.uplink_enabled = False
    assert (
        _verify_channel_url_match(_make_channel_url([s1]), _make_channel_url([s2]))
        is False
    )


@pytest.mark.unit
def test_channel_url_downlink_mismatch() -> None:
    s1 = channel_pb2.ChannelSettings()
    s1.name = "test"
    s1.psk = b"\x01"
    s1.downlink_enabled = True
    s2 = channel_pb2.ChannelSettings()
    s2.name = "test"
    s2.psk = b"\x01"
    s2.downlink_enabled = False
    assert (
        _verify_channel_url_match(_make_channel_url([s1]), _make_channel_url([s2]))
        is False
    )


@pytest.mark.unit
def test_channel_url_id_mismatch() -> None:
    s1 = channel_pb2.ChannelSettings()
    s1.name = "test"
    s1.psk = b"\x01"
    s1.id = 12345
    s2 = channel_pb2.ChannelSettings()
    s2.name = "test"
    s2.psk = b"\x01"
    s2.id = 99999
    assert (
        _verify_channel_url_match(_make_channel_url([s1]), _make_channel_url([s2]))
        is False
    )


@pytest.mark.unit
def test_channel_url_module_settings_mismatch() -> None:
    s1 = channel_pb2.ChannelSettings()
    s1.name = "test"
    s1.psk = b"\x01"
    s1.module_settings.position_precision = 10
    s2 = channel_pb2.ChannelSettings()
    s2.name = "test"
    s2.psk = b"\x01"
    s2.module_settings.position_precision = 5
    assert (
        _verify_channel_url_match(_make_channel_url([s1]), _make_channel_url([s2]))
        is False
    )


@pytest.mark.unit
def test_channel_url_module_settings_is_muted_mismatch() -> None:
    s1 = channel_pb2.ChannelSettings()
    s1.name = "test"
    s1.psk = b"\x01"
    s1.module_settings.is_muted = True
    s2 = channel_pb2.ChannelSettings()
    s2.name = "test"
    s2.psk = b"\x01"
    s2.module_settings.is_muted = False
    assert (
        _verify_channel_url_match(_make_channel_url([s1]), _make_channel_url([s2]))
        is False
    )


@pytest.mark.unit
def test_channel_url_all_fields_match() -> None:
    s1 = channel_pb2.ChannelSettings()
    s1.name = "full"
    s1.psk = b"\xaa\xbb\xcc"
    s1.id = 42
    s1.uplink_enabled = True
    s1.downlink_enabled = False
    s1.module_settings.position_precision = 8
    s1.module_settings.is_muted = True
    s2 = channel_pb2.ChannelSettings()
    s2.name = "full"
    s2.psk = b"\xaa\xbb\xcc"
    s2.id = 42
    s2.uplink_enabled = True
    s2.downlink_enabled = False
    s2.module_settings.position_precision = 8
    s2.module_settings.is_muted = True
    assert (
        _verify_channel_url_match(_make_channel_url([s1]), _make_channel_url([s2]))
        is True
    )


@pytest.mark.unit
def test_channel_url_duplicate_requested_names_returns_false() -> None:
    s1 = channel_pb2.ChannelSettings()
    s1.name = "dup"
    s1.psk = b"\x01"
    s2 = channel_pb2.ChannelSettings()
    s2.name = "dup"
    s2.psk = b"\x02"
    s3 = channel_pb2.ChannelSettings()
    s3.name = "dup"
    s3.psk = b"\x01"
    assert (
        _verify_channel_url_match(_make_channel_url([s1, s2]), _make_channel_url([s3]))
        is False
    )


@pytest.mark.unit
def test_channel_url_duplicate_device_names_returns_false() -> None:
    s1 = channel_pb2.ChannelSettings()
    s1.name = "dup"
    s1.psk = b"\x01"
    s2 = channel_pb2.ChannelSettings()
    s2.name = "dup"
    s2.psk = b"\x02"
    assert (
        _verify_channel_url_match(_make_channel_url([s1]), _make_channel_url([s1, s2]))
        is False
    )


@pytest.mark.unit
def test_channel_url_duplicate_names_both_sides_returns_false() -> None:
    s1 = channel_pb2.ChannelSettings()
    s1.name = "same"
    s1.psk = b"\x01"
    s2 = channel_pb2.ChannelSettings()
    s2.name = "same"
    s2.psk = b"\x01"
    assert (
        _verify_channel_url_match(
            _make_channel_url([s1, s2]), _make_channel_url([s1, s2])
        )
        is False
    )


@pytest.mark.unit
def test_channel_url_no_duplicates_still_works() -> None:
    s1 = channel_pb2.ChannelSettings()
    s1.name = "alpha"
    s1.psk = b"\x01"
    s2 = channel_pb2.ChannelSettings()
    s2.name = "beta"
    s2.psk = b"\x02"
    s3 = channel_pb2.ChannelSettings()
    s3.name = "alpha"
    s3.psk = b"\x01"
    s4 = channel_pb2.ChannelSettings()
    s4.name = "beta"
    s4.psk = b"\x02"
    assert (
        _verify_channel_url_match(
            _make_channel_url([s1, s2]), _make_channel_url([s3, s4])
        )
        is True
    )


@pytest.mark.unit
def test_channel_url_duplicate_requested_with_mismatch_settings_returns_false() -> None:
    s1 = channel_pb2.ChannelSettings()
    s1.name = "dup"
    s1.psk = b"\x01"
    s2 = channel_pb2.ChannelSettings()
    s2.name = "dup"
    s2.psk = b"\xff"
    s3 = channel_pb2.ChannelSettings()
    s3.name = "dup"
    s3.psk = b"\x01"
    assert (
        _verify_channel_url_match(_make_channel_url([s1, s2]), _make_channel_url([s3]))
        is False
    )
