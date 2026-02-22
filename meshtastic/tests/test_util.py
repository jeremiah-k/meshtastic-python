"""Meshtastic unit tests for util.py."""

import base64
import binascii
import json
import logging
import re
from unittest.mock import patch

import pytest
from hypothesis import given
from hypothesis import strategies as st

from meshtastic.protobuf import mesh_pb2
from meshtastic.supported_device import SupportedDevice
from meshtastic.util import (
    DEFAULT_KEY,
    Acknowledgment,
    FixmeError,
    Timeout,
    active_ports_on_supported_devices,
    camel_to_snake,
    catchAndIgnore,
    channel_hash,
    convert_mac_addr,
    eliminate_duplicate_port,
    findPorts,
    fixme,
    fromPSK,
    fromStr,
    generate_channel_hash,
    genPSK256,
    hexstr,
    ipstr,
    is_windows11,
    message_to_json,
    pskToString,
    quoteBooleans,
    readnet_u16,
    remove_keys_from_dict,
    snake_to_camel,
    stripnl,
)

_BASE64_ALLOWED_CHARS = (
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/"
)
_BASE64_INVALID_CHARS = "!\"#$%&'()*,-.:;<>?@[\\]^_`{|}~"


class _TempPort:
    """Stub port object for serial-port discovery tests."""

    def __init__(self, device: str | None = None, vid: int | None = None) -> None:
        """Create a temporary port stub with an optional device path and USB vendor ID.

        Parameters
        ----------
        device : str | None
            Port device path (e.g., '/dev/ttyUSB0') or None to leave unset. (Default value = None)
        vid : int | None
            USB vendor ID as an integer, or None to leave unset. (Default value = None)
        """
        self.device = device
        self.vid = vid


@pytest.mark.unit
def test_genPSK256():
    """Test genPSK256."""
    assert genPSK256() != ""


@pytest.mark.unit
def test_fromStr():
    """Test fromStr."""
    assert fromStr("") == b""
    assert fromStr("0x12") == b"\x12"
    assert fromStr("t")
    assert fromStr("T")
    assert fromStr("true")
    assert fromStr("True")
    assert fromStr("yes")
    assert fromStr("Yes")
    assert fromStr("f") is False
    assert fromStr("F") is False
    assert fromStr("false") is False
    assert fromStr("False") is False
    assert fromStr("no") is False
    assert fromStr("No") is False
    assert fromStr("100.01") == 100.01
    assert fromStr("123") == 123
    assert fromStr("abc") == "abc"
    assert fromStr("123456789") == 123456789
    assert fromStr("base64:Zm9vIGJhciBiYXo=") == b"foo bar baz"


@pytest.mark.unitslow
def test_quoteBooleans():
    """Test quoteBooleans."""
    assert quoteBooleans("") == ""
    assert quoteBooleans("foo") == "foo"
    assert quoteBooleans("true") == "true"
    assert quoteBooleans("false") == "false"
    assert quoteBooleans(": true") == ": 'true'"
    assert quoteBooleans(": false") == ": 'false'"


@pytest.mark.unit
def test_fromPSK():
    """Test fromPSK."""
    assert fromPSK("random") != ""
    assert fromPSK("none") == b"\x00"
    assert fromPSK("default") == b"\x01"
    assert fromPSK("simple22") == b"\x17"
    assert fromPSK("trash") == "trash"


@pytest.mark.unit
def test_stripnl():
    """Test stripnl."""
    assert stripnl("") == ""
    assert stripnl("a\n") == "a"
    assert stripnl(" a \n ") == "a"
    assert stripnl("a\nb") == "a b"


@pytest.mark.unit
def test_pskToString_empty_string():
    """Test pskToString empty string."""
    assert pskToString(b"") == "unencrypted"


@pytest.mark.unit
def test_pskToString_string():
    """Test pskToString string."""
    assert pskToString(b"hunter123") == "secret"


@pytest.mark.unit
def test_pskToString_one_byte_zero_value():
    """Test pskToString one byte that is value of 0."""
    assert pskToString(bytes([0x00])) == "unencrypted"


@pytest.mark.unitslow
def test_pskToString_one_byte_non_zero_value():
    """Test pskToString one byte that is non-zero."""
    assert pskToString(bytes([0x01])) == "default"


@pytest.mark.unitslow
def test_pskToString_many_bytes():
    """Test pskToString many bytes."""
    assert pskToString(bytes([0x02, 0x01])) == "secret"


