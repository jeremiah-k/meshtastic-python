"""Focused tests for pure CLI value helpers."""

import argparse
from types import SimpleNamespace

import pytest

from meshtastic import BROADCAST_ADDR, LOCAL_ADDR
from meshtastic.cli.values import (
    is_local_destination,
    looks_like_integer_literal,
    parse_bitfield_value,
    parse_integer_literal,
    parse_modem_preset_name,
)
from meshtastic.protobuf import config_pb2


@pytest.mark.unit
@pytest.mark.parametrize("value", ["0", "-2", "+3", "0x10", "0b11"])
def test_integer_literal_detection_accepts_supported_forms(value: str) -> None:
    assert looks_like_integer_literal(value)


@pytest.mark.unit
@pytest.mark.parametrize("value", ["", "  ", "+", "-", "LONG_FAST"])
def test_integer_literal_detection_rejects_non_numeric_forms(value: str) -> None:
    assert not looks_like_integer_literal(value)


@pytest.mark.unit
@pytest.mark.parametrize(
    ("value", "expected"),
    [("10", 10), ("-10", -10), ("0x10", 16), ("-0x10", -16), ("0b11", 3)],
)
def test_parse_integer_literal(value: str, expected: int) -> None:
    assert parse_integer_literal(value) == expected


@pytest.mark.unit
def test_parse_integer_literal_rejects_empty_text() -> None:
    with pytest.raises(ValueError, match="empty integer literal"):
        parse_integer_literal(" ")


@pytest.mark.unit
def test_parse_modem_preset_name_normalizes_schema_name() -> None:
    assert parse_modem_preset_name("long-fast") == "LONG_FAST"


@pytest.mark.unit
def test_parse_modem_preset_name_lists_choices_on_error() -> None:
    with pytest.raises(argparse.ArgumentTypeError, match="Available presets"):
        parse_modem_preset_name("not-real")


@pytest.mark.unit
def test_parse_bitfield_value_accepts_numbers_and_names() -> None:
    enum = config_pb2.Config.DisplayConfig.OledType
    # Numeric inputs
    assert parse_bitfield_value(enum, 3) == 3
    assert parse_bitfield_value(enum, "0x3") == 3
    # Comma-separated flag names (exercises flagsFromList path)
    # OLED_SSD1306=1, OLED_SH1106=2 → combined = 3
    assert parse_bitfield_value(enum, "OLED_SSD1306,OLED_SH1106") == 3


@pytest.mark.unit
@pytest.mark.parametrize("value", [-1, "-1", object()])
def test_parse_bitfield_value_rejects_invalid_values(value: object) -> None:
    with pytest.raises(ValueError, match="Invalid bitfield value"):
        parse_bitfield_value(config_pb2.Config.DisplayConfig.OledType, value)


@pytest.mark.unit
@pytest.mark.parametrize("destination", [BROADCAST_ADDR, LOCAL_ADDR, "!25d6e474", "0x25D6E474", "634840180"])
def test_is_local_destination_accepts_supported_forms(destination: str) -> None:
    interface = SimpleNamespace(myInfo=SimpleNamespace(my_node_num=int("25d6e474", 16)))
    assert is_local_destination(interface, destination)


@pytest.mark.unit
@pytest.mark.parametrize("destination", ["!bad", "0x", "123", "!00000001"])
def test_is_local_destination_rejects_other_nodes(destination: str) -> None:
    interface = SimpleNamespace(myInfo=SimpleNamespace(my_node_num=int("25d6e474", 16)))
    assert not is_local_destination(interface, destination)


@pytest.mark.unit
def test_is_local_destination_handles_missing_or_invalid_local_info() -> None:
    assert not is_local_destination(SimpleNamespace(myInfo=None), "123")
    assert not is_local_destination(SimpleNamespace(myInfo=object()), "123")
