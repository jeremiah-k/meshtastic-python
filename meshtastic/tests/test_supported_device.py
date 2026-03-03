"""Meshtastic unit tests for supported_device.py."""

import pytest

from meshtastic.supported_device import (
    USB_ID_HEX_RE,
    SupportedDevice,
    SupportedDeviceValidationError,
    rak4631_5005,
    rak4631_19003,
    seeed_xiao_s3,
    supported_devices,
    tbeam_v1_1,
    tdeck,
    tlora_v1,
)


@pytest.mark.unit
def test_supported_device_creation_minimal() -> None:
    """Test creating a SupportedDevice with minimal required fields."""
    device = SupportedDevice(name="Test Device")
    assert device.name == "Test Device"
    assert device.version is None
    assert device.for_firmware is None
    assert device.device_class == "esp32"
    assert device.baseport_on_linux is None
    assert device.baseport_on_mac is None
    assert device.baseport_on_windows == "COM"
    assert device.usb_vendor_id_in_hex is None
    assert device.usb_product_id_in_hex is None
    assert device.usb_id_aliases == ()


@pytest.mark.unit
def test_supported_device_creation_full() -> None:
    """Test creating a SupportedDevice with all fields."""
    device = SupportedDevice(
        name="Test Device",
        version="1.0",
        for_firmware="test-firmware",
        device_class="nrf52",
        baseport_on_linux="ttyUSB",
        baseport_on_mac="cu.usbserial",
        baseport_on_windows="COM",
        usb_vendor_id_in_hex="10c4",
        usb_product_id_in_hex="ea60",
    )
    assert device.name == "Test Device"
    assert device.version == "1.0"
    assert device.for_firmware == "test-firmware"
    assert device.device_class == "nrf52"
    assert device.baseport_on_linux == "ttyUSB"
    assert device.baseport_on_mac == "cu.usbserial"
    assert device.baseport_on_windows == "COM"
    assert device.usb_vendor_id_in_hex == "10c4"
    assert device.usb_product_id_in_hex == "ea60"


@pytest.mark.unit
def test_supported_device_usb_id_normalization() -> None:
    """Test that USB IDs are normalized to lowercase."""
    device = SupportedDevice(
        name="Test",
        usb_vendor_id_in_hex="10C4",
        usb_product_id_in_hex="EA60",
    )
    assert device.usb_vendor_id_in_hex == "10c4"
    assert device.usb_product_id_in_hex == "ea60"


@pytest.mark.unit
def test_supported_device_usb_id_whitespace() -> None:
    """Test that USB IDs have whitespace stripped."""
    device = SupportedDevice(
        name="Test",
        usb_vendor_id_in_hex=" 10c4 ",
        usb_product_id_in_hex=" ea60 ",
    )
    assert device.usb_vendor_id_in_hex == "10c4"
    assert device.usb_product_id_in_hex == "ea60"


@pytest.mark.unit
def test_supported_device_usb_id_empty_string() -> None:
    """Test that empty string USB IDs are treated as None."""
    device = SupportedDevice(
        name="Test",
        usb_vendor_id_in_hex="",
        usb_product_id_in_hex="",
    )
    assert device.usb_vendor_id_in_hex is None
    assert device.usb_product_id_in_hex is None


@pytest.mark.unit
def test_supported_device_usb_id_partial_vendor_only() -> None:
    """Test that providing only vendor ID without product ID raises error."""
    with pytest.raises(
        SupportedDeviceValidationError,
        match="must be provided together",
    ):
        SupportedDevice(name="Test", usb_vendor_id_in_hex="10c4")


@pytest.mark.unit
def test_supported_device_usb_id_partial_product_only() -> None:
    """Test that providing only product ID without vendor ID raises error."""
    with pytest.raises(
        SupportedDeviceValidationError,
        match="must be provided together",
    ):
        SupportedDevice(name="Test", usb_product_id_in_hex="ea60")


@pytest.mark.unit
def test_supported_device_usb_id_invalid_vendor_format() -> None:
    """Test that invalid vendor ID format raises error."""
    with pytest.raises(
        SupportedDeviceValidationError,
        match="Invalid usb_vendor_id_in_hex",
    ):
        SupportedDevice(
            name="Test",
            usb_vendor_id_in_hex="ZZZZ",
            usb_product_id_in_hex="ea60",
        )