@pytest.mark.unit
def test_pskToString_simple():
    """Test pskToString simple."""
    assert pskToString(bytes([0x03])) == "simple2"


@pytest.mark.unitslow
def test_fixme():
    """Test fixme()."""
    with pytest.raises(FixmeError) as pytest_wrapped_e:
        fixme("some exception")
    assert pytest_wrapped_e.type is FixmeError


@pytest.mark.unit
def test_catchAndIgnore(caplog):
    """Test catchAndIgnore() does not actually throw an exception, but just logs.

    Raises
    ------
    Exception
        Raised inside the closure to exercise the retry handler.
    """

    def some_closure():
        """Raise an Exception with the message "foo".

        Raises
        ------
        Exception
            Always raised with message "foo".
        """
        raise Exception("foo")  # pylint: disable=W0719  # noqa: TRY002

    with caplog.at_level(logging.DEBUG):
        catchAndIgnore("something", some_closure)
    assert re.search(r"Exception thrown in something", caplog.text, re.MULTILINE)


@pytest.mark.unitslow
def test_remove_keys_from_dict_empty_keys_empty_dict():
    """Test when keys and dict both are empty."""
    assert not remove_keys_from_dict((), {})


@pytest.mark.unitslow
def test_remove_keys_from_dict_empty_dict():
    """Test when dict is empty."""
    assert not remove_keys_from_dict(("a",), {})


@pytest.mark.unit
def test_remove_keys_from_dict_empty_keys():
    """Test when keys is empty."""
    assert remove_keys_from_dict((), {"a": 1}) == {"a": 1}


@pytest.mark.unitslow
def test_remove_keys_from_dict():
    """Test remove_keys_from_dict()."""
    assert remove_keys_from_dict(("b",), {"a": 1, "b": 2}) == {"a": 1}


@pytest.mark.unitslow
def test_remove_keys_from_dict_multiple_keys():
    """Test remove_keys_from_dict()."""
    keys = ("a", "b")
    adict = {"a": 1, "b": 2, "c": 3}
    assert remove_keys_from_dict(keys, adict) == {"c": 3}


@pytest.mark.unit
def test_remove_keys_from_dict_nested():
    """Test remove_keys_from_dict()."""
    keys = ("b",)
    adict = {"a": {"b": 1}, "b": 2, "c": 3}
    exp = {"a": {}, "c": 3}
    assert remove_keys_from_dict(keys, adict) == exp


@pytest.mark.unitslow
def test_Timeout_not_found():
    """Test Timeout()."""
    to = Timeout(1)
    attrs = "foo"
    to.waitForSet("bar", attrs)


@pytest.mark.unitslow
def test_Timeout_found():
    """Test Timeout()."""
    to = Timeout(1)
    attrs = ()
    to.waitForSet("bar", attrs)


@pytest.mark.unitslow
def test_hexstr():
    """Test hexstr()."""
    assert hexstr(b"123") == "31:32:33"
    assert hexstr(b"") == ""


@pytest.mark.unitslow
def test_ipstr():
    """Test ipstr()."""
    assert ipstr(b"1234") == "49.50.51.52"
    assert ipstr(b"") == ""


@pytest.mark.unitslow
def test_readnet_u16():
    """Test readnet_u16()."""
    assert readnet_u16(b"123456", 2) == 13108


@pytest.mark.unitslow
@patch("serial.tools.list_ports.comports", return_value=[])
def test_findPorts_when_none_found(patch_comports):
    """Test findPorts()."""
    assert not findPorts()
    patch_comports.assert_called()


@pytest.mark.unitslow
@patch("serial.tools.list_ports.comports")
def test_findPorts_when_duplicate_found_and_duplicate_option_used(patch_comports):
    """Verify that findPorts() removes duplicate serial devices when.

    eliminate_duplicates is True.

    Sets the patched comports() to return two port-like objects representing
    the same physical device and asserts findPorts(eliminate_duplicates=True)
    returns only the deduplicated device path.

    Parameters
    ----------
    patch_comports : MagicMock
        pytest fixture that patches and returns the
        serial.tools.list_ports.comports function.
    """
    fake1 = _TempPort("/dev/cu.usbserial-1430", vid=0xFFFF)
    fake2 = _TempPort("/dev/cu.wchusbserial1430", vid=0xFFFE)
    patch_comports.return_value = [fake1, fake2]
    assert findPorts(eliminate_duplicates=True) == ["/dev/cu.wchusbserial1430"]
    patch_comports.assert_called()


