"""Meshtastic unit tests for __main__.py."""

# pylint: disable=C0302,W0613,R0917

import base64
import importlib.util
import logging
import os
import platform
import re
import sys
from pathlib import Path
from typing import Any, Callable, cast
from unittest.mock import MagicMock, mock_open, patch

import pytest
import yaml

from meshtastic import mt_config
from meshtastic.__main__ import (
    _normalize_pref_name,
    _parse_host_port,
    _prefix_base64_key,
    _set_missing_flags_false,
    export_config,
    initParser,
    main,
    onConnection,
    onNode,
    onReceive,
    traverseConfig,
    tunnelMain,
)

# from ..ble_interface import BLEInterface
from ..node import Node
from ..protobuf import config_pb2, localonly_pb2
from ..protobuf.channel_pb2 import Channel  # pylint: disable=E0611

# from ..radioconfig_pb2 import UserPreferences
# import meshtastic.config_pb2
from ..serial_interface import SerialInterface
from ..tcp_interface import TCPInterface

# from ..remote_hardware import onGPIOreceive
# from ..config_pb2 import Config


def _mock_sendText_helper(
    text: str,
    dest: Any,
    wantAck: bool = False,
    wantResponse: bool = False,
    onResponse: Callable[..., Any] | None = None,
    channelIndex: int = 0,
    portNum: int = 0,
) -> None:
    """Shared helper for mocking sendText; prints parameters to stdout for test assertions.

    Parameters
    ----------
    text : str
        The text message content to send.
    dest : Any
        Destination node ID or address.
    wantAck : bool
        Whether to request acknowledgement. (Default value = False)
    wantResponse : bool
        Whether to request a response. (Default value = False)
    onResponse : Callable[..., Any] | None
        Optional response callback. (Default value = None)
    channelIndex : int
        Channel index to send on. (Default value = 0)
    portNum : int
        Port number for the message. (Default value = 0)
    """
    _ = onResponse  # Mark as intentionally unused
    print("inside mocked sendText")
    print(f"{text} {dest} {wantAck} {wantResponse} {channelIndex} {portNum}")


@pytest.fixture(autouse=True)
def _mock_newer_version_check(monkeypatch: pytest.MonkeyPatch) -> None:
    """Prevent external network calls during unit tests in this module.

    Parameters
    ----------
    monkeypatch : pytest.MonkeyPatch
        Pytest monkeypatching fixture.
    """
    monkeypatch.setattr("meshtastic.util.check_if_newer_version", lambda: None)


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_main_init_parser_no_args(capsys: pytest.CaptureFixture[str]) -> None:
    """Test no arguments."""
    sys.argv = [""]
    mt_config.args = sys.argv  # type: ignore[assignment]
    initParser()
    out, err = capsys.readouterr()
    assert out == ""
    assert err == ""


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_main_init_parser_version(capsys: pytest.CaptureFixture[str]) -> None:
    """Test --version."""
    sys.argv = ["", "--version"]
    mt_config.args = sys.argv  # type: ignore[assignment]

    with pytest.raises(SystemExit) as pytest_wrapped_e:
        initParser()
    assert pytest_wrapped_e.type is SystemExit
    assert pytest_wrapped_e.value.code == 0
    out, err = capsys.readouterr()
    assert re.match(r"[0-9]+\.[0-9]+[\.a][0-9]", out)
    assert err == ""


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_main_init_parser_help_mentions_list_fields(capsys: pytest.CaptureFixture[str]) -> None:
    """Test --help mentions dynamic config field discovery."""
    sys.argv = ["", "--help"]
    with pytest.raises(SystemExit) as pytest_wrapped_e:
        initParser()
    assert pytest_wrapped_e.type is SystemExit
    assert pytest_wrapped_e.value.code == 0
    out, err = capsys.readouterr()
    assert "--list-fields" in out
    assert "protobuf schemas" in out
    assert err == ""


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_main_main_version(capsys: pytest.CaptureFixture[str]) -> None:
    """Test --version."""
    sys.argv = ["", "--version"]
    mt_config.args = sys.argv  # type: ignore[assignment]

    with pytest.raises(SystemExit) as pytest_wrapped_e:
        main()
    assert pytest_wrapped_e.type is SystemExit
    assert pytest_wrapped_e.value.code == 0
    out, err = capsys.readouterr()
    assert re.match(r"[0-9]+\.[0-9]+[\.a][0-9]", out)
    assert err == ""


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_main_list_fields_prints_known_fields_and_alias(capsys: pytest.CaptureFixture[str]) -> None:
    """Test --list-fields prints dynamic protobuf fields and compatibility aliases."""
    sys.argv = ["", "--list-fields"]
    main()
    out, err = capsys.readouterr()
    assert "Local config fields:" in out
    assert "bluetooth.enabled" in out
    assert "bluetooth.mode" in out
    assert "bluetooth.fixed_pin" in out
    assert "display.units" in out
    assert "display.use_12h_clock" in out
    assert "display.use_12_hour -> display.use_12h_clock" in out
    assert err == ""


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_main_list_fields_includes_all_descriptor_fields(capsys: pytest.CaptureFixture[str]) -> None:
    """Test --list-fields includes every top-level protobuf config field."""
    sys.argv = ["", "--list-fields"]
    main()
    out, err = capsys.readouterr()

    expected: list[str] = []
    for message in (localonly_pb2.LocalConfig(), localonly_pb2.LocalModuleConfig()):
        for section in message.DESCRIPTOR.fields:
            if section.name == "version":
                continue
            if section.message_type is None:
                continue
            for field in section.message_type.fields:
                expected.append(f"{section.name}.{field.name}")

    missing = [field for field in expected if field not in out]
    assert missing == []
    assert err == ""


@pytest.mark.unit
def test_normalize_pref_name_display_alias() -> None:
    """Test legacy display field aliases normalize to canonical names."""
    assert _normalize_pref_name("display.use_12_hour") == "display.use_12h_clock"
    assert _normalize_pref_name("display.use12Hour") == "display.use_12h_clock"
    assert _normalize_pref_name("display.use12hClock") == "display.use_12h_clock"
    assert _normalize_pref_name("display.use12HClock") == "display.use_12h_clock"


@pytest.mark.unit
def test_parse_host_port_with_explicit_port() -> None:
    """Test _parse_host_port parses host:port values."""
    hostname, port = _parse_host_port("hostname.example:4403", default_port=4403)
    assert hostname == "hostname.example"
    assert port == 4403