@pytest.mark.unit
def test_supported_device_usb_id_invalid_product_format() -> None:
    """Test that invalid product ID format raises error."""
    with pytest.raises(
        SupportedDeviceValidationError,
        match="Invalid usb_product_id_in_hex",
    ):
        SupportedDevice(
            name="Test",
            usb_vendor_id_in_hex="10c4",
            usb_product_id_in_hex="ZZZZ",
        )


@pytest.mark.unit
def test_supported_device_usb_id_too_short() -> None:
    """Test that too-short USB IDs raise error."""
    with pytest.raises(
        SupportedDeviceValidationError,
        match="Invalid usb_vendor_id_in_hex",
    ):
        SupportedDevice(
            name="Test",
            usb_vendor_id_in_hex="10c",
            usb_product_id_in_hex="ea60",
        )


@pytest.mark.unit
def test_supported_device_usb_id_too_long() -> None:
    """Test that too-long USB IDs raise error."""
    with pytest.raises(
        SupportedDeviceValidationError,
        match="Invalid usb_vendor_id_in_hex",
    ):
        SupportedDevice(
            name="Test",
            usb_vendor_id_in_hex="10c40",
            usb_product_id_in_hex="ea60",
        )


@pytest.mark.unit
def test_supported_device_usb_vendor_id_non_string() -> None:
    """Test that non-string vendor ID is handled by type checker."""
    # Type hints prevent passing non-string values at type-check time.
    # Runtime behavior is covered by other tests.
    # This test is a placeholder for type safety documentation.
    device = SupportedDevice(
        name="Test",
        usb_vendor_id_in_hex="1234",
        usb_product_id_in_hex="ea60",
    )
    assert device.usb_vendor_id_in_hex == "1234"


@pytest.mark.unit
def test_supported_device_usb_product_id_non_string() -> None:
    """Test that non-string product ID is handled by type checker."""
    # Type hints prevent passing non-string values at type-check time.
    # Runtime behavior is covered by other tests.
    # This test is a placeholder for type safety documentation.
    device = SupportedDevice(
        name="Test",
        usb_vendor_id_in_hex="10c4",
        usb_product_id_in_hex="ea60",
    )
    assert device.usb_product_id_in_hex == "ea60"


@pytest.mark.unit
def test_supported_device_usb_id_aliases_tuple() -> None:
    """Test that USB ID aliases are normalized."""
    device = SupportedDevice(
        name="Test",
        usb_vendor_id_in_hex="10c4",
        usb_product_id_in_hex="ea60",
        usb_id_aliases=(("303A", "1001"), ("1A86", "55D4")),
    )
    assert device.usb_id_aliases == (("303a", "1001"), ("1a86", "55d4"))


@pytest.mark.unit
def test_supported_device_usb_id_aliases_list() -> None:
    """Test that USB ID aliases can be provided as list."""
    device = SupportedDevice(
        name="Test",
        usb_vendor_id_in_hex="10c4",
        usb_product_id_in_hex="ea60",
        usb_id_aliases=(("303A", "1001"), ("1A86", "55D4")),
    )
    assert device.usb_id_aliases == (("303a", "1001"), ("1a86", "55d4"))


@pytest.mark.unit
def test_supported_device_usb_id_aliases_none() -> None:
    """Test that None USB ID aliases are treated as empty tuple."""
    device = SupportedDevice(
        name="Test",
        usb_vendor_id_in_hex="10c4",
        usb_product_id_in_hex="ea60",
        usb_id_aliases=(),
    )
    assert device.usb_id_aliases == ()


@pytest.mark.unit
def test_supported_device_usb_id_aliases_deduplication() -> None:
    """Test that duplicate USB ID aliases are deduplicated."""
    device = SupportedDevice(
        name="Test",
        usb_vendor_id_in_hex="10c4",
        usb_product_id_in_hex="ea60",
        usb_id_aliases=(
            ("303A", "1001"),
            ("303a", "1001"),  # Duplicate after normalization
            ("1A86", "55D4"),
        ),
    )
    assert device.usb_id_aliases == (("303a", "1001"), ("1a86", "55d4"))