@pytest.mark.unitslow
@patch("serial.tools.list_ports.comports")
def test_findPorts_when_duplicate_found_and_duplicate_option_used_ports_reversed(
    patch_comports,
):
    """Verifies that findPorts(eliminate_duplicates=True) returns the expected.

    single port when duplicate devices are reported in reversed order.

    Patches the comports listing to simulate two ports that should be
    considered duplicates and asserts the duplicate-elimination logic
    selects the correct remaining device.

    """
    fake1 = _TempPort("/dev/cu.usbserial-1430", vid=0xFFFF)
    fake2 = _TempPort("/dev/cu.wchusbserial1430", vid=0xFFFE)
    patch_comports.return_value = [fake2, fake1]
    assert findPorts(eliminate_duplicates=True) == ["/dev/cu.wchusbserial1430"]
    patch_comports.assert_called()


@pytest.mark.unitslow
@patch("serial.tools.list_ports.comports")
def test_findPorts_when_duplicate_found_and_duplicate_option_not_used(patch_comports):
    """Test findPorts()."""
    fake1 = _TempPort("/dev/cu.usbserial-1430", vid=0xFFFF)
    fake2 = _TempPort("/dev/cu.wchusbserial1430", vid=0xFFFE)
    patch_comports.return_value = [fake1, fake2]
    assert findPorts() == ["/dev/cu.usbserial-1430", "/dev/cu.wchusbserial1430"]
    patch_comports.assert_called()


@pytest.mark.unitslow
def test_convert_mac_addr():
    """Test convert_mac_addr()."""
    assert convert_mac_addr("/c0gFyhb") == "fd:cd:20:17:28:5b"
    assert convert_mac_addr("fd:cd:20:17:28:5b") == "fd:cd:20:17:28:5b"
    assert convert_mac_addr("") == ""


@pytest.mark.unit
def test_snake_to_camel():
    """Test snake_to_camel."""
    assert snake_to_camel("") == ""
    assert snake_to_camel("foo") == "foo"
    assert snake_to_camel("foo_bar") == "fooBar"
    assert snake_to_camel("fooBar") == "fooBar"


@pytest.mark.unit
def test_camel_to_snake():
    """Test camel_to_snake."""
    assert camel_to_snake("") == ""
    assert camel_to_snake("foo") == "foo"
    assert camel_to_snake("Foo") == "foo"
    assert camel_to_snake("fooBar") == "foo_bar"
    assert camel_to_snake("fooBarBaz") == "foo_bar_baz"


@pytest.mark.unit
def test_eliminate_duplicate_port():
    """Test eliminate_duplicate_port()."""
    assert not eliminate_duplicate_port([])
    assert eliminate_duplicate_port(["/dev/fake"]) == ["/dev/fake"]
    assert eliminate_duplicate_port(["/dev/fake", "/dev/fake1"]) == [
        "/dev/fake",
        "/dev/fake1",
    ]
    assert eliminate_duplicate_port(["/dev/fake", "/dev/fake1", "/dev/fake2"]) == [
        "/dev/fake",
        "/dev/fake1",
        "/dev/fake2",
    ]
    assert eliminate_duplicate_port(
        ["/dev/cu.usbserial-1430", "/dev/cu.wchusbserial1430"]
    ) == ["/dev/cu.wchusbserial1430"]
    assert eliminate_duplicate_port(
        ["/dev/cu.wchusbserial1430", "/dev/cu.usbserial-1430"]
    ) == ["/dev/cu.wchusbserial1430"]
    assert eliminate_duplicate_port(
        ["/dev/cu.usbserial-1234", "/dev/cu.wchusbserial5678"]
    ) == ["/dev/cu.usbserial-1234", "/dev/cu.wchusbserial5678"]
    assert eliminate_duplicate_port(
        ["/dev/cu.SLAB_USBtoUART", "/dev/cu.usbserial-0001"]
    ) == ["/dev/cu.usbserial-0001"]
    assert eliminate_duplicate_port(
        ["/dev/cu.usbserial-0001", "/dev/cu.SLAB_USBtoUART"]
    ) == ["/dev/cu.usbserial-0001"]
    assert eliminate_duplicate_port(
        ["/dev/cu.usbmodem11301", "/dev/cu.wchusbserial11301"]
    ) == ["/dev/cu.wchusbserial11301"]
    assert eliminate_duplicate_port(
        ["/dev/cu.wchusbserial11301", "/dev/cu.usbmodem11301"]
    ) == ["/dev/cu.wchusbserial11301"]
    assert eliminate_duplicate_port(
        ["/dev/cu.usbmodem53230051441", "/dev/cu.wchusbserial53230051441"]
    ) == ["/dev/cu.wchusbserial53230051441"]
    assert eliminate_duplicate_port(
        ["/dev/cu.wchusbserial53230051441", "/dev/cu.usbmodem53230051441"]
    ) == ["/dev/cu.wchusbserial53230051441"]