@pytest.mark.unit
def test_parse_host_port_with_bracketed_ipv6_port() -> None:
    """Test _parse_host_port parses bracketed IPv6 addresses with port."""
    hostname, port = _parse_host_port("[2001:db8::1]:4403", default_port=4403)
    assert hostname == "2001:db8::1"
    assert port == 4403


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_main_host_argument_passes_parsed_port_to_tcp_interface() -> None:
    """Test --host host:port passes parsed host and port to TCPInterface."""
    sys.argv = ["", "--host", "hostname.example:4403", "--set-time", "1"]
    mt_config.args = cast(Any, sys.argv)
    mocked_node = MagicMock()
    iface = MagicMock(autospec=TCPInterface)
    iface.__enter__ = MagicMock(return_value=iface)
    iface.__exit__ = MagicMock(return_value=None)
    iface.getNode.return_value = mocked_node

    with patch("meshtastic.tcp_interface.TCPInterface", return_value=iface) as ctor:
        main()

    mocked_node.setTime.assert_called_once_with(1)
    ctor.assert_called_once()
    args, kwargs = ctor.call_args
    assert args[0] == "hostname.example"
    assert kwargs["portNumber"] == 4403


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_main_main_no_args(capsys: pytest.CaptureFixture[str]) -> None:
    """Test with no args."""
    sys.argv = [""]
    mt_config.args = sys.argv  # type: ignore[assignment]

    with pytest.raises(SystemExit) as pytest_wrapped_e:
        main()
    assert pytest_wrapped_e.type is SystemExit
    assert pytest_wrapped_e.value.code == 1
    _, err = capsys.readouterr()
    assert re.search(r"usage:", err, re.MULTILINE)


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_main_support(capsys: pytest.CaptureFixture[str]) -> None:
    """Verify that the CLI --support option prints system information and exits with code 0.

    Asserts that stdout contains "System", "Platform", "Machine", and "Executable", and that no stderr was produced.

    """
    sys.argv = ["", "--support"]
    mt_config.args = sys.argv  # type: ignore[assignment]

    with pytest.raises(SystemExit) as pytest_wrapped_e:
        main()
    assert pytest_wrapped_e.type is SystemExit
    assert pytest_wrapped_e.value.code == 0
    out, err = capsys.readouterr()
    assert re.search(r"System", out, re.MULTILINE)
    assert re.search(r"Platform", out, re.MULTILINE)
    assert re.search(r"Machine", out, re.MULTILINE)
    assert re.search(r"Executable", out, re.MULTILINE)
    assert err == ""


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
@patch("meshtastic.tcp_interface.TCPInterface", side_effect=OSError("no tcp"))
@patch("meshtastic.util.findPorts", return_value=[])
def test_main_ch_index_no_devices(patched_find_ports: Any, _patched_tcp: Any, capsys: pytest.CaptureFixture[str]) -> None:
    """Verify CLI handles --ch-index 1 when no devices are available.

    Asserts that the global channel_index is set to 1, main() exits with SystemExit
    code 1, stderr contains "Error connecting to localhost", and the port discovery
    function was invoked.

    """
    sys.argv = ["", "--ch-index", "1"]
    mt_config.args = sys.argv  # type: ignore[assignment]

    with pytest.raises(SystemExit) as pytest_wrapped_e:
        main()
    assert mt_config.channel_index == 1
    assert pytest_wrapped_e.type is SystemExit
    assert pytest_wrapped_e.value.code == 1
    out, err = capsys.readouterr()
    assert out == ""
    assert re.search(r"Error connecting to localhost", err, re.MULTILINE)
    patched_find_ports.assert_called()


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
@patch("meshtastic.util.findPorts", return_value=[])
def test_main_test_no_ports(patched_find_ports: Any, capsys: pytest.CaptureFixture[str]) -> None:
    """Test --test with no hardware."""
    sys.argv = ["", "--test"]
    mt_config.args = sys.argv  # type: ignore[assignment]

    with pytest.raises(SystemExit) as pytest_wrapped_e:
        main()
    assert pytest_wrapped_e.type is SystemExit
    assert pytest_wrapped_e.value.code == 1
    patched_find_ports.assert_called()
    _, err = capsys.readouterr()
    # testAll() returns False when not enough ports, CLI reports test failure
    assert re.search(
        r"Test was not successful",
        err,
        re.MULTILINE,
    )


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
@patch("meshtastic.util.findPorts", return_value=["/dev/ttyFake1"])
def test_main_test_one_port(patched_find_ports: Any, capsys: pytest.CaptureFixture[str]) -> None:
    """Test --test with one fake port."""
    sys.argv = ["", "--test"]
    mt_config.args = sys.argv  # type: ignore[assignment]

    with pytest.raises(SystemExit) as pytest_wrapped_e:
        main()
    assert pytest_wrapped_e.type is SystemExit
    assert pytest_wrapped_e.value.code == 1
    patched_find_ports.assert_called()
    _, err = capsys.readouterr()
    # testAll() returns False when not enough ports, CLI reports test failure
    assert re.search(
        r"Test was not successful",
        err,
        re.MULTILINE,
    )


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
@patch("meshtastic.test.testAll", return_value=True)
def test_main_test_two_ports_success(patched_test_all: Any, capsys: pytest.CaptureFixture[str]) -> None:
    """Test --test two fake ports and testAll() is a simulated success."""
    sys.argv = ["", "--test"]
    mt_config.args = sys.argv  # type: ignore[assignment]

    with pytest.raises(SystemExit) as pytest_wrapped_e:
        main()
    assert pytest_wrapped_e.type is SystemExit
    assert pytest_wrapped_e.value.code == 0
    patched_test_all.assert_called()
    out, err = capsys.readouterr()
    assert re.search(r"Test was a success.", out, re.MULTILINE)
    assert err == ""


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
@patch("meshtastic.test.testAll", return_value=False)
def test_main_test_two_ports_fails(patched_test_all: Any, capsys: pytest.CaptureFixture[str]) -> None:
    """Test --test two fake ports and testAll() is a simulated failure."""
    sys.argv = ["", "--test"]
    mt_config.args = sys.argv  # type: ignore[assignment]

    with pytest.raises(SystemExit) as pytest_wrapped_e:
        main()
    assert pytest_wrapped_e.type is SystemExit
    assert pytest_wrapped_e.value.code == 1
    patched_test_all.assert_called()
    out, err = capsys.readouterr()
    # Error messages go to stderr
    assert re.search(r"Test was not successful.", err, re.MULTILINE)
    assert out == ""


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_main_info(capsys: pytest.CaptureFixture[str], caplog: pytest.LogCaptureFixture) -> None:
    """Tests that invoking the CLI with `--info` connects to a radio and calls SerialInterface.showInfo.

    Patches SerialInterface with a mock that prints a recognizable marker from showInfo, then
    asserts stdout contains "Connected to radio" and the marker, stderr is empty, and the
    SerialInterface constructor was invoked.

    """
    sys.argv = ["", "--info"]
    mt_config.args = sys.argv  # type: ignore[assignment]

    iface = MagicMock(autospec=SerialInterface)
    iface.__enter__ = MagicMock(return_value=iface)
    iface.__exit__ = MagicMock(return_value=None)

    def mock_showInfo() -> None:
        """Print a recognizable marker to stdout used by tests to simulate an interface's showInfo().

        This test helper prints the string "inside mocked showInfo" so tests can detect that the mocked showInfo was invoked.
        """
        print("inside mocked showInfo")

    iface.showInfo.side_effect = mock_showInfo
    with caplog.at_level(logging.DEBUG):
        with patch(
            "meshtastic.serial_interface.SerialInterface", return_value=iface
        ) as mo:
            main()
            out, err = capsys.readouterr()
            assert re.search(r"Connected to radio", out, re.MULTILINE)
            assert re.search(r"inside mocked showInfo", out, re.MULTILINE)
            assert err == ""
            mo.assert_called()


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
@patch("os.getlogin")
def test_main_info_with_permission_error(patched_getlogin: Any, capsys: pytest.CaptureFixture[str], caplog: pytest.LogCaptureFixture) -> None:
    """Verify that invoking the CLI with --info exits with code 1 and prints a permission-related.

    message when the serial interface cannot be opened due to a PermissionError.

    Asserts that a SystemExit with code 1 is raised, the current user lookup was attempted,
    stderr contains guidance matching "Need to add yourself", and stdout is empty.

    """
    sys.argv = ["", "--info"]
    mt_config.args = sys.argv  # type: ignore[assignment]

    patched_getlogin.return_value = "me"

    iface = MagicMock(autospec=SerialInterface)
    iface.__enter__ = MagicMock(return_value=iface)
    iface.__exit__ = MagicMock(return_value=None)
    with caplog.at_level(logging.DEBUG):
        with pytest.raises(SystemExit) as pytest_wrapped_e:
            with patch(
                "meshtastic.serial_interface.SerialInterface", return_value=iface
            ) as mo:
                mo.side_effect = PermissionError("bla bla")
                main()
        assert pytest_wrapped_e.type is SystemExit
        assert pytest_wrapped_e.value.code == 1
        out, err = capsys.readouterr()
        patched_getlogin.assert_called()
        # Error messages go to stderr
        assert re.search(r"Need to add yourself", err, re.MULTILINE)
        assert out == ""


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_main_info_with_tcp_interface(capsys: pytest.CaptureFixture[str]) -> None:
    """Test --info."""
    sys.argv = ["", "--info", "--host", "meshtastic.local"]
    mt_config.args = sys.argv  # type: ignore[assignment]

    iface = MagicMock(autospec=TCPInterface)
    iface.__enter__ = MagicMock(return_value=iface)
    iface.__exit__ = MagicMock(return_value=None)

    def mock_showInfo() -> None:
        """Print a recognizable marker to stdout used by tests to simulate an interface's showInfo().

        This test helper prints the string "inside mocked showInfo" so tests can detect that the mocked showInfo was invoked.
        """
        print("inside mocked showInfo")

    iface.showInfo.side_effect = mock_showInfo
    with patch("meshtastic.tcp_interface.TCPInterface", return_value=iface) as mo:
        main()
        out, err = capsys.readouterr()
        assert re.search(r"Connected to radio", out, re.MULTILINE)
        assert re.search(r"inside mocked showInfo", out, re.MULTILINE)
        assert err == ""
        mo.assert_called()


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_main_no_proto(capsys: pytest.CaptureFixture[str]) -> None:
    """Test --noproto (using --info for output)."""
    sys.argv = ["", "--info", "--noproto"]
    mt_config.args = sys.argv  # type: ignore[assignment]

    iface = MagicMock(autospec=SerialInterface)
    iface.__enter__ = MagicMock(return_value=iface)
    iface.__exit__ = MagicMock(return_value=None)

    def mock_showInfo() -> None:
        """Print a recognizable marker to stdout used by tests to simulate an interface's showInfo().

        This test helper prints the string "inside mocked showInfo" so tests can detect that the mocked showInfo was invoked.
        """
        print("inside mocked showInfo")

    iface.showInfo.side_effect = mock_showInfo

    # Override the time.sleep so there is no loop
    def my_sleep(amount: float) -> None:
        """Print sleep duration and terminate to break the no-proto loop in tests."""
        print(f"amount:{amount}")
        sys.exit(0)

    with patch("meshtastic.serial_interface.SerialInterface", return_value=iface):
        with patch("time.sleep", side_effect=my_sleep):
            with pytest.raises(SystemExit) as pytest_wrapped_e:
                main()
            assert pytest_wrapped_e.type is SystemExit
            assert pytest_wrapped_e.value.code == 0
            out, err = capsys.readouterr()
            assert re.search(r"Connected to radio", out, re.MULTILINE)
            assert re.search(r"inside mocked showInfo", out, re.MULTILINE)
            assert err == ""


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_main_info_with_seriallog_stdout(capsys: pytest.CaptureFixture[str]) -> None:
    """Verify that running the CLI with --info and --seriallog stdout prints connection and info output.

    Asserts that stdout contains "Connected to radio" and the output produced by showInfo, and that nothing is written to stderr.

    """
    sys.argv = ["", "--info", "--seriallog", "stdout"]
    mt_config.args = sys.argv  # type: ignore[assignment]

    iface = MagicMock(autospec=SerialInterface)
    iface.__enter__ = MagicMock(return_value=iface)
    iface.__exit__ = MagicMock(return_value=None)

    def mock_showInfo() -> None:
        """Print a recognizable marker to stdout used by tests to simulate an interface's showInfo().

        This test helper prints the string "inside mocked showInfo" so tests can detect that the mocked showInfo was invoked.
        """
        print("inside mocked showInfo")

    iface.showInfo.side_effect = mock_showInfo
    with patch("meshtastic.serial_interface.SerialInterface", return_value=iface) as mo:
        main()
        out, err = capsys.readouterr()
        assert re.search(r"Connected to radio", out, re.MULTILINE)
        assert re.search(r"inside mocked showInfo", out, re.MULTILINE)
        assert err == ""
        mo.assert_called()


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_main_info_with_seriallog_output_txt(capsys: pytest.CaptureFixture[str]) -> None:
    """Test --info."""
    sys.argv = ["", "--info", "--seriallog", "output.txt"]
    mt_config.args = sys.argv  # type: ignore[assignment]

    iface = MagicMock(autospec=SerialInterface)
    iface.__enter__ = MagicMock(return_value=iface)
    iface.__exit__ = MagicMock(return_value=None)

    def mock_showInfo() -> None:
        """Print a recognizable marker to stdout used by tests to simulate an interface's showInfo().

        This test helper prints the string "inside mocked showInfo" so tests can detect that the mocked showInfo was invoked.
        """
        print("inside mocked showInfo")

    iface.showInfo.side_effect = mock_showInfo
    with patch("meshtastic.serial_interface.SerialInterface", return_value=iface) as mo:
        main()
        out, err = capsys.readouterr()
        assert re.search(r"Connected to radio", out, re.MULTILINE)
        assert re.search(r"inside mocked showInfo", out, re.MULTILINE)
        assert err == ""
        mo.assert_called()
    # do some cleanup
    os.remove("output.txt")


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_main_qr(capsys: pytest.CaptureFixture[str]) -> None:
    """Test --qr."""
    sys.argv = ["", "--qr"]
    mt_config.args = sys.argv  # type: ignore[assignment]

    iface = MagicMock(autospec=SerialInterface)
    iface.__enter__ = MagicMock(return_value=iface)
    iface.__exit__ = MagicMock(return_value=None)
    # TODO: could mock/check url
    with patch("meshtastic.serial_interface.SerialInterface", return_value=iface) as mo:
        main()
        out, err = capsys.readouterr()
        assert re.search(r"Connected to radio", out, re.MULTILINE)
        assert re.search(r"Primary channel URL", out, re.MULTILINE)
        if importlib.util.find_spec("pyqrcode") is None:
            assert re.search(
                r"Install pyqrcode to view a QR code printed to terminal.",
                out,
                re.MULTILINE,
            )
        else:
            # if a qr code is generated it will have lots of these
            assert re.search(r"\[7m", out, re.MULTILINE)
        assert err == ""
        mo.assert_called()


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_main_onConnected_exception(capsys: pytest.CaptureFixture[str]) -> None:
    """Verify that running main with --qr exits with code 1 when QR code generation raises an exception.

    Raises
    ------
    Exception
        Raised by the monkeypatched QR-code function to exercise error handling.
    """
    sys.argv = ["", "--qr"]
    mt_config.args = sys.argv  # type: ignore[assignment]

    def throw_an_exception(_junk: Any) -> None:
        """Raise a deterministic exception used by tests.

        Raises
        ------
        Exception
            A generic Exception with the message "Fake exception.".
        """
        raise Exception("Fake exception.")  # pylint: disable=W0719

    pytest.importorskip("pyqrcode")

    iface = MagicMock(autospec=SerialInterface)
    iface.__enter__ = MagicMock(return_value=iface)
    iface.__exit__ = MagicMock(return_value=None)
    with patch("meshtastic.serial_interface.SerialInterface", return_value=iface):
        with patch("pyqrcode.create", side_effect=throw_an_exception):
            with pytest.raises(SystemExit) as pytest_wrapped_e:
                main()
            _ = capsys.readouterr()  # consume output to avoid polluting test output
            assert pytest_wrapped_e.type is SystemExit
            assert pytest_wrapped_e.value.code == 1


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_main_nodes(capsys: pytest.CaptureFixture[str]) -> None:
    """Verify the CLI --nodes option connects to a radio and prints the node list.

    Asserts that the output contains a "Connected to radio" message, that the mocked
    showNodes output is printed, no stderr is produced, and SerialInterface was instantiated.

    """
    sys.argv = ["", "--nodes"]
    mt_config.args = sys.argv  # type: ignore[assignment]

    iface = MagicMock(autospec=SerialInterface)
    iface.__enter__ = MagicMock(return_value=iface)
    iface.__exit__ = MagicMock(return_value=None)

    def mock_showNodes(includeSelf: bool, showFields: Any) -> None:
        """Print a test marker indicating a mocked node listing and its options.

        Parameters
        ----------
        includeSelf : bool
            Whether the local node would be included in the listing.
        showFields : Any
            Representation of which node fields would be shown; forwarded verbatim into the printed marker.
        """
        print(f"inside mocked showNodes: {includeSelf} {showFields}")

    iface.showNodes.side_effect = mock_showNodes
    with patch("meshtastic.serial_interface.SerialInterface", return_value=iface) as mo:
        main()
        out, err = capsys.readouterr()
        assert re.search(r"Connected to radio", out, re.MULTILINE)
        assert re.search(r"inside mocked showNodes", out, re.MULTILINE)
        assert err == ""
        mo.assert_called()


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_main_set_owner_to_bob(capsys: pytest.CaptureFixture[str]) -> None:
    """Test --set-owner bob."""
    sys.argv = ["", "--set-owner", "bob"]
    mt_config.args = sys.argv  # type: ignore[assignment]

    iface = MagicMock(autospec=SerialInterface)
    iface.__enter__ = MagicMock(return_value=iface)
    iface.__exit__ = MagicMock(return_value=None)
    with patch("meshtastic.serial_interface.SerialInterface", return_value=iface) as mo:
        main()
        out, err = capsys.readouterr()
        assert re.search(r"Connected to radio", out, re.MULTILINE)
        assert re.search(r"Setting device owner to bob", out, re.MULTILINE)
        assert err == ""
        mo.assert_called()


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_main_set_owner_short_to_bob(capsys: pytest.CaptureFixture[str]) -> None:
    """Test --set-owner-short bob."""
    sys.argv = ["", "--set-owner-short", "bob"]
    mt_config.args = sys.argv  # type: ignore[assignment]

    iface = MagicMock(autospec=SerialInterface)
    iface.__enter__ = MagicMock(return_value=iface)
    iface.__exit__ = MagicMock(return_value=None)
    with patch("meshtastic.serial_interface.SerialInterface", return_value=iface) as mo:
        main()
        out, err = capsys.readouterr()
        assert re.search(r"Connected to radio", out, re.MULTILINE)
        assert re.search(r"Setting device owner short to bob", out, re.MULTILINE)
        assert err == ""
        mo.assert_called()


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_main_set_time_with_explicit_timestamp(capsys: pytest.CaptureFixture[str]) -> None:
    """Test --set-time TIMESTAMP forwards the provided epoch value."""
    epoch = 1769686798
    sys.argv = ["", "--set-time", str(epoch)]
    mt_config.args = cast(Any, sys.argv)

    mocked_node = MagicMock()
    iface = MagicMock(autospec=SerialInterface)
    iface.__enter__ = MagicMock(return_value=iface)
    iface.__exit__ = MagicMock(return_value=None)
    iface.getNode.return_value = mocked_node

    with patch("meshtastic.serial_interface.SerialInterface", return_value=iface):
        main()
        out, err = capsys.readouterr()
        assert re.search(r"Connected to radio", out, re.MULTILINE)
        assert err == ""

    mocked_node.setTime.assert_called_once_with(epoch)


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_main_set_time_without_timestamp_uses_zero(capsys: pytest.CaptureFixture[str]) -> None:
    """Test --set-time without argument forwards 0 to trigger node-side current-time behavior."""
    sys.argv = ["", "--set-time"]
    mt_config.args = cast(Any, sys.argv)

    mocked_node = MagicMock()
    iface = MagicMock(autospec=SerialInterface)
    iface.__enter__ = MagicMock(return_value=iface)
    iface.__exit__ = MagicMock(return_value=None)
    iface.getNode.return_value = mocked_node

    with patch("meshtastic.serial_interface.SerialInterface", return_value=iface):
        main()
        out, err = capsys.readouterr()
        assert re.search(r"Connected to radio", out, re.MULTILINE)
        assert err == ""

    mocked_node.setTime.assert_called_once_with(0)


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_main_set_is_unmessageable_to_true(capsys: pytest.CaptureFixture[str]) -> None:
    """Test --set-is-unmessageable true."""
    sys.argv = ["", "--set-is-unmessageable", "true"]
    mt_config.args = sys.argv  # type: ignore[assignment]

    iface = MagicMock(autospec=SerialInterface)
    iface.__enter__ = MagicMock(return_value=iface)
    iface.__exit__ = MagicMock(return_value=None)
    with patch("meshtastic.serial_interface.SerialInterface", return_value=iface) as mo:
        main()
        out, err = capsys.readouterr()
        assert re.search(r"Connected to radio", out, re.MULTILINE)
        assert re.search(
            r"Setting device owner is_unmessageable to True", out, re.MULTILINE
        )
        assert err == ""
        mo.assert_called()


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_main_set_is_unmessagable_to_true(capsys: pytest.CaptureFixture[str]) -> None:
    """Test --set-is-unmessagable true."""
    sys.argv = ["", "--set-is-unmessagable", "true"]
    mt_config.args = sys.argv  # type: ignore[assignment]

    iface = MagicMock(autospec=SerialInterface)
    iface.__enter__ = MagicMock(return_value=iface)
    iface.__exit__ = MagicMock(return_value=None)
    with patch("meshtastic.serial_interface.SerialInterface", return_value=iface) as mo:
        main()
        out, err = capsys.readouterr()
        assert re.search(r"Connected to radio", out, re.MULTILINE)
        assert re.search(
            r"Setting device owner is_unmessageable to True", out, re.MULTILINE
        )
        assert err == ""
        mo.assert_called()


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_main_set_canned_messages(capsys: pytest.CaptureFixture[str]) -> None:
    """Test --set-canned-message."""
    sys.argv = ["", "--set-canned-message", "foo"]
    mt_config.args = sys.argv  # type: ignore[assignment]

    iface = MagicMock(autospec=SerialInterface)
    iface.__enter__ = MagicMock(return_value=iface)
    iface.__exit__ = MagicMock(return_value=None)
    with patch("meshtastic.serial_interface.SerialInterface", return_value=iface) as mo:
        main()
        out, err = capsys.readouterr()
        assert re.search(r"Connected to radio", out, re.MULTILINE)
        assert re.search(r"Setting canned plugin message to foo", out, re.MULTILINE)
        assert err == ""
        mo.assert_called()


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_main_get_canned_messages(capsys: pytest.CaptureFixture[str], caplog: pytest.LogCaptureFixture, iface_with_nodes: Any) -> None:
    """Test --get-canned-message."""
    sys.argv = ["", "--get-canned-message"]
    mt_config.args = sys.argv  # type: ignore[assignment]

    iface = iface_with_nodes
    iface.localNode.cannedPluginMessage = "foo"
    iface.devPath = "bar"

    with caplog.at_level(logging.DEBUG):
        with patch(
            "meshtastic.serial_interface.SerialInterface", return_value=iface
        ) as mo:
            main()
            out, err = capsys.readouterr()
            assert re.search(r"Connected to radio", out, re.MULTILINE)
            assert re.search(r"canned_plugin_message:foo", out, re.MULTILINE)
            assert err == ""
            mo.assert_called()


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_main_set_ringtone(capsys: pytest.CaptureFixture[str]) -> None:
    """Verify the CLI --set-ringtone option instructs the device to set the ringtone and prints confirmation.

    Sets argv to request setting the ringtone, patches the SerialInterface,
    runs main(), and asserts stdout contains "Connected to radio" and
    "Setting ringtone to foo,bar", stderr is empty, and the SerialInterface
    was instantiated.

    """
    sys.argv = ["", "--set-ringtone", "foo,bar"]
    mt_config.args = sys.argv  # type: ignore[assignment]

    iface = MagicMock(autospec=SerialInterface)
    iface.__enter__ = MagicMock(return_value=iface)
    iface.__exit__ = MagicMock(return_value=None)
    with patch("meshtastic.serial_interface.SerialInterface", return_value=iface) as mo:
        main()
        out, err = capsys.readouterr()
        assert re.search(r"Connected to radio", out, re.MULTILINE)
        assert re.search(r"Setting ringtone to foo,bar", out, re.MULTILINE)
        assert err == ""
        mo.assert_called()


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_main_get_ringtone(capsys: pytest.CaptureFixture[str], caplog: pytest.LogCaptureFixture, iface_with_nodes: Any) -> None:
    """Test --get-ringtone."""
    sys.argv = ["", "--get-ringtone"]
    mt_config.args = sys.argv  # type: ignore[assignment]

    iface = iface_with_nodes
    iface.devPath = "bar"

    mocked_node = MagicMock(autospec=Node)
    mocked_node.get_ringtone.return_value = "foo,bar"
    iface.localNode = mocked_node

    with caplog.at_level(logging.DEBUG):
        with patch(
            "meshtastic.serial_interface.SerialInterface", return_value=iface
        ) as mo:
            main()
            out, err = capsys.readouterr()
            assert re.search(r"Connected to radio", out, re.MULTILINE)
            assert re.search(r"ringtone:foo,bar", out, re.MULTILINE)
            assert err == ""
            mo.assert_called()


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_main_set_ham_to_KI123(capsys: pytest.CaptureFixture[str]) -> None:
    """Test --set-ham KI123."""
    sys.argv = ["", "--set-ham", "KI123"]
    mt_config.args = sys.argv  # type: ignore[assignment]

    mocked_node = MagicMock(autospec=Node)

    def mock_turn_off_encryption_on_primary_channel() -> None:
        """Simulate disabling encryption on the primary channel."""
        print("inside mocked turnOffEncryptionOnPrimaryChannel")

    def mock_setOwner(name: str, is_licensed: bool) -> None:
        """Simulate setOwner and print received parameters."""
        print(f"inside mocked setOwner name:{name} is_licensed:{is_licensed}")

    mocked_node.turnOffEncryptionOnPrimaryChannel.side_effect = (
        mock_turn_off_encryption_on_primary_channel
    )
    mocked_node.setOwner.side_effect = mock_setOwner

    iface = MagicMock(autospec=SerialInterface)
    iface.__enter__ = MagicMock(return_value=iface)
    iface.__exit__ = MagicMock(return_value=None)
    iface.getNode.return_value = mocked_node

    with patch("meshtastic.serial_interface.SerialInterface", return_value=iface) as mo:
        main()
        out, err = capsys.readouterr()
        assert re.search(r"Connected to radio", out, re.MULTILINE)
        assert re.search(r"Setting Ham ID to KI123", out, re.MULTILINE)
        assert re.search(r"inside mocked setOwner", out, re.MULTILINE)
        assert re.search(
            r"inside mocked turnOffEncryptionOnPrimaryChannel", out, re.MULTILINE
        )
        assert err == ""
        mo.assert_called()


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_main_reboot(capsys: pytest.CaptureFixture[str]) -> None:
    """Test --reboot."""
    sys.argv = ["", "--reboot"]
    mt_config.args = sys.argv  # type: ignore[assignment]

    mocked_node = MagicMock(autospec=Node)

    def mock_reboot() -> None:
        """Simulate node reboot command."""
        print("inside mocked reboot")

    mocked_node.reboot.side_effect = mock_reboot

    iface = MagicMock(autospec=SerialInterface)
    iface.__enter__ = MagicMock(return_value=iface)
    iface.__exit__ = MagicMock(return_value=None)
    iface.getNode.return_value = mocked_node

    with patch("meshtastic.serial_interface.SerialInterface", return_value=iface) as mo:
        main()
        out, err = capsys.readouterr()
        assert re.search(r"Connected to radio", out, re.MULTILINE)
        assert re.search(r"inside mocked reboot", out, re.MULTILINE)
        assert err == ""
        mo.assert_called()


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_main_shutdown(capsys: pytest.CaptureFixture[str]) -> None:
    """Test --shutdown."""
    sys.argv = ["", "--shutdown"]
    mt_config.args = sys.argv  # type: ignore[assignment]

    mocked_node = MagicMock(autospec=Node)

    def mock_shutdown() -> None:
        """Simulate node shutdown command."""
        print("inside mocked shutdown")

    mocked_node.shutdown.side_effect = mock_shutdown

    iface = MagicMock(autospec=SerialInterface)
    iface.__enter__ = MagicMock(return_value=iface)
    iface.__exit__ = MagicMock(return_value=None)
    iface.getNode.return_value = mocked_node

    with patch("meshtastic.serial_interface.SerialInterface", return_value=iface) as mo:
        main()
        out, err = capsys.readouterr()
        assert re.search(r"Connected to radio", out, re.MULTILINE)
        assert re.search(r"inside mocked shutdown", out, re.MULTILINE)
        assert err == ""
        mo.assert_called()


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_main_sendtext(capsys: pytest.CaptureFixture[str]) -> None:
    """Verify that the CLI `--sendtext` command sends a message through the radio interface and reports progress.

    Runs meshtastic.main() with `--sendtext hello`, patches the SerialInterface to capture sendText calls, and asserts that:
    - the output contains connection and "Sending text message" lines,
    - the mocked sendText was invoked and its debug output appeared on stdout,
    - no stderr output was produced.

    """
    sys.argv = ["", "--sendtext", "hello"]
    mt_config.args = sys.argv  # type: ignore[assignment]

    iface = MagicMock(autospec=SerialInterface)
    iface.__enter__ = MagicMock(return_value=iface)
    iface.__exit__ = MagicMock(return_value=None)
    iface.sendText.side_effect = _mock_sendText_helper

    with patch("meshtastic.serial_interface.SerialInterface", return_value=iface) as mo:
        main()
        out, err = capsys.readouterr()
        assert re.search(r"Connected to radio", out, re.MULTILINE)
        assert re.search(r"Sending text message", out, re.MULTILINE)
        assert re.search(r"inside mocked sendText", out, re.MULTILINE)
        assert err == ""
        mo.assert_called()


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_main_sendtext_with_channel(capsys: pytest.CaptureFixture[str]) -> None:
    """Verify that invoking the CLI with.

    `--sendtext <message> --ch-index <n>` results in a sendText call for the
    specified channel and emits the expected connection and send messages.

    The test sets CLI arguments, replaces SerialInterface with a mock whose
    sendText prints identifiable lines, runs main(), and asserts that stdout
    contains "Connected to radio", a "Sending text message" line referencing
    the channel index, and the mock's output. Uses the pytest `capsys`
    fixture to capture stdout/stderr.

    Parameters
    ----------
    capsys : pytest.CaptureFixture[str]
        Pytest capture fixture for reading stdout and stderr.
    """
    sys.argv = ["", "--sendtext", "hello", "--ch-index", "1"]
    mt_config.args = sys.argv  # type: ignore[assignment]

    iface = MagicMock(autospec=SerialInterface)
    iface.__enter__ = MagicMock(return_value=iface)
    iface.__exit__ = MagicMock(return_value=None)
    iface.sendText.side_effect = _mock_sendText_helper

    with patch("meshtastic.serial_interface.SerialInterface", return_value=iface) as mo:
        main()
        out, err = capsys.readouterr()
        assert re.search(r"Connected to radio", out, re.MULTILINE)
        assert re.search(r"Sending text message", out, re.MULTILINE)
        assert re.search(r"on channelIndex:1", out, re.MULTILINE)
        assert re.search(r"inside mocked sendText", out, re.MULTILINE)
        assert err == ""
        mo.assert_called()


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_main_sendtext_with_invalid_channel(caplog: pytest.LogCaptureFixture, capsys: pytest.CaptureFixture[str]) -> None:
    """Test --sendtext."""
    sys.argv = ["", "--sendtext", "hello", "--ch-index", "-1"]
    mt_config.args = sys.argv  # type: ignore[assignment]

    iface = MagicMock(autospec=SerialInterface)
    iface.__enter__ = MagicMock(return_value=iface)
    iface.__exit__ = MagicMock(return_value=None)
    iface.localNode.getChannelByChannelIndex.return_value = None

    with caplog.at_level(logging.DEBUG):
        with patch(
            "meshtastic.serial_interface.SerialInterface", return_value=iface
        ) as mo:
            with pytest.raises(SystemExit) as pytest_wrapped_e:
                main()
            assert pytest_wrapped_e.type is SystemExit
            assert pytest_wrapped_e.value.code == 1
            _, err = capsys.readouterr()
            # Error messages go to stderr
            assert re.search(r"is not a valid channel", err, re.MULTILINE)
            mo.assert_called()


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_main_sendtext_with_invalid_channel_nine(caplog: pytest.LogCaptureFixture, capsys: pytest.CaptureFixture[str]) -> None:
    """Test --sendtext."""
    sys.argv = ["", "--sendtext", "hello", "--ch-index", "9"]
    mt_config.args = sys.argv  # type: ignore[assignment]

    iface = MagicMock(autospec=SerialInterface)
    iface.__enter__ = MagicMock(return_value=iface)
    iface.__exit__ = MagicMock(return_value=None)
    iface.localNode.getChannelByChannelIndex.return_value = None

    with caplog.at_level(logging.DEBUG):
        with patch(
            "meshtastic.serial_interface.SerialInterface", return_value=iface
        ) as mo:
            with pytest.raises(SystemExit) as pytest_wrapped_e:
                main()
            assert pytest_wrapped_e.type is SystemExit
            assert pytest_wrapped_e.value.code == 1
            _, err = capsys.readouterr()
            # Error messages go to stderr
            assert re.search(r"is not a valid channel", err, re.MULTILINE)
            mo.assert_called()


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
@patch("meshtastic.serial_interface.SerialInterface._set_hupcl_with_termios")
@patch("builtins.open", new_callable=mock_open, read_data="data")
@patch("serial.Serial")
@patch("meshtastic.util.findPorts", return_value=["/dev/ttyUSBfake"])
def test_main_sendtext_with_dest(
    _mock_findPorts: Any,
    _mock_serial: Any,
    _mocked_open: Any,
    _mock_hupcl: Any,
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Test --sendtext with --dest."""
    sys.argv = ["", "--sendtext", "hello", "--dest", "!12345678"]
    mt_config.args = sys.argv  # type: ignore[assignment]

    with SerialInterface(noProto=True, connectNow=False) as serial_interface:
        mocked_channel = MagicMock(autospec=Channel)
        serial_interface.localNode.getChannelByChannelIndex = MagicMock(  # type: ignore[method-assign]
            return_value=mocked_channel
        )

        with patch(
            "meshtastic.serial_interface.SerialInterface", return_value=serial_interface
        ):
            with caplog.at_level(logging.DEBUG):
                # Note: With noProto=True, the packet is not actually sent due to
                # "protocol use is disabled by noProto", so no SystemExit is raised
                main()
                out, err = capsys.readouterr()
                assert re.search(r"Connected to radio", out, re.MULTILINE)
            assert re.search(r"Not sending packet", caplog.text, re.MULTILINE)
            assert err == ""


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_main_removeposition_remote(capsys: pytest.CaptureFixture[str]) -> None:
    """Test --remove-position with a remote dest."""
    sys.argv = ["", "--remove-position", "--dest", "!12345678"]
    mt_config.args = sys.argv  # type: ignore[assignment]
    iface = MagicMock(autospec=SerialInterface)
    iface.__enter__ = MagicMock(return_value=iface)
    iface.__exit__ = MagicMock(return_value=None)
    with patch("meshtastic.serial_interface.SerialInterface", return_value=iface) as mo:
        main()
        out, err = capsys.readouterr()
        assert re.search(r"Connected to radio", out, re.MULTILINE)
        assert re.search(
            r"Removing fixed position and disabling fixed position setting",
            out,
            re.MULTILINE,
        )
        assert re.search(
            r"Waiting for an acknowledgment from remote node", out, re.MULTILINE
        )
        assert err == ""
        mo.assert_called()


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_main_setlat_remote(capsys: pytest.CaptureFixture[str]) -> None:
    """Test --setlat with a remote dest."""
    sys.argv = ["", "--setlat", "37.5", "--dest", "!12345678"]
    mt_config.args = sys.argv  # type: ignore[assignment]
    iface = MagicMock(autospec=SerialInterface)
    iface.__enter__ = MagicMock(return_value=iface)
    iface.__exit__ = MagicMock(return_value=None)
    with patch("meshtastic.serial_interface.SerialInterface", return_value=iface) as mo:
        main()
        out, err = capsys.readouterr()
        assert re.search(r"Connected to radio", out, re.MULTILINE)
        assert re.search(
            r"Setting device position and enabling fixed position setting",
            out,
            re.MULTILINE,
        )
        assert re.search(
            r"Waiting for an acknowledgment from remote node", out, re.MULTILINE
        )
        assert err == ""
        mo.assert_called()


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_main_removeposition(capsys: pytest.CaptureFixture[str]) -> None:
    """Verify that invoking the CLI with --remove-position connects to the radio, removes the node's fixed position, and prints confirmation.

    Asserts that "Connected to radio" and "Removing fixed position" appear on stdout, that the
    node's removeFixedPosition was invoked (observable via its printed output), stderr is empty,
    and a SerialInterface instance was created.

    """
    sys.argv = ["", "--remove-position"]
    mt_config.args = sys.argv  # type: ignore[assignment]

    mocked_node = MagicMock(autospec=Node)

    def mock_removeFixedPosition() -> None:
        """Simulate removing fixed position."""
        print("inside mocked removeFixedPosition")

    mocked_node.removeFixedPosition.side_effect = mock_removeFixedPosition

    iface = MagicMock(autospec=SerialInterface)
    iface.__enter__ = MagicMock(return_value=iface)
    iface.__exit__ = MagicMock(return_value=None)
    iface.getNode.return_value = mocked_node

    with patch("meshtastic.serial_interface.SerialInterface", return_value=iface) as mo:
        main()
        out, err = capsys.readouterr()
        assert re.search(r"Connected to radio", out, re.MULTILINE)
        assert re.search(r"Removing fixed position", out, re.MULTILINE)
        assert re.search(r"inside mocked removeFixedPosition", out, re.MULTILINE)
        assert err == ""
        mo.assert_called()


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_main_setlat(capsys: pytest.CaptureFixture[str]) -> None:
    """Test --setlat."""
    sys.argv = ["", "--setlat", "37.5"]
    mt_config.args = sys.argv  # type: ignore[assignment]

    mocked_node = MagicMock(autospec=Node)

    def mock_setFixedPosition(lat: Any, lon: Any, alt: Any) -> None:
        """Simulate setting fixed position and print provided coordinates."""
        print("inside mocked setFixedPosition")
        print(f"{lat} {lon} {alt}")

    mocked_node.setFixedPosition.side_effect = mock_setFixedPosition

    iface = MagicMock(autospec=SerialInterface)
    iface.__enter__ = MagicMock(return_value=iface)
    iface.__exit__ = MagicMock(return_value=None)
    iface.getNode.return_value = mocked_node

    with patch("meshtastic.serial_interface.SerialInterface", return_value=iface) as mo:
        main()
        out, err = capsys.readouterr()
        assert re.search(r"Connected to radio", out, re.MULTILINE)
        assert re.search(r"Fixing latitude", out, re.MULTILINE)
        assert re.search(r"Setting device position", out, re.MULTILINE)
        assert re.search(r"inside mocked setFixedPosition", out, re.MULTILINE)
        assert err == ""
        mo.assert_called()


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_main_setlon(capsys: pytest.CaptureFixture[str]) -> None:
    """Test --setlon."""
    sys.argv = ["", "--setlon", "-122.1"]
    mt_config.args = sys.argv  # type: ignore[assignment]

    mocked_node = MagicMock(autospec=Node)

    def mock_setFixedPosition(lat: Any, lon: Any, alt: Any) -> None:
        """Simulate setting fixed position and print provided coordinates."""
        print("inside mocked setFixedPosition")
        print(f"{lat} {lon} {alt}")

    mocked_node.setFixedPosition.side_effect = mock_setFixedPosition

    iface = MagicMock(autospec=SerialInterface)
    iface.__enter__ = MagicMock(return_value=iface)
    iface.__exit__ = MagicMock(return_value=None)
    iface.getNode.return_value = mocked_node

    with patch("meshtastic.serial_interface.SerialInterface", return_value=iface) as mo:
        main()
        out, err = capsys.readouterr()
        assert re.search(r"Connected to radio", out, re.MULTILINE)
        assert re.search(r"Fixing longitude", out, re.MULTILINE)
        assert re.search(r"Setting device position", out, re.MULTILINE)
        assert re.search(r"inside mocked setFixedPosition", out, re.MULTILINE)
        assert err == ""
        mo.assert_called()


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_main_setalt(capsys: pytest.CaptureFixture[str]) -> None:
    """Test --setalt."""
    sys.argv = ["", "--setalt", "51"]
    mt_config.args = sys.argv  # type: ignore[assignment]

    mocked_node = MagicMock(autospec=Node)

    def mock_setFixedPosition(lat: Any, lon: Any, alt: Any) -> None:
        """Simulate setting fixed position and print provided coordinates."""
        print("inside mocked setFixedPosition")
        print(f"{lat} {lon} {alt}")

    mocked_node.setFixedPosition.side_effect = mock_setFixedPosition

    iface = MagicMock(autospec=SerialInterface)
    iface.__enter__ = MagicMock(return_value=iface)
    iface.__exit__ = MagicMock(return_value=None)
    iface.getNode.return_value = mocked_node

    with patch("meshtastic.serial_interface.SerialInterface", return_value=iface) as mo:
        main()
        out, err = capsys.readouterr()
        assert re.search(r"Connected to radio", out, re.MULTILINE)
        assert re.search(r"Fixing altitude", out, re.MULTILINE)
        assert re.search(r"Setting device position", out, re.MULTILINE)
        assert re.search(r"inside mocked setFixedPosition", out, re.MULTILINE)
        assert err == ""
        mo.assert_called()


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_main_seturl(capsys: pytest.CaptureFixture[str]) -> None:
    """Test --seturl (url used below is what is generated after a factory_reset)."""
    sys.argv = ["", "--seturl", "https://www.meshtastic.org/d/#CgUYAyIBAQ"]
    mt_config.args = sys.argv  # type: ignore[assignment]

    iface = MagicMock(autospec=SerialInterface)
    iface.__enter__ = MagicMock(return_value=iface)
    iface.__exit__ = MagicMock(return_value=None)
    with patch("meshtastic.serial_interface.SerialInterface", return_value=iface) as mo:
        main()
        out, err = capsys.readouterr()
        assert re.search(r"Connected to radio", out, re.MULTILINE)
        assert err == ""
        mo.assert_called()


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
@patch("meshtastic.serial_interface.SerialInterface._set_hupcl_with_termios")
@patch("builtins.open", new_callable=mock_open, read_data="data")
@patch("serial.Serial")
@patch("meshtastic.util.findPorts", return_value=["/dev/ttyUSBfake"])
def test_main_set_valid(
    _mocked_findports: Any,
    _mocked_serial: Any,
    _mocked_open: Any,
    _mocked_hupcl: Any,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Test --set with valid field."""
    sys.argv = ["", "--set", "network.wifi_ssid", "foo"]
    mt_config.args = sys.argv  # type: ignore[assignment]

    with SerialInterface(noProto=True, connectNow=False) as serialInterface:
        anode = Node(serialInterface, 1234567890, noProto=True)
        serialInterface.localNode = anode

        with patch(
            "meshtastic.serial_interface.SerialInterface", return_value=serialInterface
        ) as mo:
            main()
            out, err = capsys.readouterr()
            assert re.search(r"Connected to radio", out, re.MULTILINE)
            assert re.search(r"Set network.wifi_ssid to foo", out, re.MULTILINE)
            assert err == ""
            mo.assert_called()


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
@patch("meshtastic.serial_interface.SerialInterface._set_hupcl_with_termios")
@patch("builtins.open", new_callable=mock_open, read_data="data")
@patch("serial.Serial")
@patch("meshtastic.util.findPorts", return_value=["/dev/ttyUSBfake"])
def test_main_set_valid_display_use_12_hour_alias(
    _mocked_findports: Any,
    _mocked_serial: Any,
    _mocked_open: Any,
    _mocked_hupcl: Any,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Test --set accepts legacy display.use_12_hour alias."""
    sys.argv = ["", "--set", "display.use_12_hour", "true"]
    mt_config.args = sys.argv  # type: ignore[assignment]

    with SerialInterface(noProto=True, connectNow=False) as serialInterface:
        anode = Node(serialInterface, 1234567890, noProto=True)
        serialInterface.localNode = anode

        with patch(
            "meshtastic.serial_interface.SerialInterface", return_value=serialInterface
        ) as mo:
            main()
            out, err = capsys.readouterr()
            assert re.search(r"Connected to radio", out, re.MULTILINE)
            assert re.search(r"Set display.use_12h_clock to true", out, re.MULTILINE)
            assert anode.localConfig.display.use_12h_clock is True
            assert err == ""
            mo.assert_called()


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
@patch("meshtastic.serial_interface.SerialInterface._set_hupcl_with_termios")
@patch("builtins.open", new_callable=mock_open, read_data="data")
@patch("serial.Serial")
@patch("meshtastic.util.findPorts", return_value=["/dev/ttyUSBfake"])
def test_main_set_valid_wifi_psk(
    _mocked_findports: Any,
    _mocked_serial: Any,
    _mocked_open: Any,
    _mocked_hupcl: Any,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Test --set with valid field."""
    sys.argv = ["", "--set", "network.wifi_psk", "123456789"]
    mt_config.args = sys.argv  # type: ignore[assignment]

    with SerialInterface(noProto=True, connectNow=False) as serialInterface:
        anode = Node(serialInterface, 1234567890, noProto=True)
        serialInterface.localNode = anode

        with patch(
            "meshtastic.serial_interface.SerialInterface", return_value=serialInterface
        ) as mo:
            main()
            out, err = capsys.readouterr()
            assert re.search(r"Connected to radio", out, re.MULTILINE)
            assert re.search(r"Set network.wifi_psk to 123456789", out, re.MULTILINE)
            assert err == ""
            mo.assert_called()


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
@patch("meshtastic.serial_interface.SerialInterface._set_hupcl_with_termios")
@patch("builtins.open", new_callable=mock_open, read_data="data")
@patch("serial.Serial")
@patch("meshtastic.util.findPorts", return_value=["/dev/ttyUSBfake"])
def test_main_set_valid_lora_hop_limit(
    _mocked_findports: Any,
    _mocked_serial: Any,
    _mocked_open: Any,
    _mocked_hupcl: Any,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Test --set lora.hop_limit applies in a single configure write."""
    sys.argv = ["", "--set", "lora.hop_limit", "4"]
    mt_config.args = sys.argv  # type: ignore[assignment]

    with SerialInterface(noProto=True, connectNow=False) as serialInterface:
        anode = Node(serialInterface, 1234567890, noProto=True)
        serialInterface.localNode = anode

        with patch(
            "meshtastic.serial_interface.SerialInterface", return_value=serialInterface
        ):
            main()
            out, err = capsys.readouterr()
            assert re.search(r"Connected to radio", out, re.MULTILINE)
            assert re.search(r"Set lora.hop_limit to 4", out, re.MULTILINE)
            assert re.search(r"Writing lora configuration to device", out, re.MULTILINE)
            assert err == ""

    assert anode.localConfig.lora.hop_limit == 4


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
@patch("meshtastic.serial_interface.SerialInterface._set_hupcl_with_termios")
@patch("builtins.open", new_callable=mock_open, read_data="data")
@patch("serial.Serial")
@patch("meshtastic.util.findPorts", return_value=["/dev/ttyUSBfake"])
def test_main_set_invalid_wifi_psk(
    _mocked_findports: Any,
    _mocked_serial: Any,
    _mocked_open: Any,
    _mocked_hupcl: Any,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Test --set with an invalid value (psk must be 8 or more characters)."""
    sys.argv = ["", "--set", "network.wifi_psk", "1234567"]
    mt_config.args = sys.argv  # type: ignore[assignment]

    with SerialInterface(noProto=True, connectNow=False) as serialInterface:
        anode = Node(serialInterface, 1234567890, noProto=True)
        serialInterface.localNode = anode

        with patch(
            "meshtastic.serial_interface.SerialInterface", return_value=serialInterface
        ) as mo:
            main()
            out, err = capsys.readouterr()
            assert re.search(r"Connected to radio", out, re.MULTILINE)
            assert not re.search(r"Set network.wifi_psk to 1234567", out, re.MULTILINE)
            assert re.search(
                r"Warning: network.wifi_psk must be 8 or more characters.",
                out,
                re.MULTILINE,
            )
            assert err == ""
            mo.assert_called()


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
@patch("meshtastic.serial_interface.SerialInterface._set_hupcl_with_termios")
@patch("builtins.open", new_callable=mock_open, read_data="data")
@patch("serial.Serial")
@patch("meshtastic.util.findPorts", return_value=["/dev/ttyUSBfake"])
def test_main_set_valid_camel_case(
    _mocked_findports: Any,
    _mocked_serial: Any,
    _mocked_open: Any,
    _mocked_hupcl: Any,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Test --set with valid field."""
    sys.argv = ["", "--set", "network.wifi_ssid", "foo"]
    mt_config.args = sys.argv  # type: ignore[assignment]
    mt_config.camel_case = True

    with SerialInterface(noProto=True, connectNow=False) as serialInterface:
        anode = Node(serialInterface, 1234567890, noProto=True)
        serialInterface.localNode = anode

        with patch(
            "meshtastic.serial_interface.SerialInterface", return_value=serialInterface
        ) as mo:
            main()
            out, err = capsys.readouterr()
            assert re.search(r"Connected to radio", out, re.MULTILINE)
            assert re.search(r"Set network.wifiSsid to foo", out, re.MULTILINE)
            assert err == ""
            mo.assert_called()


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
@patch("meshtastic.serial_interface.SerialInterface._set_hupcl_with_termios")
@patch("builtins.open", new_callable=mock_open, read_data="data")
@patch("serial.Serial")
@patch("meshtastic.util.findPorts", return_value=["/dev/ttyUSBfake"])
def test_main_set_with_invalid(
    _mocked_findports: Any,
    _mocked_serial: Any,
    _mocked_open: Any,
    _mocked_hupcl: Any,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Test --set with invalid field."""
    sys.argv = ["", "--set", "foo", "foo"]
    mt_config.args = sys.argv  # type: ignore[assignment]

    with SerialInterface(noProto=True, connectNow=False) as serialInterface:
        anode = Node(serialInterface, 1234567890, noProto=True)
        serialInterface.localNode = anode

        with patch(
            "meshtastic.serial_interface.SerialInterface", return_value=serialInterface
        ) as mo:
            main()
            out, err = capsys.readouterr()
            assert re.search(r"Connected to radio", out, re.MULTILINE)
            assert re.search(r"do not have an attribute foo", out, re.MULTILINE)
            assert err == ""
            mo.assert_called()


# TODO: write some negative --configure tests
@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
@patch("meshtastic.serial_interface.SerialInterface._set_hupcl_with_termios")
@patch("builtins.open", new_callable=mock_open, read_data="data")
@patch("serial.Serial")
@patch("meshtastic.util.findPorts", return_value=["/dev/ttyUSBfake"])
def test_main_configure_with_snake_case(
    _mocked_findports: Any,
    _mocked_serial: Any,
    _mocked_open: Any,
    _mocked_hupcl: Any,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Test --configure with valid file."""
    sys.argv = ["", "--configure", "example_config.yaml"]
    mt_config.args = sys.argv  # type: ignore[assignment]

    with SerialInterface(noProto=True, connectNow=False) as serialInterface:
        anode = Node(serialInterface, 1234567890, noProto=True)
        serialInterface.localNode = anode

        with patch(
            "meshtastic.serial_interface.SerialInterface", return_value=serialInterface
        ) as mo:
            main()
            out, err = capsys.readouterr()
            assert re.search(r"Connected to radio", out, re.MULTILINE)
            # should these come back? maybe a flag?
            # assert re.search(r"Setting device owner", out, re.MULTILINE)
            # assert re.search(r"Setting device owner short", out, re.MULTILINE)
        # assert re.search(r"Setting channel url", out, re.MULTILINE)
        # assert re.search(r"Fixing altitude", out, re.MULTILINE)
        # assert re.search(r"Fixing latitude", out, re.MULTILINE)
        # assert re.search(r"Fixing longitude", out, re.MULTILINE)
        # assert re.search(r"Set location_share to LocEnabled", out, re.MULTILINE)
        assert re.search(r"Writing modified configuration to device", out, re.MULTILINE)
        assert err == ""
        mo.assert_called()


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
@patch("meshtastic.serial_interface.SerialInterface._set_hupcl_with_termios")
@patch("builtins.open", new_callable=mock_open, read_data="data")
@patch("serial.Serial")
@patch("meshtastic.util.findPorts", return_value=["/dev/ttyUSBfake"])
def test_main_configure_with_camel_case_keys(
    _mocked_findports: Any,
    _mocked_serial: Any,
    _mocked_open: Any,
    _mocked_hupcl: Any,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Test --configure with valid file."""
    sys.argv = ["", "--configure", "exampleConfig.yaml"]
    mt_config.args = sys.argv  # type: ignore[assignment]

    with SerialInterface(noProto=True, connectNow=False) as serialInterface:
        anode = Node(serialInterface, 1234567890, noProto=True)
        serialInterface.localNode = anode

        with patch(
            "meshtastic.serial_interface.SerialInterface", return_value=serialInterface
        ) as mo:
            main()
            out, err = capsys.readouterr()
            assert re.search(r"Connected to radio", out, re.MULTILINE)
            # should these come back? maybe a flag?
            # assert re.search(r"Setting device owner", out, re.MULTILINE)
            # assert re.search(r"Setting device owner short", out, re.MULTILINE)
            # assert re.search(r"Setting channel url", out, re.MULTILINE)
            # assert re.search(r"Fixing altitude", out, re.MULTILINE)
            # assert re.search(r"Fixing latitude", out, re.MULTILINE)
            # assert re.search(r"Fixing longitude", out, re.MULTILINE)
            assert re.search(
                r"Writing modified configuration to device", out, re.MULTILINE
            )
            assert err == ""
            mo.assert_called()


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
@pytest.mark.parametrize(
    ("owner_key", "expected_error"),
    [
        (
            "owner",
            "ERROR: Long Name cannot be empty or contain only whitespace characters",
        ),
        (
            "owner_short",
            "ERROR: Short Name cannot be empty or contain only whitespace characters",
        ),
        (
            "ownerShort",
            "ERROR: Short Name cannot be empty or contain only whitespace characters",
        ),
    ],
)
def test_main_configure_rejects_blank_owner_fields(
    owner_key: str,
    expected_error: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Test --configure rejects blank owner fields and exits with a clear message."""
    config_path = tmp_path / "invalid_owner.yaml"
    config_path.write_text(yaml.safe_dump({owner_key: "   "}), encoding="utf-8")
    iface, target_node = _build_configure_interface()

    with pytest.raises(SystemExit) as excinfo:
        _run_main_configure_file(config_path, iface, monkeypatch)

    _, err = capsys.readouterr()
    assert expected_error in err
    assert excinfo.value.code == 1
    target_node.setOwner.assert_not_called()
    target_node.commitSettingsTransaction.assert_not_called()


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_main_configure_rejects_unknown_config_field(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Test --configure fails fast for unknown fields instead of partially applying."""
    config_path = tmp_path / "unknown_field.yaml"
    config_path.write_text(
        yaml.safe_dump({"config": {"bluetooth": {"not_a_field": True}}}),
        encoding="utf-8",
    )
    iface, target_node = _build_configure_interface()

    with pytest.raises(SystemExit) as excinfo:
        _run_main_configure_file(config_path, iface, monkeypatch)

    _, err = capsys.readouterr()
    assert "Failed to apply config section 'bluetooth'" in err
    assert excinfo.value.code == 1
    target_node.writeConfig.assert_not_called()
    target_node.commitSettingsTransaction.assert_not_called()


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_main_configure_rejects_invalid_enum_value(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Test --configure fails fast when enum values are invalid."""
    config_path = tmp_path / "invalid_enum.yaml"
    config_path.write_text(
        yaml.safe_dump({"config": {"bluetooth": {"mode": "NOT_A_MODE"}}}),
        encoding="utf-8",
    )
    iface, target_node = _build_configure_interface()

    with pytest.raises(SystemExit) as excinfo:
        _run_main_configure_file(config_path, iface, monkeypatch)

    out, err = capsys.readouterr()
    assert "does not have an enum called NOT_A_MODE" in out
    assert "Failed to apply config section 'bluetooth'" in err
    assert excinfo.value.code == 1
    target_node.writeConfig.assert_not_called()
    target_node.commitSettingsTransaction.assert_not_called()


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_main_configure_rejects_invalid_security_base64(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Test --configure exits when base64-encoded security keys are malformed."""
    config_path = tmp_path / "invalid_base64.yaml"
    config_path.write_text(
        yaml.safe_dump({"config": {"security": {"privateKey": "base64:A"}}}),
        encoding="utf-8",
    )
    iface, target_node = _build_configure_interface()

    with pytest.raises(SystemExit) as excinfo:
        _run_main_configure_file(config_path, iface, monkeypatch)

    _, err = capsys.readouterr()
    assert "Aborting due to:" in err
    assert "base64" in err.lower()
    assert excinfo.value.code == 1
    target_node.commitSettingsTransaction.assert_not_called()


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_main_configure_applies_mixed_case_and_security_encodings(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Test --configure accepts mixed key casing and supported security key encodings."""
    private_key = bytes(range(32))
    public_key = bytes(range(32, 64))
    admin_key_1 = bytes(range(64, 96))
    admin_key_2 = bytes(range(96, 128))

    config_path = tmp_path / "mixed_case.yaml"
    config_path.write_text(
        yaml.safe_dump(
            {
                "config": {
                    "bluetooth": {
                        "enabled": True,
                        "mode": "NO_PIN",
                        "fixedPin": 777777,
                    },
                    "display": {
                        "units": "IMPERIAL",
                        "use12HClock": True,
                        "screenOnSecs": 66,
                    },
                    "power": {"lsSecs": 222},
                    "security": {
                        "privateKey": f"base64:{base64.b64encode(private_key).decode()}",
                        "public_key": "0x" + public_key.hex(),
                        "adminKey": [
                            f"base64:{base64.b64encode(admin_key_1).decode()}",
                            "0x" + admin_key_2.hex(),
                        ],
                    },
                },
                "module_config": {
                    "telemetry": {
                        "deviceUpdateInterval": 321,
                        "environment_display_fahrenheit": True,
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    target_local = localonly_pb2.LocalConfig()
    target_module = localonly_pb2.LocalModuleConfig()
    iface, target_node = _build_configure_interface(target_local, target_module)
    _run_main_configure_file(config_path, iface, monkeypatch)

    assert target_local.bluetooth.enabled is True
    assert target_local.bluetooth.mode == config_pb2.Config.BluetoothConfig.NO_PIN
    assert target_local.bluetooth.fixed_pin == 777777
    assert target_local.display.units == config_pb2.Config.DisplayConfig.IMPERIAL
    assert target_local.display.use_12h_clock is True
    assert target_local.display.screen_on_secs == 66
    assert target_local.power.ls_secs == 222
    assert target_local.security.private_key == private_key
    assert target_local.security.public_key == public_key
    assert list(target_local.security.admin_key) == [admin_key_1, admin_key_2]
    assert target_module.telemetry.device_update_interval == 321
    assert target_module.telemetry.environment_display_fahrenheit is True

    write_sections = [call.args[0] for call in target_node.writeConfig.call_args_list]
    for required in ("bluetooth", "display", "power", "security", "telemetry"):
        assert required in write_sections


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
@pytest.mark.parametrize(
    "alias_key",
    ["use_12_hour", "use12Hour", "use12hClock", "use12HClock"],
)
def test_main_configure_accepts_display_use_12h_alias_spellings(
    alias_key: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Test --configure accepts all known alias spellings for display.use_12h_clock."""
    config_path = tmp_path / f"display_alias_{alias_key}.yaml"
    config_path.write_text(
        yaml.safe_dump({"config": {"display": {alias_key: True}}}),
        encoding="utf-8",
    )
    target_local = localonly_pb2.LocalConfig()
    iface, _ = _build_configure_interface(
        target_local, localonly_pb2.LocalModuleConfig()
    )
    _run_main_configure_file(config_path, iface, monkeypatch)
    assert target_local.display.use_12h_clock is True


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_main_ch_add_valid(capsys: pytest.CaptureFixture[str]) -> None:
    """Test --ch-add with valid channel name, and that channel name does not already exist."""
    sys.argv = ["", "--ch-add", "testing"]
    mt_config.args = sys.argv  # type: ignore[assignment]

    mocked_channel = MagicMock(autospec=Channel)
    # TODO: figure out how to get it to print the channel name instead of MagicMock

    mocked_node = MagicMock(autospec=Node)
    # set it up so we do not already have a channel named this
    mocked_node.getChannelByName.return_value = False
    # set it up so we have free channels
    mocked_node.getDisabledChannel.return_value = mocked_channel

    iface = MagicMock(autospec=SerialInterface)
    iface.__enter__ = MagicMock(return_value=iface)
    iface.__exit__ = MagicMock(return_value=None)
    iface.getNode.return_value = mocked_node

    with patch("meshtastic.serial_interface.SerialInterface", return_value=iface) as mo:
        main()
        out, err = capsys.readouterr()
        assert re.search(r"Connected to radio", out, re.MULTILINE)
        assert re.search(r"Writing modified channels to device", out, re.MULTILINE)
        assert err == ""
        mo.assert_called()


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_main_ch_add_invalid_name_too_long(capsys: pytest.CaptureFixture[str]) -> None:
    """Test --ch-add with invalid channel name, name too long."""
    sys.argv = ["", "--ch-add", "testingtestingtesting"]
    mt_config.args = sys.argv  # type: ignore[assignment]

    mocked_channel = MagicMock(autospec=Channel)
    # TODO: figure out how to get it to print the channel name instead of MagicMock

    mocked_node = MagicMock(autospec=Node)
    # set it up so we do not already have a channel named this
    mocked_node.getChannelByName.return_value = False
    # set it up so we have free channels
    mocked_node.getDisabledChannel.return_value = mocked_channel

    iface = MagicMock(autospec=SerialInterface)
    iface.__enter__ = MagicMock(return_value=iface)
    iface.__exit__ = MagicMock(return_value=None)
    iface.getNode.return_value = mocked_node

    with patch("meshtastic.serial_interface.SerialInterface", return_value=iface) as mo:
        with pytest.raises(SystemExit) as pytest_wrapped_e:
            main()
        assert pytest_wrapped_e.type is SystemExit
        assert pytest_wrapped_e.value.code == 1
        out, err = capsys.readouterr()
        combined = out + err
        assert re.search(r"Connected to radio", out, re.MULTILINE)
        assert re.search(
            r"Warning: Channel name must be shorter", combined, re.MULTILINE
        )
        mo.assert_called()


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_main_ch_add_but_name_already_exists(capsys: pytest.CaptureFixture[str]) -> None:
    """Test --ch-add with a channel name that already exists."""
    sys.argv = ["", "--ch-add", "testing"]
    mt_config.args = sys.argv  # type: ignore[assignment]

    mocked_node = MagicMock(autospec=Node)
    # set it up so we do not already have a channel named this
    mocked_node.getChannelByName.return_value = True

    iface = MagicMock(autospec=SerialInterface)
    iface.__enter__ = MagicMock(return_value=iface)
    iface.__exit__ = MagicMock(return_value=None)
    iface.getNode.return_value = mocked_node

    with patch("meshtastic.serial_interface.SerialInterface", return_value=iface) as mo:
        with pytest.raises(SystemExit) as pytest_wrapped_e:
            main()
        assert pytest_wrapped_e.type is SystemExit
        assert pytest_wrapped_e.value.code == 1
        out, err = capsys.readouterr()
        combined = out + err
        assert re.search(r"Connected to radio", out, re.MULTILINE)
        assert re.search(r"Warning: This node already has", combined, re.MULTILINE)
        mo.assert_called()


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_main_ch_add_but_no_more_channels(capsys: pytest.CaptureFixture[str]) -> None:
    """Test --ch-add with but there are no more channels."""
    sys.argv = ["", "--ch-add", "testing"]
    mt_config.args = sys.argv  # type: ignore[assignment]

    mocked_node = MagicMock(autospec=Node)
    # set it up so we do not already have a channel named this
    mocked_node.getChannelByName.return_value = False
    # set it up so we have free channels
    mocked_node.getDisabledChannel.return_value = None

    iface = MagicMock(autospec=SerialInterface)
    iface.__enter__ = MagicMock(return_value=iface)
    iface.__exit__ = MagicMock(return_value=None)
    iface.getNode.return_value = mocked_node

    with patch("meshtastic.serial_interface.SerialInterface", return_value=iface) as mo:
        with pytest.raises(SystemExit) as pytest_wrapped_e:
            main()
        assert pytest_wrapped_e.type is SystemExit
        assert pytest_wrapped_e.value.code == 1
        out, err = capsys.readouterr()
        combined = out + err
        assert re.search(r"Connected to radio", out, re.MULTILINE)
        assert re.search(
            r"Warning: No free channels were found", combined, re.MULTILINE
        )
        mo.assert_called()


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_main_ch_del(capsys: pytest.CaptureFixture[str]) -> None:
    """Test --ch-del with valid secondary channel to be deleted."""
    sys.argv = ["", "--ch-del", "--ch-index", "1"]
    mt_config.args = sys.argv  # type: ignore[assignment]

    mocked_node = MagicMock(autospec=Node)

    iface = MagicMock(autospec=SerialInterface)
    iface.__enter__ = MagicMock(return_value=iface)
    iface.__exit__ = MagicMock(return_value=None)
    iface.getNode.return_value = mocked_node

    with patch("meshtastic.serial_interface.SerialInterface", return_value=iface) as mo:
        main()
        out, err = capsys.readouterr()
        assert re.search(r"Connected to radio", out, re.MULTILINE)
        assert re.search(r"Deleting channel", out, re.MULTILINE)
        assert err == ""
        mo.assert_called()


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_main_ch_del_no_ch_index_specified(capsys: pytest.CaptureFixture[str]) -> None:
    """Test --ch-del without a valid ch-index."""
    sys.argv = ["", "--ch-del"]
    mt_config.args = sys.argv  # type: ignore[assignment]

    mocked_node = MagicMock(autospec=Node)

    iface = MagicMock(autospec=SerialInterface)
    iface.__enter__ = MagicMock(return_value=iface)
    iface.__exit__ = MagicMock(return_value=None)
    iface.getNode.return_value = mocked_node

    with patch("meshtastic.serial_interface.SerialInterface", return_value=iface) as mo:
        with pytest.raises(SystemExit) as pytest_wrapped_e:
            main()
        assert pytest_wrapped_e.type is SystemExit
        assert pytest_wrapped_e.value.code == 1
        out, err = capsys.readouterr()
        combined = out + err
        assert re.search(r"Connected to radio", out, re.MULTILINE)
        assert re.search(r"Warning: Need to specify", combined, re.MULTILINE)
        mo.assert_called()


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_main_ch_del_primary_channel(capsys: pytest.CaptureFixture[str]) -> None:
    """Test --ch-del on ch-index=0."""
    sys.argv = ["", "--ch-del", "--ch-index", "0"]
    mt_config.args = sys.argv  # type: ignore[assignment]
    mt_config.channel_index = 1

    mocked_node = MagicMock(autospec=Node)

    iface = MagicMock(autospec=SerialInterface)
    iface.__enter__ = MagicMock(return_value=iface)
    iface.__exit__ = MagicMock(return_value=None)
    iface.getNode.return_value = mocked_node

    with patch("meshtastic.serial_interface.SerialInterface", return_value=iface) as mo:
        with pytest.raises(SystemExit) as pytest_wrapped_e:
            main()
        assert pytest_wrapped_e.type is SystemExit
        assert pytest_wrapped_e.value.code == 1
        out, err = capsys.readouterr()
        combined = out + err
        assert re.search(r"Connected to radio", out, re.MULTILINE)
        assert re.search(
            r"Warning: Cannot delete primary channel", combined, re.MULTILINE
        )
        mo.assert_called()


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_main_ch_enable_valid_secondary_channel(capsys: pytest.CaptureFixture[str]) -> None:
    """Test --ch-enable with --ch-index."""
    sys.argv = ["", "--ch-enable", "--ch-index", "1"]
    mt_config.args = sys.argv  # type: ignore[assignment]

    mocked_node = MagicMock(autospec=Node)

    iface = MagicMock(autospec=SerialInterface)
    iface.__enter__ = MagicMock(return_value=iface)
    iface.__exit__ = MagicMock(return_value=None)
    iface.getNode.return_value = mocked_node

    with patch("meshtastic.serial_interface.SerialInterface", return_value=iface) as mo:
        main()
        out, err = capsys.readouterr()
        assert re.search(r"Connected to radio", out, re.MULTILINE)
        assert re.search(r"Writing modified channels", out, re.MULTILINE)
        assert err == ""
        assert mt_config.channel_index == 1
        mo.assert_called()


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_main_ch_disable_valid_secondary_channel(capsys: pytest.CaptureFixture[str]) -> None:
    """Test --ch-disable with --ch-index."""
    sys.argv = ["", "--ch-disable", "--ch-index", "1"]
    mt_config.args = sys.argv  # type: ignore[assignment]

    mocked_node = MagicMock(autospec=Node)

    iface = MagicMock(autospec=SerialInterface)
    iface.__enter__ = MagicMock(return_value=iface)
    iface.__exit__ = MagicMock(return_value=None)
    iface.getNode.return_value = mocked_node

    with patch("meshtastic.serial_interface.SerialInterface", return_value=iface) as mo:
        main()
        out, err = capsys.readouterr()
        assert re.search(r"Connected to radio", out, re.MULTILINE)
        assert re.search(r"Writing modified channels", out, re.MULTILINE)
        assert err == ""
        assert mt_config.channel_index == 1
        mo.assert_called()


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_main_ch_enable_without_a_ch_index(capsys: pytest.CaptureFixture[str]) -> None:
    """Test --ch-enable without --ch-index."""
    sys.argv = ["", "--ch-enable"]
    mt_config.args = sys.argv  # type: ignore[assignment]

    mocked_node = MagicMock(autospec=Node)

    iface = MagicMock(autospec=SerialInterface)
    iface.__enter__ = MagicMock(return_value=iface)
    iface.__exit__ = MagicMock(return_value=None)
    iface.getNode.return_value = mocked_node

    with patch("meshtastic.serial_interface.SerialInterface", return_value=iface) as mo:
        with pytest.raises(SystemExit) as pytest_wrapped_e:
            main()
        assert pytest_wrapped_e.type is SystemExit
        assert pytest_wrapped_e.value.code == 1
        out, err = capsys.readouterr()
        combined = out + err
        assert re.search(r"Connected to radio", out, re.MULTILINE)
        assert re.search(r"Warning: Need to specify", combined, re.MULTILINE)
        assert mt_config.channel_index is None
        mo.assert_called()


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_main_ch_enable_primary_channel(capsys: pytest.CaptureFixture[str]) -> None:
    """Test --ch-enable with --ch-index = 0."""
    sys.argv = ["", "--ch-enable", "--ch-index", "0"]
    mt_config.args = sys.argv  # type: ignore[assignment]

    mocked_node = MagicMock(autospec=Node)

    iface = MagicMock(autospec=SerialInterface)
    iface.__enter__ = MagicMock(return_value=iface)
    iface.__exit__ = MagicMock(return_value=None)
    iface.getNode.return_value = mocked_node

    with patch("meshtastic.serial_interface.SerialInterface", return_value=iface) as mo:
        with pytest.raises(SystemExit) as pytest_wrapped_e:
            main()
        assert pytest_wrapped_e.type is SystemExit
        assert pytest_wrapped_e.value.code == 1
        out, err = capsys.readouterr()
        combined = out + err
        assert re.search(r"Connected to radio", out, re.MULTILINE)
        assert re.search(
            r"Warning: Cannot enable/disable PRIMARY", combined, re.MULTILINE
        )
        assert mt_config.channel_index == 0
        mo.assert_called()


# TODO
# @pytest.mark.unit
# @pytest.mark.usefixtures("reset_mt_config")
# def test_main_ch_range_options(capsys: pytest.CaptureFixture[str]) -> None:
#    """Test changing the various range options."""
#    range_options = ['--ch-vlongslow', '--ch-longslow', '--ch-longfast', '--ch-midslow',
#                     '--ch-midfast', '--ch-shortslow', '--ch-shortfast']
#    for range_option in range_options:
#        sys.argv = ['', f"{range_option}" ]
#        mt_config.args = sys.argv  # type: ignore[assignment]
#
#        mocked_node = MagicMock(autospec=Node)
#
#        iface = MagicMock(autospec=SerialInterface)
#        iface.getNode.return_value = mocked_node
#
#        with patch('meshtastic.serial_interface.SerialInterface', return_value=iface) as mo:
#            main()
#            out, err = capsys.readouterr()
#            assert re.search(r'Connected to radio', out, re.MULTILINE)
#            assert re.search(r'Writing modified channels', out, re.MULTILINE)
#            assert err == ''
#            mo.assert_called()


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_main_ch_longfast_on_non_primary_channel(capsys: pytest.CaptureFixture[str]) -> None:
    """Verify that invoking the CLI with --ch-longfast and a non-primary.

    --ch-index exits with code 1 and prints a warning that the modem preset
    cannot be set for a non-primary channel while still showing
    "Connected to radio".

    """
    sys.argv = ["", "--ch-longfast", "--ch-index", "1"]
    mt_config.args = sys.argv  # type: ignore[assignment]

    mocked_node = MagicMock(autospec=Node)

    iface = MagicMock(autospec=SerialInterface)
    iface.__enter__ = MagicMock(return_value=iface)
    iface.__exit__ = MagicMock(return_value=None)
    iface.getNode.return_value = mocked_node

    with patch("meshtastic.serial_interface.SerialInterface", return_value=iface) as mo:
        with pytest.raises(SystemExit) as pytest_wrapped_e:
            main()
        assert pytest_wrapped_e.type is SystemExit
        assert pytest_wrapped_e.value.code == 1
        out, err = capsys.readouterr()
        combined = out + err
        assert re.search(r"Connected to radio", out, re.MULTILINE)
        assert re.search(
            r"Warning: Cannot set modem preset for non-primary channel",
            combined,
            re.MULTILINE,
        )
        mo.assert_called()


# PositionFlags:
# Misc info that might be helpful (this info will grow stale, just
# a snapshot of the values.) The radioconfig_pb2.PositionFlags.Name and bit values are:
# POS_UNDEFINED 0
# POS_ALTITUDE 1
# POS_ALT_MSL 2
# POS_GEO_SEP 4
# POS_DOP 8
# POS_HVDOP 16
# POS_BATTERY 32
# POS_SATINVIEW 64
# POS_SEQ_NOS 128
# POS_TIMESTAMP 256

# TODO
# @pytest.mark.unit
# @pytest.mark.usefixtures("reset_mt_config")
# def test_main_pos_fields_no_args(capsys: pytest.CaptureFixture[str]) -> None:
#    """Test --pos-fields no args (which shows settings)"""
#    sys.argv = ['', '--pos-fields']
#    mt_config.args = sys.argv  # type: ignore[assignment]
#
#    pos_flags = MagicMock(autospec=meshtastic.radioconfig_pb2.PositionFlags)
#
#    with patch('meshtastic.serial_interface.SerialInterface') as mo:
#        mo().getNode().radioConfig.preferences.position_flags = 35
#        with patch('meshtastic.radioconfig_pb2.PositionFlags', return_value=pos_flags) as mrc:
#
#            mrc.values.return_value = [0, 1, 2, 4, 8, 16, 32, 64, 128, 256]
#            # Note: When you use side_effect and a list, each call will use a value from the front of the list then
#            # remove that value from the list. If there are three values in the list, we expect it to be called
#            # three times.
#            mrc.Name.side_effect = ['POS_ALTITUDE', 'POS_ALT_MSL', 'POS_BATTERY']
#
#            main()
#
#            mrc.Name.assert_called()
#            mrc.values.assert_called()
#            mo.assert_called()
#
#            out, err = capsys.readouterr()
#            assert re.search(r'Connected to radio', out, re.MULTILINE)
#            assert re.search(r'POS_ALTITUDE POS_ALT_MSL POS_BATTERY', out, re.MULTILINE)
#            assert err == ''


# TODO
# @pytest.mark.unit
# @pytest.mark.usefixtures("reset_mt_config")
# def test_main_pos_fields_arg_of_zero(capsys: pytest.CaptureFixture[str]) -> None:
#    """Test --pos-fields an arg of 0 (which shows list)"""
#    sys.argv = ['', '--pos-fields', '0']
#    mt_config.args = sys.argv  # type: ignore[assignment]
#
#    pos_flags = MagicMock(autospec=meshtastic.radioconfig_pb2.PositionFlags)
#
#    with patch('meshtastic.serial_interface.SerialInterface') as mo:
#        with patch('meshtastic.radioconfig_pb2.PositionFlags', return_value=pos_flags) as mrc:
#
#            def throw_value_error_exception(exc):
#                raise ValueError()
#            mrc.Value.side_effect = throw_value_error_exception
#            mrc.keys.return_value = [ 'POS_UNDEFINED', 'POS_ALTITUDE', 'POS_ALT_MSL',
#                                      'POS_GEO_SEP', 'POS_DOP', 'POS_HVDOP', 'POS_BATTERY',
#                                      'POS_SATINVIEW', 'POS_SEQ_NOS', 'POS_TIMESTAMP']
#
#            main()
#
#            mrc.Value.assert_called()
#            mrc.keys.assert_called()
#            mo.assert_called()
#
#            out, err = capsys.readouterr()
#            assert re.search(r'Connected to radio', out, re.MULTILINE)
#            assert re.search(r'ERROR: supported position fields are:', out, re.MULTILINE)
#            assert re.search(r"['POS_UNDEFINED', 'POS_ALTITUDE', 'POS_ALT_MSL', 'POS_GEO_SEP',"\
#                              "'POS_DOP', 'POS_HVDOP', 'POS_BATTERY', 'POS_SATINVIEW', 'POS_SEQ_NOS',"\
#                              "'POS_TIMESTAMP']", out, re.MULTILINE)
#            assert err == ''


# TODO
# @pytest.mark.unit
# @pytest.mark.usefixtures("reset_mt_config")
# def test_main_pos_fields_valid_values(capsys: pytest.CaptureFixture[str]) -> None:
#    """Test --pos-fields with valid values"""
#    sys.argv = ['', '--pos-fields', 'POS_GEO_SEP', 'POS_ALT_MSL']
#    mt_config.args = sys.argv  # type: ignore[assignment]
#
#    pos_flags = MagicMock(autospec=meshtastic.radioconfig_pb2.PositionFlags)
#
#    with patch('meshtastic.serial_interface.SerialInterface') as mo:
#        with patch('meshtastic.radioconfig_pb2.PositionFlags', return_value=pos_flags) as mrc:
#
#            mrc.Value.side_effect = [ 4, 2 ]
#
#            main()
#
#            mrc.Value.assert_called()
#            mo.assert_called()
#
#            out, err = capsys.readouterr()
#            assert re.search(r'Connected to radio', out, re.MULTILINE)
#            assert re.search(r'Setting position fields to 6', out, re.MULTILINE)
#            assert re.search(r'Set position_flags to 6', out, re.MULTILINE)
#            assert re.search(r'Writing modified preferences to device', out, re.MULTILINE)
#            assert err == ''


# TODO
# @pytest.mark.unit
# @pytest.mark.usefixtures("reset_mt_config")
# def test_main_get_with_valid_values(capsys: pytest.CaptureFixture[str]) -> None:
#    """Test --get with valid values (with string, number, boolean)"""
#    sys.argv = ['', '--get', 'ls_secs', '--get', 'wifi_ssid', '--get', 'fixed_position']
#    mt_config.args = sys.argv  # type: ignore[assignment]
#
#    with patch('meshtastic.serial_interface.SerialInterface') as mo:
#
#        mo().getNode().radioConfig.preferences.wifi_ssid = 'foo'
#        mo().getNode().radioConfig.preferences.ls_secs = 300
#        mo().getNode().radioConfig.preferences.fixed_position = False
#
#        main()
#
#        mo.assert_called()
#
#        out, err = capsys.readouterr()
#        assert re.search(r'Connected to radio', out, re.MULTILINE)
#        assert re.search(r'ls_secs: 300', out, re.MULTILINE)
#        assert re.search(r'wifi_ssid: foo', out, re.MULTILINE)
#        assert re.search(r'fixed_position: False', out, re.MULTILINE)
#        assert err == ''


# TODO
# @pytest.mark.unit
# @pytest.mark.usefixtures("reset_mt_config")
# def test_main_get_with_valid_values_camel(capsys: pytest.CaptureFixture[str], caplog: pytest.LogCaptureFixture) -> None:
#    """Test --get with valid values (with string, number, boolean)"""
#    sys.argv = ["", "--get", "lsSecs", "--get", "wifiSsid", "--get", "fixedPosition"]
#    mt_config.args = sys.argv  # type: ignore[assignment]
#    mt_config.camel_case = True
#
#    with caplog.at_level(logging.DEBUG):
#        with patch("meshtastic.serial_interface.SerialInterface") as mo:
#            mo().getNode().radioConfig.preferences.wifi_ssid = "foo"
#            mo().getNode().radioConfig.preferences.ls_secs = 300
#            mo().getNode().radioConfig.preferences.fixed_position = False
#
#            main()
#
#            mo.assert_called()
#
#            out, err = capsys.readouterr()
#            assert re.search(r"Connected to radio", out, re.MULTILINE)
#            assert re.search(r"lsSecs: 300", out, re.MULTILINE)
#            assert re.search(r"wifiSsid: foo", out, re.MULTILINE)
#            assert re.search(r"fixedPosition: False", out, re.MULTILINE)
#            assert err == ""


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_main_get_with_invalid(capsys: pytest.CaptureFixture[str]) -> None:
    """Test --get with invalid field."""
    sys.argv = ["", "--get", "foo"]
    mt_config.args = sys.argv  # type: ignore[assignment]

    mocked_user_prefs = MagicMock()
    mocked_user_prefs.DESCRIPTOR.fields_by_name.get.return_value = None

    mocked_node = MagicMock(autospec=Node)
    mocked_node.localConfig = mocked_user_prefs
    mocked_node.moduleConfig = mocked_user_prefs

    iface = MagicMock(autospec=SerialInterface)
    iface.__enter__ = MagicMock(return_value=iface)
    iface.__exit__ = MagicMock(return_value=None)
    iface.getNode.return_value = mocked_node

    with patch("meshtastic.serial_interface.SerialInterface", return_value=iface) as mo:
        main()
        out, err = capsys.readouterr()
        assert re.search(r"Connected to radio", out, re.MULTILINE)
        assert re.search(r"do not have an attribute foo", out, re.MULTILINE)
        assert re.search(r"Choices are...", out, re.MULTILINE)
        assert err == ""
        mo.assert_called()


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_main_onReceive_empty(caplog: pytest.LogCaptureFixture, capsys: pytest.CaptureFixture[str]) -> None:
    """Test onReceive with empty packet - should handle gracefully without error."""
    args = MagicMock()
    mt_config.args = args
    iface = MagicMock(autospec=SerialInterface)
    iface.__enter__ = MagicMock(return_value=iface)
    iface.__exit__ = MagicMock(return_value=None)
    # Need 'decoded' to be truthy so the code path reaches packet.get("to")
    packet: dict[str, Any] = {"decoded": {}}
    with caplog.at_level(logging.DEBUG):
        onReceive(packet, iface)
    assert re.search(r"in onReceive", caplog.text, re.MULTILINE)
    out, err = capsys.readouterr()
    # Should not print any warnings - packet.get("to") returns None gracefully
    assert out == ""
    assert err == ""


#    TODO: use this captured position app message (might want/need in the future)
#    packet = {
#            'to': 4294967295,
#            'decoded': {
#                'portnum': 'POSITION_APP',
#                'payload': "M69\306a"
#                },
#            'id': 334776976,
#            'hop_limit': 3
#            }


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_main_onReceive_with_sendtext(caplog: pytest.LogCaptureFixture, capsys: pytest.CaptureFixture[str]) -> None:
    """Test onReceive with sendtext.

    The entire point of this test is to make sure the interface.close() call
    is made in onReceive().

    """
    sys.argv = ["", "--sendtext", "hello"]
    mt_config.args = sys.argv  # type: ignore[assignment]

    # Note: 'TEXT_MESSAGE_APP' value is 1
    packet = {
        "to": 4294967295,
        "decoded": {"portnum": "TEXT_MESSAGE_APP", "payload": "hello"},
        "id": 334776977,
        "hop_limit": 3,
        "want_ack": True,
    }

    iface = MagicMock(autospec=SerialInterface)
    iface.__enter__ = MagicMock(return_value=iface)
    iface.__exit__ = MagicMock(return_value=None)
    iface.myInfo.my_node_num = 4294967295

    with patch("meshtastic.serial_interface.SerialInterface", return_value=iface) as mo:
        with caplog.at_level(logging.DEBUG):
            main()
            onReceive(packet, iface)
        assert re.search(r"in onReceive", caplog.text, re.MULTILINE)
        mo.assert_called()
        out, err = capsys.readouterr()
        assert re.search(r"Sending text message hello to", out, re.MULTILINE)
        assert err == ""


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_main_onReceive_with_text(caplog: pytest.LogCaptureFixture, capsys: pytest.CaptureFixture[str]) -> None:
    """Test onReceive with text."""
    args = MagicMock()
    args.sendtext.return_value = "foo"
    mt_config.args = args

    # Note: 'TEXT_MESSAGE_APP' value is 1
    # Note: Some of this is faked below.
    packet = {
        "to": 4294967295,
        "decoded": {"portnum": "TEXT_MESSAGE_APP", "payload": "hello", "text": "faked"},
        "id": 334776977,
        "hop_limit": 3,
        "want_ack": True,
        "rxSnr": 6.0,
        "hopLimit": 3,
        "raw": "faked",
        "fromId": "!28b5465c",
        "toId": "^all",
    }

    iface = MagicMock(autospec=SerialInterface)
    iface.__enter__ = MagicMock(return_value=iface)
    iface.__exit__ = MagicMock(return_value=None)
    iface.myInfo.my_node_num = 4294967295

    with patch("meshtastic.serial_interface.SerialInterface", return_value=iface):
        with caplog.at_level(logging.DEBUG):
            onReceive(packet, iface)
        assert re.search(r"in onReceive", caplog.text, re.MULTILINE)
        out, err = capsys.readouterr()
        assert re.search(r"Sending reply", out, re.MULTILINE)
        assert err == ""


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_main_onConnection(capsys: pytest.CaptureFixture[str]) -> None:
    """Test onConnection."""
    sys.argv = [""]
    mt_config.args = sys.argv  # type: ignore[assignment]
    iface = MagicMock(autospec=SerialInterface)
    iface.__enter__ = MagicMock(return_value=iface)
    iface.__exit__ = MagicMock(return_value=None)

    class TempTopic:
        """temp class for topic."""

        def getName(self) -> str:
            """Get a fake topic name.

            Returns
            -------
            str
                The fixed fake topic name `'foo'`.
            """
            return "foo"

    mytopic = TempTopic()
    onConnection(iface, mytopic)
    out, err = capsys.readouterr()
    assert re.search(r"Connection changed: foo", out, re.MULTILINE)
    assert err == ""


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_main_onConnection_with_non_topic(capsys: pytest.CaptureFixture[str]) -> None:
    """Test onConnection with non-topic objects."""
    sys.argv = [""]
    mt_config.args = sys.argv  # type: ignore[assignment]
    iface = MagicMock(autospec=SerialInterface)
    iface.__enter__ = MagicMock(return_value=iface)
    iface.__exit__ = MagicMock(return_value=None)
    onConnection(iface, topic="raw-topic")
    out, err = capsys.readouterr()
    assert re.search(r"Connection changed: raw-topic", out, re.MULTILINE)
    assert err == ""


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_main_export_config(capsys: pytest.CaptureFixture[str]) -> None:
    """Test export_config() function directly."""
    iface = MagicMock(autospec=SerialInterface)
    iface.__enter__ = MagicMock(return_value=iface)
    iface.__exit__ = MagicMock(return_value=None)
    with patch("meshtastic.serial_interface.SerialInterface", return_value=iface) as mo:
        mo.getLongName.return_value = "foo"
        mo.getShortName.return_value = "oof"
        mo.localNode.getURL.return_value = "bar"
        mo.getCannedMessage.return_value = "foo|bar"
        mo.getRingtone.return_value = "24:d=32,o=5"
        mo.getMyNodeInfo().get.return_value = {
            "latitudeI": 1100000000,
            "longitudeI": 1200000000,
            "altitude": 100,
            "batteryLevel": 34,
            "latitude": 110.0,
            "longitude": 120.0,
        }
        mo.localNode.radioConfig.preferences = """phone_timeout_secs: 900
ls_secs: 300
position_broadcast_smart: true
fixed_position: true
position_flags: 35"""
        export_config(mo)
    out = export_config(mo)
    _, err = capsys.readouterr()

    # ensure we do not output this line
    assert not re.search(r"Connected to radio", out, re.MULTILINE)

    assert re.search(r"owner: foo", out, re.MULTILINE)
    assert re.search(r"owner_short: oof", out, re.MULTILINE)
    assert re.search(r"channel_url: bar", out, re.MULTILINE)
    assert re.search(r"location:", out, re.MULTILINE)
    assert re.search(r"lat: 110.0", out, re.MULTILINE)
    assert re.search(r"lon: 120.0", out, re.MULTILINE)
    assert re.search(r"alt: 100", out, re.MULTILINE)
    # TODO: rework above config to test the following
    # assert re.search(r"user_prefs:", out, re.MULTILINE)
    # assert re.search(r"phone_timeout_secs: 900", out, re.MULTILINE)
    # assert re.search(r"ls_secs: 300", out, re.MULTILINE)
    # assert re.search(r"position_broadcast_smart: 'true'", out, re.MULTILINE)
    # assert re.search(r"fixed_position: 'true'", out, re.MULTILINE)
    # assert re.search(r"position_flags: 35", out, re.MULTILINE)
    assert err == ""


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_export_config_omits_empty_optional_fields() -> None:
    """Test export_config omits optional top-level fields when values are empty/missing."""
    iface = _build_export_interface(
        localonly_pb2.LocalConfig(), localonly_pb2.LocalModuleConfig()
    )
    iface.getLongName.return_value = ""
    iface.getShortName.return_value = ""
    iface.localNode.getURL.return_value = ""
    iface.getCannedMessage.return_value = ""
    iface.getRingtone.return_value = ""
    iface.getMyNodeInfo.return_value = {}

    exported = yaml.safe_load(export_config(iface))

    assert "owner" not in exported
    assert "owner_short" not in exported
    assert "channel_url" not in exported
    assert "canned_messages" not in exported
    assert "ringtone" not in exported
    assert "location" not in exported


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_export_config_sets_missing_true_default_flags_false() -> None:
    """Test export_config explicitly writes known true-default flags as false when missing."""
    source_local = localonly_pb2.LocalConfig()
    source_module = localonly_pb2.LocalModuleConfig()
    source_local.display.units = config_pb2.Config.DisplayConfig.IMPERIAL
    source_module.telemetry.device_update_interval = 1

    exported = yaml.safe_load(
        export_config(_build_export_interface(source_local, source_module))
    )
    config = exported["config"]
    module_config = exported["module_config"]

    assert config["bluetooth"]["enabled"] is False
    assert config["lora"]["sx126xRxBoostedGain"] is False
    assert config["lora"]["txEnabled"] is False
    assert config["lora"]["usePreset"] is False
    assert config["position"]["positionBroadcastSmartEnabled"] is False
    assert config["security"]["serialEnabled"] is False
    assert module_config["mqtt"]["encryptionEnabled"] is False


def _build_export_interface(
    local_config: localonly_pb2.LocalConfig,
    module_config: localonly_pb2.LocalModuleConfig,
) -> MagicMock:
    """Build a minimal interface mock compatible with export_config().

    Parameters
    ----------
    local_config : localonly_pb2.LocalConfig
        Local device configuration to attach to the mocked interface.
    module_config : localonly_pb2.LocalModuleConfig
        Module configuration to attach to the mocked interface.

    Returns
    -------
    MagicMock
        A MagicMock instance wired up with localConfig, moduleConfig, and helper return values.
    """
    iface = MagicMock(autospec=SerialInterface)
    iface.localNode = MagicMock()
    iface.localNode.localConfig = local_config
    iface.localNode.moduleConfig = module_config
    iface.localNode.getURL.return_value = "https://meshtastic.org/e/#Cgo"
    iface.getLongName.return_value = "Roundtrip Node"
    iface.getShortName.return_value = "RT"
    iface.getMyNodeInfo.return_value = {}
    iface.getCannedMessage.return_value = ""
    iface.getRingtone.return_value = ""
    return iface


def _build_configure_interface(
    target_local: localonly_pb2.LocalConfig | None = None,
    target_module: localonly_pb2.LocalModuleConfig | None = None,
) -> tuple[MagicMock, MagicMock]:
    """Build a minimal interface mock compatible with --configure operations."""
    if target_local is None:
        target_local = localonly_pb2.LocalConfig()
    if target_module is None:
        target_module = localonly_pb2.LocalModuleConfig()

    target_node = MagicMock()
    target_node.localConfig = target_local
    target_node.moduleConfig = target_module
    target_node.beginSettingsTransaction = MagicMock()
    target_node.commitSettingsTransaction = MagicMock()
    target_node.setOwner = MagicMock()
    target_node.setURL = MagicMock()
    target_node.set_canned_message = MagicMock()
    target_node.set_ringtone = MagicMock()
    target_node.writeConfig = MagicMock()
    target_node.setFixedPosition = MagicMock()

    iface = MagicMock(autospec=SerialInterface)
    iface.__enter__ = MagicMock(return_value=iface)
    iface.__exit__ = MagicMock(return_value=None)
    iface.getNode.return_value = target_node
    iface.localNode = target_node
    return iface, target_node


def _run_main_configure_file(
    config_path: Path,
    iface: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Run main() for --configure against a supplied YAML file and interface mock."""
    monkeypatch.setattr("time.sleep", lambda _: None)
    sys.argv = ["", "--configure", str(config_path)]
    mt_config.args = cast(Any, sys.argv)
    with patch("meshtastic.serial_interface.SerialInterface", return_value=iface):
        main()


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_export_config_configure_round_trip_security_keys(capsys: pytest.CaptureFixture[str]) -> None:
    """Ensure export->configure->export preserves security keys and structure."""
    source_local = localonly_pb2.LocalConfig()
    source_module = localonly_pb2.LocalModuleConfig()
    source_local.bluetooth.enabled = True
    source_local.security.serial_enabled = True
    source_local.security.private_key = b"\x01" * 32
    source_local.security.public_key = b"\x02" * 32
    source_local.security.admin_key.extend([b"\x03" * 32, b"\x04" * 32])
    source_module.mqtt.address = "mqtt.meshtastic.org"

    exported_yaml = export_config(_build_export_interface(source_local, source_module))
    exported = yaml.safe_load(exported_yaml)
    security = exported["config"]["security"]
    assert security["privateKey"].startswith("base64:")
    assert security["publicKey"].startswith("base64:")
    assert all(
        isinstance(item, str) and item.startswith("base64:")
        for item in security["adminKey"]
    )
    assert "base64:base64:" not in security["privateKey"]
    assert "base64:base64:" not in security["publicKey"]

    restored_local = localonly_pb2.LocalConfig()
    restored_module = localonly_pb2.LocalModuleConfig()
    for section, values in exported["config"].items():
        traverseConfig(section, values, restored_local)
    for section, values in exported["module_config"].items():
        traverseConfig(section, values, restored_module)

    assert restored_local.security.private_key == source_local.security.private_key
    assert restored_local.security.public_key == source_local.security.public_key
    assert list(restored_local.security.admin_key) == list(
        source_local.security.admin_key
    )

    exported_round_trip = yaml.safe_load(
        export_config(_build_export_interface(restored_local, restored_module))
    )
    assert exported_round_trip == exported
    _, err = capsys.readouterr()
    assert err == ""


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_main_export_config_and_configure_round_trip_nonstandard(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Round-trip --export-config/--configure with nonstandard fully-configured settings."""
    source_local = localonly_pb2.LocalConfig()
    source_module = localonly_pb2.LocalModuleConfig()

    source_local.bluetooth.enabled = True
    source_local.bluetooth.mode = config_pb2.Config.BluetoothConfig.NO_PIN
    source_local.bluetooth.fixed_pin = 654321
    source_local.display.units = config_pb2.Config.DisplayConfig.IMPERIAL
    source_local.display.use_12h_clock = True
    source_local.display.screen_on_secs = 45
    source_local.power.ls_secs = 111
    source_local.security.serial_enabled = True
    source_local.security.private_key = b"\xaa" * 32
    source_local.security.public_key = b"\xbb" * 32
    source_local.security.admin_key.extend([b"\xcc" * 32, b"\xdd" * 32])

    source_module.telemetry.device_update_interval = 321
    source_module.telemetry.environment_display_fahrenheit = True
    source_module.remote_hardware.enabled = True

    export_iface = _build_export_interface(source_local, source_module)
    export_iface.__enter__ = MagicMock(return_value=export_iface)
    export_iface.__exit__ = MagicMock(return_value=None)
    export_iface.getCannedMessage.return_value = "Alpha|Bravo|Charlie"
    export_iface.getRingtone.return_value = "24:d=16,o=5,b=100:c"
    export_iface.localNode.getURL.return_value = "https://meshtastic.org/e/#CgYSAQABAA"
    export_iface.getMyNodeInfo.return_value = {
        "position": {"latitude": 12.345, "longitude": -98.765, "altitude": 432}
    }

    export_path = tmp_path / "roundtrip_config.yaml"
    sys.argv = ["", "--export-config", str(export_path)]
    mt_config.args = cast(Any, sys.argv)
    with patch(
        "meshtastic.serial_interface.SerialInterface", return_value=export_iface
    ):
        main()

    exported = yaml.safe_load(export_path.read_text(encoding="utf-8"))
    assert exported["owner"] == "Roundtrip Node"
    assert exported["owner_short"] == "RT"
    assert exported["channel_url"] == "https://meshtastic.org/e/#CgYSAQABAA"
    assert exported["canned_messages"] == "Alpha|Bravo|Charlie"
    assert exported["ringtone"] == "24:d=16,o=5,b=100:c"
    assert exported["location"] == {"lat": 12.345, "lon": -98.765, "alt": 432}
    bluetooth_cfg = exported["config"]["bluetooth"]
    display_cfg = exported["config"]["display"]
    power_cfg = exported["config"]["power"]
    telemetry_cfg = exported["module_config"]["telemetry"]
    assert bluetooth_cfg["mode"] == "NO_PIN"
    assert bluetooth_cfg.get("fixed_pin", bluetooth_cfg.get("fixedPin")) == 654321
    assert display_cfg["units"] == "IMPERIAL"
    assert display_cfg.get("use_12h_clock", display_cfg.get("use12hClock")) is True
    assert power_cfg.get("ls_secs", power_cfg.get("lsSecs")) == 111
    assert exported["config"]["security"]["privateKey"].startswith("base64:")
    assert exported["config"]["security"]["publicKey"].startswith("base64:")
    assert all(
        isinstance(v, str) and v.startswith("base64:")
        for v in exported["config"]["security"]["adminKey"]
    )
    assert (
        telemetry_cfg.get(
            "device_update_interval", telemetry_cfg.get("deviceUpdateInterval")
        )
        == 321
    )
    assert (
        telemetry_cfg.get(
            "environment_display_fahrenheit",
            telemetry_cfg.get("environmentDisplayFahrenheit"),
        )
        is True
    )
    assert exported["module_config"]["remote_hardware"]["enabled"] is True

    target_local = localonly_pb2.LocalConfig()
    target_module = localonly_pb2.LocalModuleConfig()
    target_node = MagicMock()
    target_node.localConfig = target_local
    target_node.moduleConfig = target_module
    target_node.beginSettingsTransaction = MagicMock()
    target_node.commitSettingsTransaction = MagicMock()
    target_node.setOwner = MagicMock()
    target_node.setURL = MagicMock()
    target_node.set_canned_message = MagicMock()
    target_node.set_ringtone = MagicMock()
    target_node.writeConfig = MagicMock()
    target_node.setFixedPosition = MagicMock()

    configure_iface = MagicMock(autospec=SerialInterface)
    configure_iface.__enter__ = MagicMock(return_value=configure_iface)
    configure_iface.__exit__ = MagicMock(return_value=None)
    configure_iface.getNode.return_value = target_node
    configure_iface.localNode = target_node

    monkeypatch.setattr("time.sleep", lambda _: None)
    sys.argv = ["", "--configure", str(export_path)]
    mt_config.args = cast(Any, sys.argv)
    with patch(
        "meshtastic.serial_interface.SerialInterface", return_value=configure_iface
    ):
        main()

    target_node.beginSettingsTransaction.assert_called_once()
    target_node.commitSettingsTransaction.assert_called_once()
    assert target_node.setOwner.call_count == 2
    target_node.setURL.assert_called_once_with("https://meshtastic.org/e/#CgYSAQABAA")
    target_node.set_canned_message.assert_called_once_with("Alpha|Bravo|Charlie")
    target_node.set_ringtone.assert_called_once_with("24:d=16,o=5,b=100:c")
    target_node.setFixedPosition.assert_called_once_with(12.345, -98.765, 432)

    assert target_local.bluetooth.enabled is True
    assert target_local.bluetooth.mode == config_pb2.Config.BluetoothConfig.NO_PIN
    assert target_local.bluetooth.fixed_pin == 654321
    assert target_local.display.units == config_pb2.Config.DisplayConfig.IMPERIAL
    assert target_local.display.use_12h_clock is True
    assert target_local.display.screen_on_secs == 45
    assert target_local.power.ls_secs == 111
    assert target_local.security.serial_enabled is True
    assert target_local.security.private_key == source_local.security.private_key
    assert target_local.security.public_key == source_local.security.public_key
    assert list(target_local.security.admin_key) == list(
        source_local.security.admin_key
    )

    assert target_module.telemetry.device_update_interval == 321
    assert target_module.telemetry.environment_display_fahrenheit is True
    assert target_module.remote_hardware.enabled is True

    write_sections = [c.args[0] for c in target_node.writeConfig.call_args_list]
    for required in (
        "bluetooth",
        "display",
        "power",
        "security",
        "telemetry",
        "remote_hardware",
    ):
        assert required in write_sections

    out, err = capsys.readouterr()
    assert re.search(r"Exported configuration to", out, re.MULTILINE)
    assert re.search(r"Writing modified configuration to device", out, re.MULTILINE)
    assert err == ""


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_export_config_round_trip_with_camel_case_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Test export->traverse round trip when mt_config.camel_case is enabled."""
    source_local = localonly_pb2.LocalConfig()
    source_module = localonly_pb2.LocalModuleConfig()
    source_local.display.use_12h_clock = True
    source_local.power.ls_secs = 123
    source_local.security.serial_enabled = True
    source_module.telemetry.device_update_interval = 77

    monkeypatch.setattr(mt_config, "camel_case", True)
    exported = yaml.safe_load(
        export_config(_build_export_interface(source_local, source_module))
    )

    assert "channelUrl" in exported
    assert exported["config"]["display"]["use12hClock"] is True
    assert exported["config"]["power"]["lsSecs"] == 123
    assert exported["config"]["security"]["serialEnabled"] is True
    assert exported["module_config"]["telemetry"]["deviceUpdateInterval"] == 77

    restored_local = localonly_pb2.LocalConfig()
    restored_module = localonly_pb2.LocalModuleConfig()
    for section, values in exported["config"].items():
        assert traverseConfig(section, values, restored_local)
    for section, values in exported["module_config"].items():
        assert traverseConfig(section, values, restored_module)

    assert restored_local.display.use_12h_clock is True
    assert restored_local.power.ls_secs == 123
    assert restored_local.security.serial_enabled is True
    assert restored_module.telemetry.device_update_interval == 77


@pytest.mark.unit
def test_prefix_base64_key_skips_existing_prefixes() -> None:
    """Ensure _prefix_base64_key does not double-prefix already-normalized values."""
    security = {
        "privateKey": "base64:abc123==",
        "adminKey": ["base64:def456==", "ghi789==", 7],
    }
    normalized_key_map = {
        "privateKey": "privateKey",
        "adminKey": "adminKey",
    }
    _prefix_base64_key(security, normalized_key_map, "privateKey")
    _prefix_base64_key(security, normalized_key_map, "adminKey")

    assert security["privateKey"] == "base64:abc123=="
    assert security["adminKey"] == ["base64:def456==", "base64:ghi789==", 7]


# TODO
# recursion depth exceeded error
# @pytest.mark.unit
# @pytest.mark.usefixtures("reset_mt_config")
# def test_main_export_config_use_camel(capsys: pytest.CaptureFixture[str]) -> None:
#    """Test export_config() function directly"""
#    mt_config.camel_case = True
#    iface = MagicMock(autospec=SerialInterface)
#    with patch("meshtastic.serial_interface.SerialInterface", return_value=iface) as mo:
#        mo.getLongName.return_value = "foo"
#        mo.localNode.getURL.return_value = "bar"
#        mo.getMyNodeInfo().get.return_value = {
#            "latitudeI": 1100000000,
#            "longitudeI": 1200000000,
#            "altitude": 100,
#            "batteryLevel": 34,
#            "latitude": 110.0,
#            "longitude": 120.0,
#        }
#        mo.localNode.radioConfig.preferences = """phone_timeout_secs: 900
# ls_secs: 300
# position_broadcast_smart: true
# fixed_position: true
# position_flags: 35"""
#        export_config(mo)
#    out, err = capsys.readouterr()
#
#    # ensure we do not output this line
#    assert not re.search(r"Connected to radio", out, re.MULTILINE)
#
#    assert re.search(r"owner: foo", out, re.MULTILINE)
#    assert re.search(r"channelUrl: bar", out, re.MULTILINE)
#    assert re.search(r"location:", out, re.MULTILINE)
#    assert re.search(r"lat: 110.0", out, re.MULTILINE)
#    assert re.search(r"lon: 120.0", out, re.MULTILINE)
#    assert re.search(r"alt: 100", out, re.MULTILINE)
#    assert re.search(r"userPrefs:", out, re.MULTILINE)
#    assert re.search(r"phoneTimeoutSecs: 900", out, re.MULTILINE)
#    assert re.search(r"lsSecs: 300", out, re.MULTILINE)
#    # TODO: should True be capitalized here?
#    assert re.search(r"positionBroadcastSmart: 'True'", out, re.MULTILINE)
#    assert re.search(r"fixedPosition: 'True'", out, re.MULTILINE)
#    assert re.search(r"positionFlags: 35", out, re.MULTILINE)
#    assert err == ""


# TODO
# maximum recursion depth error
# @pytest.mark.unit
# @pytest.mark.usefixtures("reset_mt_config")
# def test_main_export_config_called_from_main(capsys: pytest.CaptureFixture[str]) -> None:
#    """Test --export-config"""
#    sys.argv = ["", "--export-config"]
#    mt_config.args = sys.argv  # type: ignore[assignment]
#
#    iface = MagicMock(autospec=SerialInterface)
#    with patch("meshtastic.serial_interface.SerialInterface", return_value=iface) as mo:
#        main()
#        out, err = capsys.readouterr()
#        assert not re.search(r"Connected to radio", out, re.MULTILINE)
#        assert re.search(r"# start of Meshtastic configure yaml", out, re.MULTILINE)
#        assert err == ""
#        mo.assert_called()


@pytest.mark.unit
def test_set_missing_flags_false() -> None:
    """Test _set_missing_flags_false() function."""
    config = {"bluetooth": {"enabled": True}, "lora": {"txEnabled": True}}

    false_defaults: set[tuple[str, ...]] = {
        ("bluetooth", "enabled"),
        ("lora", "sx126xRxBoostedGain"),
        ("lora", "txEnabled"),
        ("lora", "usePreset"),
        ("position", "positionBroadcastSmartEnabled"),
        ("security", "serialEnabled"),
        ("mqtt", "encryptionEnabled"),
    }

    _set_missing_flags_false(config, false_defaults)

    # Preserved
    assert config["bluetooth"]["enabled"] is True
    assert config["lora"]["txEnabled"] is True

    # Added
    assert config["lora"]["usePreset"] is False
    assert config["lora"]["sx126xRxBoostedGain"] is False
    assert config["position"]["positionBroadcastSmartEnabled"] is False
    assert config["security"]["serialEnabled"] is False
    assert config["mqtt"]["encryptionEnabled"] is False


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_main_gpio_rd_no_gpio_channel(capsys: pytest.CaptureFixture[str]) -> None:
    """Test --gpio_rd with no named gpio channel."""
    sys.argv = ["", "--gpio-rd", "0x10", "--dest", "!foo"]
    mt_config.args = sys.argv  # type: ignore[assignment]

    iface = MagicMock(autospec=SerialInterface)
    iface.__enter__ = MagicMock(return_value=iface)
    iface.__exit__ = MagicMock(return_value=None)
    iface.localNode.getChannelByName.return_value = None
    with patch("meshtastic.serial_interface.SerialInterface", return_value=iface):
        with pytest.raises(SystemExit) as pytest_wrapped_e:
            main()
        assert pytest_wrapped_e.type is SystemExit
        assert pytest_wrapped_e.value.code == 1
        out, err = capsys.readouterr()
        # Error messages go to stderr, stdout contains "Connected to radio"
        assert re.search(r"No channel named 'gpio'", err)
        assert "Connected to radio" in out


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_main_gpio_rd_no_dest(capsys: pytest.CaptureFixture[str]) -> None:
    """Test --gpio_rd with a named gpio channel but no dest was specified."""
    sys.argv = ["", "--gpio-rd", "0x2000"]
    mt_config.args = sys.argv  # type: ignore[assignment]

    channel = Channel(index=2, role=Channel.Role.SECONDARY)
    channel.settings.psk = b"\x8a\x94y\x0e\xc6\xc9\x1e5\x91\x12@\xa60\xa8\xb43\x87\x00\xf2K\x0e\xe7\x7fAz\xcd\xf5\xb0\x900\xa84"
    channel.settings.name = "gpio"

    iface = MagicMock(autospec=SerialInterface)
    iface.__enter__ = MagicMock(return_value=iface)
    iface.__exit__ = MagicMock(return_value=None)
    iface.localNode.getChannelByName.return_value = channel
    with patch("meshtastic.serial_interface.SerialInterface", return_value=iface):
        with pytest.raises(SystemExit) as pytest_wrapped_e:
            main()
        assert pytest_wrapped_e.type is SystemExit
        assert pytest_wrapped_e.value.code == 1
        out, err = capsys.readouterr()
        combined = out + err
        assert re.search(r"Warning: Must use a destination node ID", combined)


# TODO
# @pytest.mark.unit
# @pytest.mark.usefixtures("reset_mt_config")
# @patch('time.sleep')
# def test_main_gpio_rd(caplog, capsys):
#    """Test --gpio_rd with a named gpio channel"""
#    # Note: On the Heltec v2.1, there is a GPIO pin GPIO 13 that does not have a
#    # red arrow (meaning ok to use for our purposes)
#    # See https://resource.heltec.cn/download/WiFi_LoRa_32/WIFI_LoRa_32_V2.pdf
#    # To find out the mask for GPIO 13, let us assign n as 13.
#    # 1. Find the 2^n or 2^13 (8192)
#    # 2. Convert 8192 decimal to hex (0x2000)
#    # You can use python:
#    # >>> print(hex(2**13))
#    # 0x2000
#    sys.argv = ['', '--gpio-rd', '0x1000', '--dest', '!1234']
#    mt_config.args = sys.argv  # type: ignore[assignment]
#
#    channel = Channel(index=1, role=1)
#    channel.settings.modem_config = 3
#    channel.settings.psk = b'\x01'
#
#    packet = {
#
#            'from': 682968668,
#            'to': 682968612,
#            'channel': 1,
#            'decoded': {
#                'portnum': 'REMOTE_HARDWARE_APP',
#                'payload': b'\x08\x05\x18\x80 ',
#                'requestId': 1629980484,
#                'remotehw': {
#                    'typ': 'READ_GPIOS_REPLY',
#                    'gpioValue': '4096',
#                    'raw': 'faked',
#                    'id': 1693085229,
#                    'rxTime': 1640294262,
#                    'rxSnr': 4.75,
#                    'hopLimit': 3,
#                    'wantAck': True,
#                    }
#                }
#            }
#
#    iface = MagicMock(autospec=SerialInterface)
#    iface.localNode.getChannelByName.return_value = channel
#    with patch('meshtastic.serial_interface.SerialInterface', return_value=iface) as mo:
#        with caplog.at_level(logging.DEBUG):
#            main()
#            onGPIOreceive(packet, mo)
#    out, err = capsys.readouterr()
#    assert re.search(r'Connected to radio', out, re.MULTILINE)
#    assert re.search(r'Reading GPIO mask 0x1000 ', out, re.MULTILINE)
#    assert re.search(r'Received RemoteHardware typ=READ_GPIOS_REPLY, gpio_value=4096', out, re.MULTILINE)
#    assert err == ''


# TODO
# @pytest.mark.unit
# @pytest.mark.usefixtures("reset_mt_config")
# @patch('time.sleep')
# def test_main_gpio_rd_with_no_gpioMask(caplog, capsys):
#    """Test --gpio_rd with a named gpio channel"""
#    sys.argv = ['', '--gpio-rd', '0x1000', '--dest', '!1234']
#    mt_config.args = sys.argv  # type: ignore[assignment]
#
#    channel = Channel(index=1, role=1)
#    channel.settings.modem_config = 3
#    channel.settings.psk = b'\x01'
#
#    # Note: Intentionally do not have gpioValue in response as that is the
#    # default value
#    packet = {
#            'from': 682968668,
#            'to': 682968612,
#            'channel': 1,
#            'decoded': {
#                'portnum': 'REMOTE_HARDWARE_APP',
#                'payload': b'\x08\x05\x18\x80 ',
#                'requestId': 1629980484,
#                'remotehw': {
#                    'typ': 'READ_GPIOS_REPLY',
#                    'raw': 'faked',
#                    'id': 1693085229,
#                    'rxTime': 1640294262,
#                    'rxSnr': 4.75,
#                    'hopLimit': 3,
#                    'wantAck': True,
#                    }
#                }
#            }
#
#    iface = MagicMock(autospec=SerialInterface)
#    iface.localNode.getChannelByName.return_value = channel
#    with patch('meshtastic.serial_interface.SerialInterface', return_value=iface) as mo:
#        with caplog.at_level(logging.DEBUG):
#            main()
#            onGPIOreceive(packet, mo)
#    out, err = capsys.readouterr()
#    assert re.search(r'Connected to radio', out, re.MULTILINE)
#    assert re.search(r'Reading GPIO mask 0x1000 ', out, re.MULTILINE)
#    assert re.search(r'Received RemoteHardware typ=READ_GPIOS_REPLY, gpio_value=0', out, re.MULTILINE)
#    assert err == ''


# TODO
# @pytest.mark.unit
# @pytest.mark.usefixtures("reset_mt_config")
# def test_main_gpio_watch(caplog, capsys):
#    """Test --gpio_watch with a named gpio channel"""
#    sys.argv = ['', '--gpio-watch', '0x1000', '--dest', '!1234']
#    mt_config.args = sys.argv  # type: ignore[assignment]
#
#    def my_sleep(amount: float) -> None:
#        print(f'{amount}')
#        sys.exit(3)
#
#    channel = Channel(index=1, role=1)
#    channel.settings.modem_config = 3
#    channel.settings.psk = b'\x01'
#
#    packet = {
#
#            'from': 682968668,
#            'to': 682968612,
#            'channel': 1,
#            'decoded': {
#                'portnum': 'REMOTE_HARDWARE_APP',
#                'payload': b'\x08\x05\x18\x80 ',
#                'requestId': 1629980484,
#                'remotehw': {
#                    'typ': 'READ_GPIOS_REPLY',
#                    'gpioValue': '4096',
#                    'raw': 'faked',
#                    'id': 1693085229,
#                    'rxTime': 1640294262,
#                    'rxSnr': 4.75,
#                    'hopLimit': 3,
#                    'wantAck': True,
#                    }
#                }
#            }
#
#    with patch('time.sleep', side_effect=my_sleep):
#        with pytest.raises(SystemExit) as pytest_wrapped_e:
#            iface = MagicMock(autospec=SerialInterface)
#            iface.localNode.getChannelByName.return_value = channel
#            with patch('meshtastic.serial_interface.SerialInterface', return_value=iface) as mo:
#                with caplog.at_level(logging.DEBUG):
#                    main()
#                    onGPIOreceive(packet, mo)
#        assert pytest_wrapped_e.type is SystemExit
#        assert pytest_wrapped_e.value.code == 3
#        out, err = capsys.readouterr()
#        assert re.search(r'Connected to radio', out, re.MULTILINE)
#        assert re.search(r'Watching GPIO mask 0x1000 ', out, re.MULTILINE)
#        assert err == ''


# TODO
# @pytest.mark.unit
# @pytest.mark.usefixtures("reset_mt_config")
# def test_main_gpio_wrb(caplog, capsys):
#    """Test --gpio_wrb with a named gpio channel"""
#    sys.argv = ['', '--gpio-wrb', '4', '1', '--dest', '!1234']
#    mt_config.args = sys.argv  # type: ignore[assignment]
#
#    channel = Channel(index=1, role=1)
#    channel.settings.modem_config = 3
#    channel.settings.psk = b'\x01'
#
#    packet = {
#
#            'from': 682968668,
#            'to': 682968612,
#            'channel': 1,
#            'decoded': {
#                'portnum': 'REMOTE_HARDWARE_APP',
#                'payload': b'\x08\x05\x18\x80 ',
#                'requestId': 1629980484,
#                'remotehw': {
#                    'typ': 'READ_GPIOS_REPLY',
#                    'gpioValue': '16',
#                    'raw': 'faked',
#                    'id': 1693085229,
#                    'rxTime': 1640294262,
#                    'rxSnr': 4.75,
#                    'hopLimit': 3,
#                    'wantAck': True,
#                    }
#                }
#            }
#
#
#    iface = MagicMock(autospec=SerialInterface)
#    iface.localNode.getChannelByName.return_value = channel
#    with patch('meshtastic.serial_interface.SerialInterface', return_value=iface) as mo:
#        with caplog.at_level(logging.DEBUG):
#            main()
#            onGPIOreceive(packet, mo)
#    out, err = capsys.readouterr()
#    assert re.search(r'Connected to radio', out, re.MULTILINE)
#    assert re.search(r'Writing GPIO mask 0x10 with value 0x10 to !1234', out, re.MULTILINE)
#    assert re.search(r'Received RemoteHardware typ=READ_GPIOS_REPLY, gpio_value=16 value=0', out, re.MULTILINE)
#    assert err == ''


# TODO
# need to restructure these for nested configs
# @pytest.mark.unit
# @pytest.mark.usefixtures("reset_mt_config")
# def test_main_getPref_valid_field(capsys: pytest.CaptureFixture[str]) -> None:
#    """Test getPref() with a valid field"""
#    prefs = MagicMock()
#    prefs.DESCRIPTOR.fields_by_name.get.return_value = "ls_secs"
#    prefs.wifi_ssid = "foo"
#    prefs.ls_secs = 300
#    prefs.fixed_position = False
#
#    getPref(prefs, "ls_secs")
#    out, err = capsys.readouterr()
#    assert re.search(r"ls_secs: 300", out, re.MULTILINE)
#    assert err == ""
#
#
# @pytest.mark.unit
# @pytest.mark.usefixtures("reset_mt_config")
# def test_main_getPref_valid_field_camel(capsys: pytest.CaptureFixture[str]) -> None:
#    """Test getPref() with a valid field"""
#    mt_config.camel_case = True
#    prefs = MagicMock()
#    prefs.DESCRIPTOR.fields_by_name.get.return_value = "ls_secs"
#    prefs.wifi_ssid = "foo"
#    prefs.ls_secs = 300
#    prefs.fixed_position = False
#
#    getPref(prefs, "ls_secs")
#    out, err = capsys.readouterr()
#    assert re.search(r"lsSecs: 300", out, re.MULTILINE)
#    assert err == ""
#
#
# @pytest.mark.unit
# @pytest.mark.usefixtures("reset_mt_config")
# def test_main_getPref_valid_field_string(capsys: pytest.CaptureFixture[str]) -> None:
#    """Test getPref() with a valid field and value as a string"""
#    prefs = MagicMock()
#    prefs.DESCRIPTOR.fields_by_name.get.return_value = "wifi_ssid"
#    prefs.wifi_ssid = "foo"
#    prefs.ls_secs = 300
#    prefs.fixed_position = False
#
#    getPref(prefs, "wifi_ssid")
#    out, err = capsys.readouterr()
#    assert re.search(r"wifi_ssid: foo", out, re.MULTILINE)
#    assert err == ""
#
#
# @pytest.mark.unit
# @pytest.mark.usefixtures("reset_mt_config")
# def test_main_getPref_valid_field_string_camel(capsys: pytest.CaptureFixture[str]) -> None:
#    """Test getPref() with a valid field and value as a string"""
#    mt_config.camel_case = True
#    prefs = MagicMock()
#    prefs.DESCRIPTOR.fields_by_name.get.return_value = "wifi_ssid"
#    prefs.wifi_ssid = "foo"
#    prefs.ls_secs = 300
#    prefs.fixed_position = False
#
#    getPref(prefs, "wifi_ssid")
#    out, err = capsys.readouterr()
#    assert re.search(r"wifiSsid: foo", out, re.MULTILINE)
#    assert err == ""
#
#
# @pytest.mark.unit
# @pytest.mark.usefixtures("reset_mt_config")
# def test_main_getPref_valid_field_bool(capsys: pytest.CaptureFixture[str]) -> None:
#    """Test getPref() with a valid field and value as a bool"""
#    prefs = MagicMock()
#    prefs.DESCRIPTOR.fields_by_name.get.return_value = "fixed_position"
#    prefs.wifi_ssid = "foo"
#    prefs.ls_secs = 300
#    prefs.fixed_position = False
#
#    getPref(prefs, "fixed_position")
#    out, err = capsys.readouterr()
#    assert re.search(r"fixed_position: False", out, re.MULTILINE)
#    assert err == ""
#
#
# @pytest.mark.unit
# @pytest.mark.usefixtures("reset_mt_config")
# def test_main_getPref_valid_field_bool_camel(capsys: pytest.CaptureFixture[str]) -> None:
#    """Test getPref() with a valid field and value as a bool"""
#    mt_config.camel_case = True
#    prefs = MagicMock()
#    prefs.DESCRIPTOR.fields_by_name.get.return_value = "fixed_position"
#    prefs.wifi_ssid = "foo"
#    prefs.ls_secs = 300
#    prefs.fixed_position = False
#
#    getPref(prefs, "fixed_position")
#    out, err = capsys.readouterr()
#    assert re.search(r"fixedPosition: False", out, re.MULTILINE)
#    assert err == ""
#
#
# @pytest.mark.unit
# @pytest.mark.usefixtures("reset_mt_config")
# def test_main_getPref_invalid_field(capsys: pytest.CaptureFixture[str]) -> None:
#    """Test getPref() with an invalid field"""
#
#    class Field:
#        """Simple class for testing."""
#
#        def __init__(self, name):
#            """constructor"""
#            self.name = name
#
#    prefs = MagicMock()
#    prefs.DESCRIPTOR.fields_by_name.get.return_value = None
#
#    # Note: This is a subset of the real fields
#    ls_secs_field = Field("ls_secs")
#    is_router = Field("is_router")
#    fixed_position = Field("fixed_position")
#
#    fields = [ls_secs_field, is_router, fixed_position]
#    prefs.DESCRIPTOR.fields = fields
#
#    getPref(prefs, "foo")
#
#    out, err = capsys.readouterr()
#    assert re.search(r"does not have an attribute called foo", out, re.MULTILINE)
#    # ensure they are sorted
#    assert re.search(r"fixed_position\s+is_router\s+ls_secs", out, re.MULTILINE)
#    assert err == ""
#
#
# @pytest.mark.unit
# @pytest.mark.usefixtures("reset_mt_config")
# def test_main_getPref_invalid_field_camel(capsys: pytest.CaptureFixture[str]) -> None:
#    """Test getPref() with an invalid field"""
#    mt_config.camel_case = True
#
#    class Field:
#        """Simple class for testing."""
#
#        def __init__(self, name):
#            """constructor"""
#            self.name = name
#
#    prefs = MagicMock()
#    prefs.DESCRIPTOR.fields_by_name.get.return_value = None
#
#    # Note: This is a subset of the real fields
#    ls_secs_field = Field("ls_secs")
#    is_router = Field("is_router")
#    fixed_position = Field("fixed_position")
#
#    fields = [ls_secs_field, is_router, fixed_position]
#    prefs.DESCRIPTOR.fields = fields
#
#    getPref(prefs, "foo")
#
#    out, err = capsys.readouterr()
#    assert re.search(r"does not have an attribute called foo", out, re.MULTILINE)
#    # ensure they are sorted
#    assert re.search(r"fixedPosition\s+isRouter\s+lsSecs", out, re.MULTILINE)
#    assert err == ""
#
#
# @pytest.mark.unit
# @pytest.mark.usefixtures("reset_mt_config")
# def test_main_setPref_valid_field_int_as_string(capsys: pytest.CaptureFixture[str]) -> None:
#    """Test setPref() with a valid field"""
#
#    class Field:
#        """Simple class for testing."""
#
#        def __init__(self, name, enum_type):
#            """constructor"""
#            self.name = name
#            self.enum_type = enum_type
#
#    ls_secs_field = Field("ls_secs", "int")
#    prefs = MagicMock()
#    prefs.DESCRIPTOR.fields_by_name.get.return_value = ls_secs_field
#
#    setPref(prefs, "ls_secs", "300")
#    out, err = capsys.readouterr()
#    assert re.search(r"Set ls_secs to 300", out, re.MULTILINE)
#    assert err == ""


# TODO
# @pytest.mark.unit
# @pytest.mark.usefixtures("reset_mt_config")
# def test_main_setPref_valid_field_invalid_enum(capsys: pytest.CaptureFixture[str], caplog: pytest.LogCaptureFixture) -> None:
#    """Test setPref() with a valid field but invalid enum value"""
#
#    radioConfig = RadioConfig()
#    prefs = radioConfig.preferences
#
#    with caplog.at_level(logging.DEBUG):
#        setPref(prefs, 'charge_current', 'foo')
#        out, err = capsys.readouterr()
#        assert re.search(r'charge_current does not have an enum called foo', out, re.MULTILINE)
#        assert re.search(r'Choices in sorted order are', out, re.MULTILINE)
#        assert re.search(r'MA100', out, re.MULTILINE)
#        assert re.search(r'MA280', out, re.MULTILINE)
#        assert err == ''


# TODO
# @pytest.mark.unit
# @pytest.mark.usefixtures("reset_mt_config")
# def test_main_setPref_valid_field_invalid_enum_where_enums_are_camel_cased_values(capsys: pytest.CaptureFixture[str], caplog: pytest.LogCaptureFixture) -> None:
#    """Test setPref() with a valid field but invalid enum value"""
#
#    radioConfig = RadioConfig()
#    prefs = radioConfig.preferences
#
#    with caplog.at_level(logging.DEBUG):
#        setPref(prefs, 'region', 'foo')
#        out, err = capsys.readouterr()
#        assert re.search(r'region does not have an enum called foo', out, re.MULTILINE)
#        assert re.search(r'Choices in sorted order are', out, re.MULTILINE)
#        assert re.search(r'ANZ', out, re.MULTILINE)
#        assert re.search(r'CN', out, re.MULTILINE)
#        assert err == ''


# TODO
# @pytest.mark.unit
# @pytest.mark.usefixtures("reset_mt_config")
# def test_main_setPref_valid_field_invalid_enum_camel(capsys: pytest.CaptureFixture[str], caplog: pytest.LogCaptureFixture) -> None:
#    """Test setPref() with a valid field but invalid enum value"""
#    mt_config.camel_case = True
#
#    radioConfig = RadioConfig()
#    prefs = radioConfig.preferences
#
#    with caplog.at_level(logging.DEBUG):
#        setPref(prefs, 'charge_current', 'foo')
#        out, err = capsys.readouterr()
#        assert re.search(r'chargeCurrent does not have an enum called foo', out, re.MULTILINE)
#        assert err == ''


# TODO
# @pytest.mark.unit
# @pytest.mark.usefixtures("reset_mt_config")
# def test_main_setPref_valid_field_valid_enum(capsys: pytest.CaptureFixture[str], caplog: pytest.LogCaptureFixture) -> None:
#    """Test setPref() with a valid field and valid enum value"""
#
#    # charge_current
#    # some valid values:   MA100 MA1000 MA1080
#
#    radioConfig = RadioConfig()
#    prefs = radioConfig.preferences
#
#    with caplog.at_level(logging.DEBUG):
#        setPref(prefs, 'charge_current', 'MA100')
#        out, err = capsys.readouterr()
#        assert re.search(r'Set charge_current to MA100', out, re.MULTILINE)
#        assert err == ''


# TODO
# @pytest.mark.unit
# @pytest.mark.usefixtures("reset_mt_config")
# def test_main_setPref_valid_field_valid_enum_camel(capsys: pytest.CaptureFixture[str], caplog: pytest.LogCaptureFixture) -> None:
#    """Test setPref() with a valid field and valid enum value"""
#    mt_config.camel_case = True
#
#    # charge_current
#    # some valid values:   MA100 MA1000 MA1080
#
#    radioConfig = RadioConfig()
#    prefs = radioConfig.preferences
#
#    with caplog.at_level(logging.DEBUG):
#        setPref(prefs, 'charge_current', 'MA100')
#        out, err = capsys.readouterr()
#        assert re.search(r'Set chargeCurrent to MA100', out, re.MULTILINE)
#        assert err == ''

# TODO
# need to update for nested configs
# @pytest.mark.unit
# @pytest.mark.usefixtures("reset_mt_config")
# def test_main_setPref_invalid_field(capsys: pytest.CaptureFixture[str]) -> None:
#    """Test setPref() with a invalid field"""
#
#    class Field:
#        """Simple class for testing."""
#
#        def __init__(self, name):
#            """constructor"""
#            self.name = name
#
#    prefs = MagicMock()
#    prefs.DESCRIPTOR.fields_by_name.get.return_value = None
#
#    # Note: This is a subset of the real fields
#    ls_secs_field = Field("ls_secs")
#    is_router = Field("is_router")
#    fixed_position = Field("fixed_position")
#
#    fields = [ls_secs_field, is_router, fixed_position]
#    prefs.DESCRIPTOR.fields = fields
#
#    setPref(prefs, "foo", "300")
#    out, err = capsys.readouterr()
#    assert re.search(r"does not have an attribute called foo", out, re.MULTILINE)
#    # ensure they are sorted
#    assert re.search(r"fixed_position\s+is_router\s+ls_secs", out, re.MULTILINE)
#    assert err == ""
#
#
# @pytest.mark.unit
# @pytest.mark.usefixtures("reset_mt_config")
# def test_main_setPref_invalid_field_camel(capsys: pytest.CaptureFixture[str]) -> None:
#    """Test setPref() with a invalid field"""
#    mt_config.camel_case = True
#
#    class Field:
#        """Simple class for testing."""
#
#        def __init__(self, name):
#            """constructor"""
#            self.name = name
#
#    prefs = MagicMock()
#    prefs.DESCRIPTOR.fields_by_name.get.return_value = None
#
#    # Note: This is a subset of the real fields
#    ls_secs_field = Field("ls_secs")
#    is_router = Field("is_router")
#    fixed_position = Field("fixed_position")
#
#    fields = [ls_secs_field, is_router, fixed_position]
#    prefs.DESCRIPTOR.fields = fields
#
#    setPref(prefs, "foo", "300")
#    out, err = capsys.readouterr()
#    assert re.search(r"does not have an attribute called foo", out, re.MULTILINE)
#    # ensure they are sorted
#    assert re.search(r"fixedPosition\s+isRouter\s+lsSecs", out, re.MULTILINE)
#    assert err == ""
#
#
# @pytest.mark.unit
# @pytest.mark.usefixtures("reset_mt_config")
# def test_main_setPref_ignore_incoming_123(capsys):
#    """Test setPref() with ignore_incoming"""
#
#    class Field:
#        """Simple class for testing."""
#
#        def __init__(self, name, enum_type):
#            """constructor"""
#            self.name = name
#            self.enum_type = enum_type
#
#    ignore_incoming_field = Field("ignore_incoming", "list")
#    prefs = MagicMock()
#    prefs.DESCRIPTOR.fields_by_name.get.return_value = ignore_incoming_field
#
#    setPref(prefs, "ignore_incoming", "123")
#    out, err = capsys.readouterr()
#    assert re.search(r"Adding '123' to the ignore_incoming list", out, re.MULTILINE)
#    assert re.search(r"Set ignore_incoming to 123", out, re.MULTILINE)
#    assert err == ""
#
#
# @pytest.mark.unit
# @pytest.mark.usefixtures("reset_mt_config")
# def test_main_setPref_ignore_incoming_0(capsys):
#    """Test setPref() with ignore_incoming"""
#
#    class Field:
#        """Simple class for testing."""
#
#        def __init__(self, name, enum_type):
#            """constructor"""
#            self.name = name
#            self.enum_type = enum_type
#
#    ignore_incoming_field = Field("ignore_incoming", "list")
#    prefs = MagicMock()
#    prefs.DESCRIPTOR.fields_by_name.get.return_value = ignore_incoming_field
#
#    setPref(prefs, "ignore_incoming", "0")
#    out, err = capsys.readouterr()
#    assert re.search(r"Clearing ignore_incoming list", out, re.MULTILINE)
#    assert re.search(r"Set ignore_incoming to 0", out, re.MULTILINE)
#    assert err == ""


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_main_ch_set_psk_no_ch_index(capsys: pytest.CaptureFixture[str]) -> None:
    """Verify that invoking the CLI with `--ch-set psk` but without a `--ch-index` prints a warning and exits with code 1.

    Asserts that the tool reports a successful connection, emits a warning that `--ch-index` must
    be specified, produces no stderr output, and raises SystemExit with code 1.

    """
    sys.argv = ["", "--ch-set", "psk", "foo", "--host", "meshtastic.local"]
    mt_config.args = sys.argv  # type: ignore[assignment]

    iface = MagicMock(autospec=TCPInterface)
    iface.__enter__ = MagicMock(return_value=iface)
    iface.__exit__ = MagicMock(return_value=None)
    with patch("meshtastic.tcp_interface.TCPInterface", return_value=iface) as mo:
        with pytest.raises(SystemExit) as pytest_wrapped_e:
            main()
        out, err = capsys.readouterr()
        combined = out + err
        assert re.search(r"Connected to radio", out, re.MULTILINE)
        assert re.search(
            r"Warning: Need to specify '--ch-index'", combined, re.MULTILINE
        )
        assert pytest_wrapped_e.type is SystemExit
        assert pytest_wrapped_e.value.code == 1
        mo.assert_called()


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_main_ch_set_psk_with_ch_index(capsys: pytest.CaptureFixture[str]) -> None:
    """Test --ch-set psk."""
    sys.argv = [
        "",
        "--ch-set",
        "psk",
        "foo",
        "--host",
        "meshtastic.local",
        "--ch-index",
        "0",
    ]
    mt_config.args = sys.argv  # type: ignore[assignment]

    iface = MagicMock(autospec=TCPInterface)
    iface.__enter__ = MagicMock(return_value=iface)
    iface.__exit__ = MagicMock(return_value=None)
    with patch("meshtastic.tcp_interface.TCPInterface", return_value=iface) as mo:
        main()
    out, err = capsys.readouterr()
    assert re.search(r"Connected to radio", out, re.MULTILINE)
    assert re.search(r"Writing modified channels to device", out, re.MULTILINE)
    assert err == ""
    mo.assert_called()


# TODO
# doesn't work properly with nested/module config stuff
# @pytest.mark.unit
# @pytest.mark.usefixtures("reset_mt_config")
# def test_main_ch_set_name_with_ch_index(capsys: pytest.CaptureFixture[str]) -> None:
#    """Test --ch-set setting other than psk"""
#    sys.argv = [
#        "",
#        "--ch-set",
#        "name",
#        "foo",
#        "--host",
#        "meshtastic.local",
#        "--ch-index",
#        "0",
#    ]
#    mt_config.args = sys.argv  # type: ignore[assignment]
#
#    iface = MagicMock(autospec=TCPInterface)
#    with patch("meshtastic.tcp_interface.TCPInterface", return_value=iface) as mo:
#        main()
#    out, err = capsys.readouterr()
#    assert re.search(r"Connected to radio", out, re.MULTILINE)
#    assert re.search(r"Set name to foo", out, re.MULTILINE)
#    assert re.search(r"Writing modified channels to device", out, re.MULTILINE)
#    assert err == ""
#    mo.assert_called()


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_onNode(capsys: pytest.CaptureFixture[str]) -> None:
    """Test onNode."""
    onNode("foo")
    out, err = capsys.readouterr()
    assert re.search(r"Node changed", out, re.MULTILINE)
    assert err == ""


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_tunnel_no_args(capsys: pytest.CaptureFixture[str]) -> None:
    """Test tunnel no arguments."""
    sys.argv = [""]
    mt_config.args = sys.argv  # type: ignore[assignment]
    with pytest.raises(SystemExit) as pytest_wrapped_e:
        tunnelMain()
    assert pytest_wrapped_e.type is SystemExit
    assert pytest_wrapped_e.value.code == 1
    _, err = capsys.readouterr()
    assert re.search(r"usage: ", err, re.MULTILINE)


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
@patch("meshtastic.util.findPorts", return_value=[])
@patch("platform.system")
def test_tunnel_tunnel_arg_with_no_devices(mock_platform_system: Any, caplog: pytest.LogCaptureFixture, capsys: pytest.CaptureFixture[str]) -> None:
    """Test tunnel with tunnel arg (act like we are on a linux system)."""
    a_mock = MagicMock()
    a_mock.return_value = "Linux"
    mock_platform_system.side_effect = a_mock
    sys.argv = ["", "--tunnel"]
    mt_config.args = sys.argv  # type: ignore[assignment]
    print(f"platform.system():{platform.system()}")
    with caplog.at_level(logging.DEBUG):
        with pytest.raises(SystemExit) as pytest_wrapped_e:
            tunnelMain()
        mock_platform_system.assert_called()
        assert pytest_wrapped_e.type is SystemExit
        assert pytest_wrapped_e.value.code == 1
        _out, err = capsys.readouterr()
        assert re.search(r"Error connecting to localhost", err, re.MULTILINE)


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
@patch("meshtastic.util.findPorts", return_value=[])
@patch("platform.system")
def test_tunnel_subnet_arg_with_no_devices(mock_platform_system: Any, caplog: pytest.LogCaptureFixture, capsys: pytest.CaptureFixture[str]) -> None:
    """Test tunnel with subnet arg (act like we are on a linux system)."""
    a_mock = MagicMock()
    a_mock.return_value = "Linux"
    mock_platform_system.side_effect = a_mock
    sys.argv = ["", "--subnet", "foo"]
    mt_config.args = sys.argv  # type: ignore[assignment]
    print(f"platform.system():{platform.system()}")
    with caplog.at_level(logging.DEBUG):
        with pytest.raises(SystemExit) as pytest_wrapped_e:
            tunnelMain()
        mock_platform_system.assert_called()
        assert pytest_wrapped_e.type is SystemExit
        assert pytest_wrapped_e.value.code == 1
        _out, err = capsys.readouterr()
        assert re.search(r"Error connecting to localhost", err, re.MULTILINE)


@pytest.mark.skipif(sys.platform == "win32", reason="on windows is no fcntl module")
@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
@patch("platform.system")
@patch("meshtastic.serial_interface.SerialInterface._set_hupcl_with_termios")
@patch("builtins.open", new_callable=mock_open, read_data="data")
@patch("serial.Serial")
@patch("meshtastic.util.findPorts", return_value=["/dev/ttyUSBfake"])
def test_tunnel_tunnel_arg(
    _mocked_findPorts: Any,
    _mocked_serial: Any,
    _mocked_open: Any,
    _mock_hupcl: Any,
    mock_platform_system: Any,
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Test tunnel with tunnel arg (act like we are on a linux system)."""

    # Override the time.sleep so there is no loop
    def my_sleep(amount: float) -> None:
        """Simulate a sleep in tests by printing the provided value and terminating the process.

        Prints `amount` to stdout and then exits the process with exit code 3.

        Parameters
        ----------
        amount : float
            The value (typically a sleep duration) to print before exiting.
        """
        print(f"{amount}")
        sys.exit(3)

    a_mock = MagicMock()
    a_mock.return_value = "Linux"
    mock_platform_system.side_effect = a_mock
    sys.argv = ["", "--tunnel"]
    mt_config.args = sys.argv  # type: ignore[assignment]

    with SerialInterface(noProto=True, connectNow=False) as serialInterface:
        with (
            caplog.at_level(logging.DEBUG),
            patch(
                "meshtastic.serial_interface.SerialInterface",
                return_value=serialInterface,
            ),
            patch("time.sleep", side_effect=my_sleep),
        ):
            with pytest.raises(SystemExit) as pytest_wrapped_e:
                tunnelMain()
            assert pytest_wrapped_e.type is SystemExit
            assert pytest_wrapped_e.value.code == 3
        mock_platform_system.assert_called()
        assert re.search(r"Not starting Tunnel", caplog.text, re.MULTILINE)
    out, err = capsys.readouterr()
    assert re.search(r"Connected to radio", out, re.MULTILINE)
    assert err == ""


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_set_favorite_node() -> None:
    """Test --set-favorite-node node."""
    sys.argv = ["", "--set-favorite-node", "!12345678"]
    mt_config.args = sys.argv  # type: ignore[assignment]
    mocked_node = MagicMock(autospec=Node)
    iface = MagicMock(autospec=SerialInterface)
    iface.__enter__ = MagicMock(return_value=iface)
    iface.__exit__ = MagicMock(return_value=None)
    iface.getNode.return_value = mocked_node
    with patch("meshtastic.serial_interface.SerialInterface", return_value=iface):
        main()

    mocked_node.setFavorite.assert_called_once_with("!12345678")


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_remove_favorite_node() -> None:
    """Test --remove-favorite-node node."""
    sys.argv = ["", "--remove-favorite-node", "!12345678"]
    mt_config.args = sys.argv  # type: ignore[assignment]
    mocked_node = MagicMock(autospec=Node)
    iface = MagicMock(autospec=SerialInterface)
    iface.__enter__ = MagicMock(return_value=iface)
    iface.__exit__ = MagicMock(return_value=None)
    iface.getNode.return_value = mocked_node
    mocked_node.iface = iface
    with patch("meshtastic.serial_interface.SerialInterface", return_value=iface):
        main()

    mocked_node.removeFavorite.assert_called_once_with("!12345678")


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_set_ignored_node() -> None:
    """Test --set-ignored-node node."""
    sys.argv = ["", "--set-ignored-node", "!12345678"]
    mt_config.args = sys.argv  # type: ignore[assignment]
    mocked_node = MagicMock(autospec=Node)
    iface = MagicMock(autospec=SerialInterface)
    iface.__enter__ = MagicMock(return_value=iface)
    iface.__exit__ = MagicMock(return_value=None)
    iface.getNode.return_value = mocked_node
    with patch("meshtastic.serial_interface.SerialInterface", return_value=iface):
        main()

    mocked_node.setIgnored.assert_called_once_with("!12345678")


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_remove_ignored_node() -> None:
    """Test --remove-ignored-node node."""
    sys.argv = ["", "--remove-ignored-node", "!12345678"]
    mt_config.args = sys.argv  # type: ignore[assignment]
    mocked_node = MagicMock(autospec=Node)
    iface = MagicMock(autospec=SerialInterface)
    iface.__enter__ = MagicMock(return_value=iface)
    iface.__exit__ = MagicMock(return_value=None)
    iface.getNode.return_value = mocked_node
    mocked_node.iface = iface
    with patch("meshtastic.serial_interface.SerialInterface", return_value=iface):
        main()

    mocked_node.removeIgnored.assert_called_once_with("!12345678")


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_main_set_owner_whitespace_only(capsys: pytest.CaptureFixture[str]) -> None:
    """Test --set-owner with whitespace-only name."""
    sys.argv = ["", "--set-owner", "   "]
    mt_config.args = sys.argv  # type: ignore[assignment]

    iface = MagicMock(autospec=SerialInterface)
    iface.__enter__ = MagicMock(return_value=iface)
    iface.__exit__ = MagicMock(return_value=None)
    with patch("meshtastic.serial_interface.SerialInterface", return_value=iface):
        with pytest.raises(SystemExit) as excinfo:
            main()

    _, err = capsys.readouterr()
    # Error messages go to stderr
    assert (
        "ERROR: Long Name cannot be empty or contain only whitespace characters" in err
    )
    assert excinfo.value.code == 1


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_main_set_owner_empty_string(capsys: pytest.CaptureFixture[str]) -> None:
    """Test --set-owner with empty string."""
    sys.argv = ["", "--set-owner", ""]
    mt_config.args = sys.argv  # type: ignore[assignment]

    iface = MagicMock(autospec=SerialInterface)
    iface.__enter__ = MagicMock(return_value=iface)
    iface.__exit__ = MagicMock(return_value=None)
    with patch("meshtastic.serial_interface.SerialInterface", return_value=iface):
        with pytest.raises(SystemExit) as excinfo:
            main()

    _, err = capsys.readouterr()
    # Error messages go to stderr
    assert (
        "ERROR: Long Name cannot be empty or contain only whitespace characters" in err
    )
    assert excinfo.value.code == 1


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_main_set_owner_short_whitespace_only(capsys: pytest.CaptureFixture[str]) -> None:
    """Test --set-owner-short with whitespace-only name."""
    sys.argv = ["", "--set-owner-short", "   "]
    mt_config.args = sys.argv  # type: ignore[assignment]

    iface = MagicMock(autospec=SerialInterface)
    iface.__enter__ = MagicMock(return_value=iface)
    iface.__exit__ = MagicMock(return_value=None)
    with patch("meshtastic.serial_interface.SerialInterface", return_value=iface):
        with pytest.raises(SystemExit) as excinfo:
            main()

    _, err = capsys.readouterr()
    # Error messages go to stderr
    assert (
        "ERROR: Short Name cannot be empty or contain only whitespace characters" in err
    )
    assert excinfo.value.code == 1


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_main_set_owner_short_empty_string(capsys: pytest.CaptureFixture[str]) -> None:
    """Test --set-owner-short with empty string."""
    sys.argv = ["", "--set-owner-short", ""]
    mt_config.args = sys.argv  # type: ignore[assignment]

    iface = MagicMock(autospec=SerialInterface)
    iface.__enter__ = MagicMock(return_value=iface)
    iface.__exit__ = MagicMock(return_value=None)
    with patch("meshtastic.serial_interface.SerialInterface", return_value=iface):
        with pytest.raises(SystemExit) as excinfo:
            main()

    _, err = capsys.readouterr()
    # Error messages go to stderr
    assert (
        "ERROR: Short Name cannot be empty or contain only whitespace characters" in err
    )
    assert excinfo.value.code == 1


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_main_set_ham_whitespace_only(capsys: pytest.CaptureFixture[str]) -> None:
    """Verify that invoking the CLI with --set-ham and a whitespace-only callsign prints an appropriate error and exits with code 1.

    Asserts the error message "ERROR: Ham radio callsign cannot be empty or contain only
    whitespace characters" appears on stderr and that the process exits with code 1.

    """
    sys.argv = ["", "--set-ham", "   "]
    mt_config.args = sys.argv  # type: ignore[assignment]

    iface = MagicMock(autospec=SerialInterface)
    iface.__enter__ = MagicMock(return_value=iface)
    iface.__exit__ = MagicMock(return_value=None)
    with patch("meshtastic.serial_interface.SerialInterface", return_value=iface):
        with pytest.raises(SystemExit) as excinfo:
            main()

    _, err = capsys.readouterr()
    # Error messages go to stderr
    assert (
        "ERROR: Ham radio callsign cannot be empty or contain only whitespace characters"
        in err
    )
    assert excinfo.value.code == 1


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_main_set_ham_empty_string(capsys: pytest.CaptureFixture[str]) -> None:
    """Test --set-ham with empty string."""
    sys.argv = ["", "--set-ham", ""]
    mt_config.args = sys.argv  # type: ignore[assignment]

    iface = MagicMock(autospec=SerialInterface)
    iface.__enter__ = MagicMock(return_value=iface)
    iface.__exit__ = MagicMock(return_value=None)
    with patch("meshtastic.serial_interface.SerialInterface", return_value=iface):
        with pytest.raises(SystemExit) as excinfo:
            main()

    _, err = capsys.readouterr()
    # Error messages go to stderr
    assert (
        "ERROR: Ham radio callsign cannot be empty or contain only whitespace characters"
        in err
    )
    assert excinfo.value.code == 1