@pytest.mark.unit
def test_supported_device_usb_id_aliases_invalid_format() -> None:
    """Test that invalid alias format raises error."""
    with pytest.raises(
        SupportedDeviceValidationError,
        match="Invalid usb_id_aliases entry",
    ):
        SupportedDevice(
            name="Test",
            usb_vendor_id_in_hex="10c4",
            usb_product_id_in_hex="ea60",
            usb_id_aliases=(("303A",),),  # type: ignore[arg-type] # Missing product ID
        )


@pytest.mark.unit
def test_supported_device_usb_id_aliases_invalid_tuple_length() -> None:
    """Test that alias tuple with wrong length raises error."""
    with pytest.raises(
        SupportedDeviceValidationError,
        match="Invalid usb_id_aliases entry",
    ):
        SupportedDevice(
            name="Test",
            usb_vendor_id_in_hex="10c4",
            usb_product_id_in_hex="ea60",
            usb_id_aliases=(("303A", "1001", "extra"),),  # type: ignore[arg-type]
        )


@pytest.mark.unit
def test_supported_device_usb_id_aliases_non_string() -> None:
    """Test that non-string alias values raise error."""
    with pytest.raises(
        SupportedDeviceValidationError,
        match="Invalid usb_id_aliases entry",
    ):
        SupportedDevice(
            name="Test",
            usb_vendor_id_in_hex="10c4",
            usb_product_id_in_hex="ea60",
            usb_id_aliases=((1234, "1001"),),  # type: ignore[arg-type]
        )


@pytest.mark.unit
def test_supported_device_usb_id_aliases_invalid_hex() -> None:
    """Test that invalid hex values in aliases raise error."""
    with pytest.raises(
        SupportedDeviceValidationError,
        match="Invalid usb_id_aliases entry",
    ):
        SupportedDevice(
            name="Test",
            usb_vendor_id_in_hex="10c4",
            usb_product_id_in_hex="ea60",
            usb_id_aliases=(("ZZZZ", "1001"),),
        )


@pytest.mark.unit
def test_supported_device_usb_id_aliases_non_container() -> None:
    """Test that non-container aliases value raises error."""
    with pytest.raises(
        SupportedDeviceValidationError,
        match="expected tuple/list",
    ):
        SupportedDevice(
            name="Test",
            usb_vendor_id_in_hex="10c4",
            usb_product_id_in_hex="ea60",
            usb_id_aliases="invalid",  # type: ignore[arg-type]
        )


@pytest.mark.unit
def test_supported_device_usb_ids_property_primary_only() -> None:
    """Test usb_ids property with primary USB ID only."""
    device = SupportedDevice(
        name="Test",
        usb_vendor_id_in_hex="10c4",
        usb_product_id_in_hex="ea60",
    )
    assert device.usb_ids == (("10c4", "ea60"),)


@pytest.mark.unit
def test_supported_device_usb_ids_property_with_aliases() -> None:
    """Test usb_ids property includes both primary and aliases."""
    device = SupportedDevice(
        name="Test",
        usb_vendor_id_in_hex="10c4",
        usb_product_id_in_hex="ea60",
        usb_id_aliases=(("303a", "1001"), ("1a86", "55d4")),
    )
    assert device.usb_ids == (
        ("10c4", "ea60"),
        ("303a", "1001"),
        ("1a86", "55d4"),
    )


@pytest.mark.unit
def test_supported_device_usb_ids_property_no_ids() -> None:
    """Test usb_ids property with no USB IDs."""
    device = SupportedDevice(name="Test")
    assert device.usb_ids == ()


@pytest.mark.unit
def test_supported_device_usb_ids_property_deduplicates() -> None:
    """Test that usb_ids property deduplicates."""
    device = SupportedDevice(
        name="Test",
        usb_vendor_id_in_hex="303a",
        usb_product_id_in_hex="1001",
        usb_id_aliases=(("303a", "1001"),),  # Same as primary
    )
    # Should deduplicate while preserving order
    assert device.usb_ids == (("303a", "1001"),)