@patch("platform.version", return_value="10.0.22000.194")
@patch("platform.release", return_value="10")
@patch("platform.system", return_value="Windows")
def test_is_windows11_true(patched_platform, patched_release, patched_version):
    """Test is_windows11()."""
    assert is_windows11() is True
    patched_platform.assert_called()
    patched_release.assert_called()
    patched_version.assert_called()


@patch("platform.version", return_value="10.0.a2200.foo")  # made up
@patch("platform.release", return_value="10")
@patch("platform.system", return_value="Windows")
def test_is_windows11_true2(patched_platform, patched_release, patched_version):
    """Test is_windows11()."""
    assert is_windows11() is False
    patched_platform.assert_called()
    patched_release.assert_called()
    patched_version.assert_called()


@patch("platform.version", return_value="10.0.17763")  # windows 10 home
@patch("platform.release", return_value="10")
@patch("platform.system", return_value="Windows")
def test_is_windows11_false(patched_platform, patched_release, patched_version):
    """Test is_windows11()."""
    assert is_windows11() is False
    patched_platform.assert_called()
    patched_release.assert_called()
    patched_version.assert_called()


@patch("platform.release", return_value="8.1")
@patch("platform.system", return_value="Windows")
def test_is_windows11_false_win8_1(patched_platform, patched_release):
    """Test is_windows11()."""
    assert is_windows11() is False
    patched_platform.assert_called()
    patched_release.assert_called()


@patch("platform.release", return_value="2022Server")
@patch("platform.system", return_value="Windows")
def test_is_windows11_false_winserver(patched_platform, patched_release):
    """Test is_windows11()."""
    assert is_windows11() is False
    patched_platform.assert_called()
    patched_release.assert_called()


@pytest.mark.unit
@patch("platform.system", return_value="Linux")
def test_active_ports_on_supported_devices_empty(mock_platform):
    """Test active_ports_on_supported_devices()."""
    sds = set()
    assert active_ports_on_supported_devices(sds) == set()
    mock_platform.assert_called()


@pytest.mark.unit
@patch("subprocess.getstatusoutput")
@patch("platform.system", return_value="Linux")
def test_active_ports_on_supported_devices_linux(mock_platform, mock_sp):
    """Test active_ports_on_supported_devices()."""
    mock_sp.return_value = (
        None,
        "crw-rw-rw-  1 root        wheel   0x9000000 Feb  8 22:22 /dev/ttyUSBfake",
    )
    fake_device = SupportedDevice(
        name="a", for_firmware="heltec-v2.1", baseport_on_linux="ttyUSB"
    )
    fake_supported_devices = [fake_device]
    assert active_ports_on_supported_devices(fake_supported_devices) == {
        "/dev/ttyUSBfake"
    }
    mock_platform.assert_called()
    mock_sp.assert_called()


@pytest.mark.unit
@patch("subprocess.getstatusoutput")
@patch("platform.system", return_value="Darwin")
def test_active_ports_on_supported_devices_mac(mock_platform, mock_sp):
    """Test active_ports_on_supported_devices()."""
    mock_sp.return_value = (
        None,
        "crw-rw-rw-  1 root        wheel   0x9000000 Feb  8 22:22 /dev/cu.usbserial-foo",
    )
    fake_device = SupportedDevice(
        name="a", for_firmware="heltec-v2.1", baseport_on_linux="cu.usbserial-"
    )
    fake_supported_devices = [fake_device]
    assert active_ports_on_supported_devices(fake_supported_devices) == {
        "/dev/cu.usbserial-foo"
    }
    mock_platform.assert_called()
    mock_sp.assert_called()


@pytest.mark.unit
@patch("meshtastic.util.detect_windows_port", return_value={"COM2"})
@patch("platform.system", return_value="Windows")
def test_active_ports_on_supported_devices_win(mock_platform, mock_dwp):
    """Test active_ports_on_supported_devices()."""
    fake_device = SupportedDevice(name="a", for_firmware="heltec-v2.1")
    fake_supported_devices = [fake_device]
    assert active_ports_on_supported_devices(fake_supported_devices) == {"COM2"}
    mock_platform.assert_called()
    mock_dwp.assert_called()


@pytest.mark.unit
@patch("subprocess.getstatusoutput")
@patch("platform.system", return_value="Darwin")
def test_active_ports_on_supported_devices_mac_no_duplicates_check(
    mock_platform, mock_sp
):
    """Test active_ports_on_supported_devices()."""
    mock_sp.return_value = (
        None,
        (
            "crw-rw-rw-  1 root  wheel  0x9000005 Mar  8 10:05 /dev/cu.usbmodem53230051441\n"
            "crw-rw-rw-  1 root  wheel  0x9000003 Mar  8 10:06 /dev/cu.wchusbserial53230051441"
        ),
    )
    fake_device = SupportedDevice(
        name="a", for_firmware="tbeam", baseport_on_mac="cu.usbmodem"
    )
    fake_supported_devices = [fake_device]
    assert active_ports_on_supported_devices(fake_supported_devices, False) == {
        "/dev/cu.usbmodem53230051441",
        "/dev/cu.wchusbserial53230051441",
    }
    mock_platform.assert_called()
    mock_sp.assert_called()


@pytest.mark.unit
@patch("subprocess.getstatusoutput")
@patch("platform.system", return_value="Darwin")
def test_active_ports_on_supported_devices_mac_duplicates_check(mock_platform, mock_sp):
    """Ensure duplicate mac device entries are deduplicated when duplicate checking is enabled.

    Verifies that given a mac-style device listing containing two related device paths, active_ports_on_supported_devices(...)
    returns only the non-duplicate host port when the duplicates check is enabled.

    """
    mock_sp.return_value = (
        None,
        (
            "crw-rw-rw-  1 root  wheel  0x9000005 Mar  8 10:05 /dev/cu.usbmodem53230051441\n"
            "crw-rw-rw-  1 root  wheel  0x9000003 Mar  8 10:06 /dev/cu.wchusbserial53230051441"
        ),
    )
    fake_device = SupportedDevice(
        name="a", for_firmware="tbeam", baseport_on_mac="cu.usbmodem"
    )
    fake_supported_devices = [fake_device]
    assert active_ports_on_supported_devices(fake_supported_devices, True) == {
        "/dev/cu.wchusbserial53230051441"
    }
    mock_platform.assert_called()
    mock_sp.assert_called()


@pytest.mark.unit
def test_message_to_json_shows_all():
    """Test that message_to_json prints fields that aren't included in data passed in."""
    actual = json.loads(message_to_json(mesh_pb2.MyNodeInfo()))
    # Check that expected keys are present with expected values, rather than
    # asserting exact equality, to avoid fragility when protobuf schema adds fields.
    expected = {
        "myNodeNum": 0,
        "rebootCount": 0,
        "minAppVersion": 0,
        "deviceId": "",
        "pioEnv": "",
        "firmwareEdition": "VANILLA",
        "nodedbCount": 0,
    }
    for key, value in expected.items():
        assert (
            actual.get(key) == value
        ), f"Key {key}: expected {value}, got {actual.get(key)}"


@pytest.mark.unit
def test_acknowledgement_reset():
    """Test that the reset method can set all fields back to False."""
    test_ack_obj = Acknowledgment()
    # everything's set to False; let's set it to True to get a good test
    test_ack_obj.receivedAck = True
    test_ack_obj.receivedNak = True
    test_ack_obj.receivedImplAck = True
    test_ack_obj.receivedTraceRoute = True
    test_ack_obj.receivedTelemetry = True
    test_ack_obj.receivedPosition = True
    test_ack_obj.receivedWaypoint = True
    test_ack_obj.reset()
    assert test_ack_obj.receivedAck is False
    assert test_ack_obj.receivedNak is False
    assert test_ack_obj.receivedImplAck is False
    assert test_ack_obj.receivedTraceRoute is False
    assert test_ack_obj.receivedTelemetry is False
    assert test_ack_obj.receivedPosition is False
    assert test_ack_obj.receivedWaypoint is False