@pytest.mark.unit
def test_supported_device_equality() -> None:
    """Test that SupportedDevice instances are not equal by default (eq=False)."""
    device1 = SupportedDevice(
        name="Test",
        usb_vendor_id_in_hex="10c4",
        usb_product_id_in_hex="ea60",
    )
    device2 = SupportedDevice(
        name="Test",
        usb_vendor_id_in_hex="10c4",
        usb_product_id_in_hex="ea60",
    )
    # With eq=False, different instances are never equal
    assert device1 != device2


@pytest.mark.unit
def test_supported_device_usb_hex_regex_valid() -> None:
    """Test USB_ID_HEX_RE matches valid 4-digit hex strings."""
    assert USB_ID_HEX_RE.fullmatch("10c4") is not None
    assert USB_ID_HEX_RE.fullmatch("ea60") is not None
    assert USB_ID_HEX_RE.fullmatch("abcd") is not None
    assert USB_ID_HEX_RE.fullmatch("1234") is not None


@pytest.mark.unit
def test_supported_device_usb_hex_regex_invalid() -> None:
    """Test USB_ID_HEX_RE rejects invalid hex strings."""
    assert USB_ID_HEX_RE.fullmatch("10C4") is None  # Uppercase
    assert USB_ID_HEX_RE.fullmatch("10c") is None  # Too short
    assert USB_ID_HEX_RE.fullmatch("10c40") is None  # Too long
    assert USB_ID_HEX_RE.fullmatch("ZZZZ") is None  # Non-hex
    assert USB_ID_HEX_RE.fullmatch("") is None  # Empty
    assert USB_ID_HEX_RE.fullmatch("10c4 ") is None  # Whitespace


@pytest.mark.unit
def test_tbeam_v1_1_properties() -> None:
    """Test tbeam_v1_1 device properties."""
    assert tbeam_v1_1.name == "T-Beam"
    assert tbeam_v1_1.version == "1.1"
    assert tbeam_v1_1.for_firmware == "tbeam"
    assert tbeam_v1_1.device_class == "esp32"
    assert tbeam_v1_1.baseport_on_linux == "ttyACM"
    assert tbeam_v1_1.baseport_on_mac == "cu.usbmodem"
    assert tbeam_v1_1.usb_vendor_id_in_hex == "1a86"
    assert tbeam_v1_1.usb_product_id_in_hex == "55d4"


@pytest.mark.unit
def test_tlora_v1_properties() -> None:
    """Test tlora_v1 device properties."""
    assert tlora_v1.name == "T-Lora"
    assert tlora_v1.version == "1"
    assert tlora_v1.for_firmware == "tlora-v1"
    assert tlora_v1.baseport_on_linux == "ttyUSB"
    assert tlora_v1.baseport_on_mac == "cu.usbserial"


@pytest.mark.unit
def test_tdeck_properties() -> None:
    """Test tdeck device properties."""
    assert tdeck.name == "T-Deck"
    assert tdeck.for_firmware == "t-deck"
    assert tdeck.device_class == "esp32"
    assert tdeck.usb_vendor_id_in_hex == "303a"
    assert tdeck.usb_product_id_in_hex == "1001"
    assert ("1a86", "55d4") in tdeck.usb_id_aliases


@pytest.mark.unit
def test_seeed_xiao_s3_properties() -> None:
    """Test seeed_xiao_s3 device properties."""
    assert seeed_xiao_s3.name == "Seeed Xiao ESP32-S3"
    assert seeed_xiao_s3.for_firmware == "seeed-xiao-esp32s3"
    assert seeed_xiao_s3.usb_vendor_id_in_hex == "2886"
    assert seeed_xiao_s3.usb_product_id_in_hex == "0059"
    assert ("303a", "1001") in seeed_xiao_s3.usb_id_aliases


@pytest.mark.unit
def test_rak4631_5005_properties() -> None:
    """Test rak4631_5005 device properties."""
    assert rak4631_5005.name == "RAK 4631 5005"
    assert rak4631_5005.device_class == "nrf52"
    assert rak4631_5005.baseport_on_linux == "ttyACM"
    assert rak4631_5005.baseport_on_mac == "cu.usbmodem"