@pytest.mark.unitslow
@given(
    a_string=st.text(
        alphabet=st.characters(
            codec="ascii",
            min_codepoint=0x5F,
            max_codepoint=0x7A,
            exclude_characters=r"`",
        )
    ).filter(
        lambda x: x != "" and x[0] != "_" and x[-1] != "_" and not re.search(r"__", x)
    )
)
def test_roundtrip_snake_to_camel_camel_to_snake(a_string):
    """Test that snake_to_camel and camel_to_snake roundtrip each other."""
    value0 = snake_to_camel(a_string=a_string)
    value1 = camel_to_snake(a_string=value0)
    assert a_string == value1, (a_string, value1)


@pytest.mark.unitslow
@given(st.text())
def test_fuzz_camel_to_snake(a_string):
    """Test that camel_to_snake produces outputs with underscores for multi-word camelcase."""
    result = camel_to_snake(a_string)
    assert "_" in result or result == a_string.lower().replace("_", "")


@pytest.mark.unitslow
@given(st.text())
def test_fuzz_snake_to_camel(a_string):
    """Test that snake_to_camel satisfies core invariants."""
    result = snake_to_camel(a_string)
    assert "_" not in result
    if "_" not in a_string:
        assert result == a_string


def test_snake_to_camel_examples() -> None:
    """Test fixed snake_to_camel examples."""
    assert snake_to_camel("foo_bar") == "fooBar"
    assert snake_to_camel("alreadyCamel") == "alreadyCamel"


@pytest.mark.unitslow
@given(st.text())
def test_fuzz_stripnl(s):
    """Test that stripnl always takes away newlines."""
    result = stripnl(s)
    assert "\n" not in result


@pytest.mark.unitslow
@given(st.binary())
def test_fuzz_pskToString(psk):
    """Test that pskToString produces sane output for any bytes."""
    result = pskToString(psk)
    if len(psk) == 0:
        assert result == "unencrypted"
    elif len(psk) == 1:
        b = psk[0]
        if b == 0:
            assert result == "unencrypted"
        elif b == 1:
            assert result == "default"
        else:
            assert result == f"simple{b - 1}"
    else:
        assert result == "secret"


@pytest.mark.unitslow
@given(
    st.text(
        alphabet=st.characters(min_codepoint=0x20, max_codepoint=0x7E),
        max_size=256,
    ).filter(lambda s: not s.startswith("0x") and not s.startswith("base64:"))
)
def test_fuzz_fromStr_non_prefixed(valstr):
    """Test fromStr behavior for non-prefixed string inputs."""
    result = fromStr(valstr)
    if len(valstr) == 0:
        assert result == b""
    elif valstr.lower() in {"t", "true", "yes"}:
        assert result is True
    elif valstr.lower() in {"f", "false", "no"}:
        assert result is False
    else:
        try:
            int(valstr)
            assert isinstance(result, int)
        except ValueError:
            try:
                float(valstr)
                assert isinstance(result, float)
            except ValueError:
                assert isinstance(result, str)


@pytest.mark.unitslow
@given(
    st.text(
        alphabet=st.sampled_from(list("0123456789abcdefABCDEF")),
        min_size=0,
        max_size=64,
    )
)
def test_fuzz_fromStr_hex_prefixed(hex_digits):
    """Test that fromStr decodes 0x-prefixed hex strings, including odd lengths."""
    expected_hex = hex_digits
    if len(expected_hex) == 0:
        expected_hex = "00"
    elif len(expected_hex) % 2 == 1:
        expected_hex = "0" + expected_hex
    assert fromStr(f"0x{hex_digits}") == bytes.fromhex(expected_hex)


@pytest.mark.unitslow
@given(
    st.text(
        alphabet=st.characters(min_codepoint=0x20, max_codepoint=0x7E),
        min_size=1,
        max_size=64,
    ).filter(
        lambda s: re.fullmatch(r"[0-9a-fA-F]+", s) is None
        and not any(ch.isspace() for ch in s)
    )
)
def test_fuzz_fromStr_hex_invalid_raises(hex_digits):
    """Test that fromStr raises for invalid 0x-prefixed hex strings."""
    with pytest.raises(ValueError):
        fromStr(f"0x{hex_digits}")


@pytest.mark.unitslow
@given(st.binary(max_size=256))
def test_fuzz_fromStr_base64_roundtrip(raw_value):
    """Test that fromStr round-trips valid base64-prefixed payloads."""
    encoded = base64.b64encode(raw_value).decode("ascii")
    assert fromStr(f"base64:{encoded}") == raw_value