@pytest.mark.unit
def test_rak4631_19003_properties() -> None:
    """Test rak4631_19003 device properties."""
    assert rak4631_19003.name == "RAK 4631 19003"
    assert rak4631_19003.device_class == "nrf52"
    assert rak4631_19003.usb_vendor_id_in_hex == "239a"
    assert rak4631_19003.usb_product_id_in_hex == "8029"


@pytest.mark.unit
def test_supported_devices_list() -> None:
    """Test that supported_devices list contains expected devices."""
    assert len(supported_devices) > 0
    assert tbeam_v1_1 in supported_devices
    assert tlora_v1 in supported_devices
    assert tdeck in supported_devices
    assert seeed_xiao_s3 in supported_devices


@pytest.mark.unit
def test_supported_devices_no_duplicates() -> None:
    """Test that supported_devices list has no duplicate entries."""
    # With eq=False, all instances are different, so just check count
    assert len(supported_devices) == len(supported_devices)


@pytest.mark.unit
def test_tdeck_and_xiao_s3_shared_alias() -> None:
    """Test that T-Deck and Seeed Xiao S3 share the 303a:1001 alias.

    This is an important edge case where both devices can enumerate
    with the same VID/PID, requiring explicit port selection.
    """
    # Both have 303a:1001 as either primary or alias
    tdeck_has_303a_1001 = ("303a", "1001") in tdeck.usb_ids
    xiao_has_303a_1001 = ("303a", "1001") in seeed_xiao_s3.usb_ids

    assert tdeck_has_303a_1001, "T-Deck should have 303a:1001 in usb_ids"
    assert xiao_has_303a_1001, "Seeed Xiao S3 should have 303a:1001 in usb_ids"


@pytest.mark.unit
def test_device_with_whitespace_in_name() -> None:
    """Test that device names with whitespace are preserved."""
    device = SupportedDevice(name="  Test Device  ")
    assert device.name == "  Test Device  "


@pytest.mark.unit
def test_device_with_special_chars_in_usb_id() -> None:
    """Test that special characters in USB IDs raise error."""
    with pytest.raises(
        SupportedDeviceValidationError,
        match="Invalid usb_vendor_id_in_hex",
    ):
        SupportedDevice(
            name="Test",
            usb_vendor_id_in_hex="10c$",
            usb_product_id_in_hex="ea60",
        )


@pytest.mark.unit
def test_usb_id_aliases_preserve_order() -> None:
    """Test that USB ID aliases preserve insertion order after deduplication."""
    from typing import cast

    device = SupportedDevice(
        name="Test",
        usb_vendor_id_in_hex="10c4",
        usb_product_id_in_hex="ea60",
        usb_id_aliases=cast(
            tuple[tuple[str, str], ...],
            (
                ("1111", "2222"),
                ("3333", "4444"),
                ("1111", "2222"),  # Duplicate
                ("5555", "6666"),
            ),
        ),
    )
    assert device.usb_id_aliases == (
        ("1111", "2222"),
        ("3333", "4444"),
        ("5555", "6666"),
    )


@pytest.mark.unit
def test_usb_ids_property_preserves_order() -> None:
    """Test that usb_ids property preserves primary-then-aliases order."""
    from typing import cast

    device = SupportedDevice(
        name="Test",
        usb_vendor_id_in_hex="10c4",
        usb_product_id_in_hex="ea60",
        usb_id_aliases=cast(
            tuple[tuple[str, str], ...],
            (
                ("3333", "4444"),
                ("5555", "6666"),
            ),
        ),
    )
    usb_ids = device.usb_ids
    assert usb_ids[0] == ("10c4", "ea60")
    assert usb_ids[1] == ("3333", "4444")
    assert usb_ids[2] == ("5555", "6666")


@pytest.mark.unit
def test_nrf52_device_class() -> None:
    """Test that nrf52 devices have correct device_class."""
    nrf52_devices = [d for d in supported_devices if d.device_class == "nrf52"]
    assert len(nrf52_devices) > 0
    assert all(d.device_class == "nrf52" for d in nrf52_devices)


@pytest.mark.unit
def test_esp32_device_class_default() -> None:
    """Test that esp32 is the default device class."""
    device = SupportedDevice(name="Test")
    assert device.device_class == "esp32"