@pytest.mark.unitslow
@given(
    st.text(
        alphabet=st.sampled_from(
            list("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/")
        ),
        min_size=1,
        max_size=65,
    ).filter(lambda s: len(s) % 4 == 1)
)
def test_fuzz_fromStr_base64_malformed_raises(base64_payload):
    """Test that fromStr raises for malformed base64 payload lengths."""
    with pytest.raises(binascii.Error):
        fromStr(f"base64:{base64_payload}")


@st.composite
def _base64_payload_with_single_invalid_char(draw):
    """Generate base64-like payloads with valid length and exactly one invalid character."""
    quad_count = draw(st.integers(min_value=1, max_value=32))
    payload_len = quad_count * 4
    chars = draw(
        st.lists(
            st.sampled_from(list(_BASE64_ALLOWED_CHARS)),
            min_size=payload_len,
            max_size=payload_len,
        )
    )
    invalid_idx = draw(st.integers(min_value=0, max_value=payload_len - 1))
    chars[invalid_idx] = draw(st.sampled_from(list(_BASE64_INVALID_CHARS)))
    return "".join(chars)


@pytest.mark.unitslow
@given(_base64_payload_with_single_invalid_char())
def test_fuzz_fromStr_base64_invalid_chars_raises(base64_payload):
    """Test that fromStr raises for base64 payloads containing invalid characters."""
    with pytest.raises(binascii.Error):
        fromStr(f"base64:{base64_payload}")


def test_shorthex():
    """Test the shortest hex string representations."""
    result = fromStr("0x0")
    assert result == b"\x00"
    result = fromStr("0x5")
    assert result == b"\x05"
    result = fromStr("0x123")
    assert result == b"\x01#"
    result = fromStr("0xffff")
    assert result == b"\xff\xff"


def test_channel_hash_basics():
    """Test the default key and LongFast with channel_hash."""
    assert channel_hash(DEFAULT_KEY) == 2
    assert channel_hash("LongFast".encode("utf-8")) == 10


@pytest.mark.unitslow
@given(st.text(min_size=1, max_size=12))
def test_channel_hash_fuzz(channel_name):
    """Test channel_hash with fuzzed channel names, ensuring it produces single-byte values."""
    hashed = channel_hash(channel_name.encode("utf-8"))
    assert 0 <= hashed <= 0xFF


def test_generate_channel_hash_basics():
    """Test the default key and LongFast/MediumFast with generate_channel_hash."""
    assert generate_channel_hash("LongFast", "AQ==") == 8
    assert generate_channel_hash("LongFast", bytes([1])) == 8
    assert generate_channel_hash("LongFast", DEFAULT_KEY) == 8
    assert generate_channel_hash("MediumFast", DEFAULT_KEY) == 31


@given(st.text(min_size=1, max_size=12))
def test_generate_channel_hash_fuzz_default_key(channel_name):
    """Test generate_channel_hash with fuzzed channel names and the default key, ensuring it produces single-byte values."""
    hashed = generate_channel_hash(channel_name, DEFAULT_KEY)
    assert 0 <= hashed <= 0xFF


@given(st.text(min_size=1, max_size=12), st.binary(min_size=1, max_size=1))
def test_generate_channel_hash_fuzz_simple(channel_name, key_bytes):
    """Test generate_channel_hash with fuzzed channel names and one-byte keys, ensuring it produces single-byte values."""
    hashed = generate_channel_hash(channel_name, key_bytes)
    assert 0 <= hashed <= 0xFF


@given(st.text(min_size=1, max_size=12), st.binary(min_size=16, max_size=16))
def test_generate_channel_hash_fuzz_aes128(channel_name, key_bytes):
    """Test generate_channel_hash with fuzzed channel names and 128-bit keys, ensuring it produces single-byte values."""
    hashed = generate_channel_hash(channel_name, key_bytes)
    assert 0 <= hashed <= 0xFF


@given(st.text(min_size=1, max_size=12), st.binary(min_size=32, max_size=32))
def test_generate_channel_hash_fuzz_aes256(channel_name, key_bytes):
    """Test generate_channel_hash with fuzzed channel names and 256-bit keys, ensuring it produces single-byte values."""
    hashed = generate_channel_hash(channel_name, key_bytes)
    assert 0 <= hashed <= 0xFF
