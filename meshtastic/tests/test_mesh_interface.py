"""Meshtastic unit tests for mesh_interface.py."""

# pylint: disable=too-many-lines

import builtins
import importlib.util
import io
import logging
import re
import threading
import time
import types
from collections import OrderedDict
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import MagicMock, create_autospec, patch

import pytest
from hypothesis import given
from hypothesis import strategies as st

import meshtastic.mesh_interface as mesh_interface_module

from .. import BROADCAST_ADDR, LOCAL_ADDR, NODELESS_WANT_CONFIG_ID, ResponseHandler
from ..mesh_interface import MeshInterface, _timeago
from ..node import Node
from ..protobuf import channel_pb2, config_pb2, mesh_pb2, portnums_pb2, telemetry_pb2

# TODO
# from ..config import Config
from ..util import Timeout

if TYPE_CHECKING:
    from .conftest import FakeTimer


@pytest.mark.unit
def test_mesh_interface_import_handles_missing_print_color(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Import should gracefully set print_color to None when dependency is unavailable."""
    module_path = Path(mesh_interface_module.__file__)
    spec = importlib.util.spec_from_file_location(
        "meshtastic.mesh_interface_import_fallback_test", module_path
    )
    assert spec is not None
    assert spec.loader is not None
    isolated_module = importlib.util.module_from_spec(spec)

    real_import = builtins.__import__

    def _import_with_print_color_failure(
        name: str,
        globals_dict: Any = None,
        locals_dict: Any = None,
        from_list: tuple[str, ...] = (),
        level: int = 0,
    ) -> Any:
        if name == "print_color":
            raise ImportError(  # noqa: TRY003 - intentional test sentinel
                "simulated missing print_color"
            ) from None
        return real_import(name, globals_dict, locals_dict, from_list, level)

    monkeypatch.setattr(builtins, "__import__", _import_with_print_color_failure)
    spec.loader.exec_module(isolated_module)
    assert isolated_module.print_color is None


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_MeshInterface(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Test that we can instantiate a MeshInterface."""
    # Optional dependencies used only by this test path.
    powermon_module = pytest.importorskip("meshtastic.powermon")
    slog_module = pytest.importorskip("meshtastic.slog")
    SimPowerSupply = powermon_module.SimPowerSupply
    LogSet = slog_module.LogSet

    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    with MeshInterface(noProto=True) as iface:
        NODE_ID = "!9388f81c"
        NODE_NUM = 2475227164
        node = {
            "num": NODE_NUM,
            "user": {
                "id": NODE_ID,
                "longName": "Unknown f81c",
                "shortName": "?1C",
                "macaddr": "RBeTiPgc",
                "hwModel": "TBEAM",
            },
            "position": {},
            "lastHeard": 1640204888,
        }

        iface.nodes = {NODE_ID: node}
        iface.nodesByNum = {NODE_NUM: node}

        myInfo = MagicMock()
        iface.myInfo = myInfo

        iface.localNode.localConfig.lora.CopyFrom(config_pb2.Config.LoRaConfig())

        # Also get some coverage of the structured logging/power meter stuff by turning it on as well
        log_set = LogSet(iface, None, SimPowerSupply())
        try:
            iface.showInfo()
            iface.localNode.showInfo()
            iface.showNodes()
            iface.sendText("hello")
        finally:
            log_set.close()
    out, err = capsys.readouterr()
    assert re.search(r"Owner: None \(None\)", out, re.MULTILINE)
    assert re.search(r"Nodes", out, re.MULTILINE)
    assert re.search(r"Preferences", out, re.MULTILINE)
    assert re.search(r"Channels", out, re.MULTILINE)
    assert re.search(r"Primary channel URL", out, re.MULTILINE)
    assert err == ""


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_showInfo_skips_nodes_without_user_dict() -> None:
    """ShowInfo should ignore node records whose user payload is not a dict."""
    with MeshInterface(noProto=True) as iface:
        iface.nodes = {
            "!bad": {"num": 1, "user": "invalid"},
            "!good": {"num": 2, "user": {"id": "!good"}},
        }
        output = io.StringIO()
        summary = iface.showInfo(file=output)

    assert '"!good"' in summary
    assert '"!bad"' not in summary


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_showInfo_tolerates_malformed_macaddr() -> None:
    """ShowInfo should not fail when a node user entry contains malformed macaddr."""
    with MeshInterface(noProto=True) as iface:
        iface.nodes = {
            "!good": {"num": 2, "user": {"id": "!good", "macaddr": "not-base64!!!"}},
        }
        output = io.StringIO()
        summary = iface.showInfo(file=output)

    assert '"!good"' in summary


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_getMyUser(iface_with_nodes: MeshInterface) -> None:
    """Test getMyUser()."""
    iface = iface_with_nodes
    assert iface.myInfo is not None
    iface.myInfo.my_node_num = 2475227164
    myuser = iface.getMyUser()
    assert myuser is not None
    assert myuser["id"] == "!9388f81c"


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_getLongName(iface_with_nodes: MeshInterface) -> None:
    """Test getLongName()."""
    iface = iface_with_nodes
    assert iface.myInfo is not None
    iface.myInfo.my_node_num = 2475227164
    mylongname = iface.getLongName()
    assert mylongname == "Unknown f81c"


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_getShortName(iface_with_nodes: MeshInterface) -> None:
    """Test getShortName()."""
    iface = iface_with_nodes
    assert iface.myInfo is not None
    iface.myInfo.my_node_num = 2475227164
    myshortname = iface.getShortName()
    assert myshortname == "?1C"


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_handlePacketFromRadio_no_from(caplog: pytest.LogCaptureFixture) -> None:
    """Test _handle_packet_from_radio with no 'from' in the mesh packet."""
    with MeshInterface(noProto=True) as iface:
        meshPacket = mesh_pb2.MeshPacket()
        with caplog.at_level(logging.ERROR):
            iface._handle_packet_from_radio(meshPacket)
    assert re.search(
        r"Device returned a packet we sent, ignoring", caplog.text, re.MULTILINE
    )


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_handlePacketFromRadio_with_a_portnum(caplog: pytest.LogCaptureFixture) -> None:
    """Test _handle_packet_from_radio with a portnum.

    Since we have an attribute called 'from', we cannot simply 'set' it.
    Had to implement a hack just to be able to test some code.

    """
    with MeshInterface(noProto=True) as iface:
        meshPacket = mesh_pb2.MeshPacket()
        meshPacket.decoded.payload = b""
        meshPacket.decoded.portnum = portnums_pb2.PortNum.TEXT_MESSAGE_APP
        with caplog.at_level(logging.WARNING):
            iface._handle_packet_from_radio(meshPacket, hack=True)
    assert re.search(r"Not populating fromId", caplog.text, re.MULTILINE)


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_handlePacketFromRadio_no_portnum(caplog: pytest.LogCaptureFixture) -> None:
    """Verify that _handle_packet_from_radio logs a warning about not populating fromId when a MeshPacket has no portnum."""
    with MeshInterface(noProto=True) as iface:
        meshPacket = mesh_pb2.MeshPacket()
        meshPacket.decoded.payload = b""
        with caplog.at_level(logging.WARNING):
            iface._handle_packet_from_radio(meshPacket, hack=True)
    assert re.search(r"Not populating fromId", caplog.text, re.MULTILINE)


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_getNode_with_local() -> None:
    """Test getNode."""
    with MeshInterface(noProto=True) as iface:
        anode = iface.getNode(LOCAL_ADDR)
        assert anode == iface.localNode


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_getNode_not_local(caplog: pytest.LogCaptureFixture) -> None:
    """Test getNode not local."""
    with MeshInterface(noProto=True) as iface:
        anode = create_autospec(Node, instance=True)
        anode.partialChannels = []
        with caplog.at_level(logging.DEBUG):
            with patch("meshtastic.node.Node", return_value=anode):
                another_node = iface.getNode("bar2")
                assert another_node != iface.localNode
    assert re.search(r"About to requestChannels", caplog.text, re.MULTILINE)


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
@pytest.mark.parametrize("request_channel_attempts", [None, 2])
def test_getNode_not_local_timeout(
    caplog: pytest.LogCaptureFixture,
    request_channel_attempts: int | None,
) -> None:
    """Test getNode timeout behavior with default and explicit request-channel attempts."""
    with MeshInterface(noProto=True) as iface:
        anode = create_autospec(Node, instance=True)
        anode.waitForConfig.return_value = False
        anode.partialChannels = []
        with caplog.at_level(logging.WARNING):
            with patch("meshtastic.node.Node", return_value=anode):
                with pytest.raises(
                    MeshInterface.MeshInterfaceError
                ) as pytest_wrapped_e:
                    if request_channel_attempts is None:
                        iface.getNode("bar2")
                    else:
                        iface.getNode(
                            "bar2",
                            requestChannelAttempts=request_channel_attempts,
                        )
                assert pytest_wrapped_e.type is MeshInterface.MeshInterfaceError
                assert "Timed out waiting for channels, giving up" in str(
                    pytest_wrapped_e.value
                )
                assert re.search(
                    r"Timed out trying to retrieve channel info, retrying",
                    caplog.text,
                    re.MULTILINE,
                )


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_sendPosition(caplog: pytest.LogCaptureFixture) -> None:
    """Verify that MeshInterface.sendPosition() executes without error and emits position-related debug logs.

    Creates a MeshInterface(noProto=True), calls sendPosition() while capturing DEBUG logs, and then closes the interface.

    """
    with MeshInterface(noProto=True) as iface:
        with caplog.at_level(logging.DEBUG):
            iface.sendPosition()
    # assert re.search(r"p.time:", caplog.text, re.MULTILINE)


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_heartbeat_timer_is_daemon_and_cancelled_on_close(
    monkeypatch: pytest.MonkeyPatch,
    fake_timer_cls: type["FakeTimer"],
) -> None:
    """Heartbeat timer should be daemonized and cancelled during close()."""

    with MeshInterface(noProto=True) as iface:
        monkeypatch.setattr(iface, "sendHeartbeat", lambda: None)

        iface._start_heartbeat()
        assert len(fake_timer_cls.created) == 1
        timer = fake_timer_cls.created[0]
        assert timer.daemon is True
        assert timer.started is True

    assert timer.cancelled is True


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_heartbeat_callback_does_not_reschedule_after_close(
    monkeypatch: pytest.MonkeyPatch,
    fake_timer_cls: type["FakeTimer"],
) -> None:
    """A heartbeat callback firing after close() must not create a new timer."""

    with MeshInterface(noProto=True) as iface:
        monkeypatch.setattr(iface, "sendHeartbeat", lambda: None)

        iface._start_heartbeat()
        assert len(fake_timer_cls.created) == 1
        old_timer = fake_timer_cls.created[0]

        iface.close()
        old_timer.function()

        assert len(fake_timer_cls.created) == 1


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_close_waits_for_inflight_heartbeat_send(
    monkeypatch: pytest.MonkeyPatch,
    fake_timer_cls: type["FakeTimer"],
) -> None:
    """close() should wait for an in-flight heartbeat send to finish."""

    with MeshInterface(noProto=True) as iface:
        send_started = threading.Event()
        release_send = threading.Event()
        close_done = threading.Event()
        close_started = threading.Event()

        def blocking_send_heartbeat() -> None:
            send_started.set()
            release_send.wait(timeout=2.0)

        def close_interface() -> None:
            close_started.set()
            iface.close()
            close_done.set()

        monkeypatch.setattr(iface, "sendHeartbeat", blocking_send_heartbeat)

        start_thread = threading.Thread(target=iface._start_heartbeat, daemon=True)
        start_thread.start()
        assert send_started.wait(timeout=1.0)
        assert len(fake_timer_cls.created) == 1

        close_thread = threading.Thread(target=close_interface, daemon=True)
        close_thread.start()
        assert close_started.wait(timeout=1.0)
        # close() should block until the in-flight heartbeat send completes.
        # Use a generous timeout (0.2s) to avoid flakiness on slow CI runners.
        assert not close_done.wait(timeout=0.2)

        release_send.set()
        close_thread.join(timeout=1.0)
        start_thread.join(timeout=1.0)

        assert close_done.is_set()
        assert not close_thread.is_alive()
        assert not start_thread.is_alive()


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
@pytest.mark.parametrize(
    "disconnect_error",
    [
        OSError("bad fd"),
        MeshInterface.MeshInterfaceError("ble write failed"),
    ],
)
def test_close_suppresses_disconnect_send_failures(
    caplog: pytest.LogCaptureFixture,
    disconnect_error: BaseException,
) -> None:
    """close() should continue cleanup if sending disconnect fails."""
    iface = MeshInterface(noProto=True)
    try:
        iface.debugOut = io.StringIO()
        with (
            patch.object(iface, "_send_disconnect", side_effect=disconnect_error),
            caplog.at_level(logging.DEBUG),
        ):
            iface.close()
        assert iface._closing is True
        assert iface.debugOut is None
    finally:
        if not getattr(iface, "_closing", False):
            iface.close()

    assert (
        "Failed to send disconnect during close(); continuing shutdown." in caplog.text
    )


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_connected_noop_when_closing() -> None:
    """_connected() should not set connection state while shutdown is in progress."""
    with MeshInterface(noProto=True) as iface:
        iface._closing = True

        iface._connected()

        assert iface.isConnected.is_set() is False


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_connected_publishes_established_once_per_connected_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """_connected() should publish established once per connect transition."""
    queued_callbacks: list[Any] = []

    def _queue_work(callback: Any) -> None:
        queued_callbacks.append(callback)

    monkeypatch.setattr(
        "meshtastic.mesh_interface.publishingThread.queueWork", _queue_work
    )

    with MeshInterface(noProto=True) as iface:
        monkeypatch.setattr(iface, "_start_heartbeat", lambda: None)

        iface._connected()
        iface._connected()
        assert len(queued_callbacks) == 1

        # Simulate a new session transition and verify publish happens again.
        iface.isConnected.clear()
        iface._connected()
        assert len(queued_callbacks) == 2


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_disconnected_publishes_lost_once_per_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """_disconnected() should publish at most once for each connected session."""
    queued_callbacks: list[Any] = []

    def _queue_work(callback: Any) -> None:
        queued_callbacks.append(callback)

    monkeypatch.setattr(
        "meshtastic.mesh_interface.publishingThread.queueWork", _queue_work
    )

    with MeshInterface(noProto=True) as iface:
        iface.isConnected.set()
        iface._disconnected()
        assert len(queued_callbacks) == 1

        # Idempotent while already disconnected.
        iface._disconnected()
        assert len(queued_callbacks) == 1

        # A new connection should allow one new lost notification.
        iface.isConnected.set()
        iface._disconnected()
        assert len(queued_callbacks) == 2


# TODO
# @pytest.mark.unit
# @pytest.mark.usefixtures("reset_mt_config")
# def test_close_with_heartbeatTimer(caplog):
#    """Test close() with heartbeatTimer"""
#    iface = MeshInterface(noProto=True)
#    anode = Node('foo', 'bar')
#    aconfig = Config()
#    aonfig.preferences.phone_timeout_secs = 10
#    anode.config = aconfig
#    iface.localNode = anode
#    assert iface.heartbeatTimer is None
#    with caplog.at_level(logging.DEBUG):
#        iface._start_heartbeat()
#        assert iface.heartbeatTimer is not None
#        iface.close()


# TODO
# @pytest.mark.unit
# @pytest.mark.usefixtures("reset_mt_config")
# def test_handleFromRadio_empty_payload(caplog):
#    """Test _handle_from_radio"""
#    iface = MeshInterface(noProto=True)
#    with caplog.at_level(logging.DEBUG):
#        iface._handle_from_radio(b'')
#    iface.close()
#    assert re.search(r'Unexpected FromRadio payload', caplog.text, re.MULTILINE)


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_handleFromRadio_with_my_info(caplog: pytest.LogCaptureFixture) -> None:
    """Test _handle_from_radio with my_info."""
    # Note: I captured the '--debug --info' for the bytes below.
    # It "translates" to this:
    # my_info {
    #  my_node_num: 682584012
    #  firmware_version: "1.2.49.5354c49"
    #  reboot_count: 13
    #  bitrate: 17.088470458984375
    #  message_timeout_msec: 300000
    #  min_app_version: 20200
    #  max_channels: 8
    #  has_wifi: true
    # }
    from_radio_bytes = b"\x1a,\x08\xcc\xcf\xbd\xc5\x02\x18\r2\x0e1.2.49.5354c49P\r]0\xb5\x88Ah\xe0\xa7\x12p\xe8\x9d\x01x\x08\x90\x01\x01"
    with MeshInterface(noProto=True) as iface:
        with caplog.at_level(logging.DEBUG):
            iface._handle_from_radio(from_radio_bytes)
    assert re.search(r"Received from radio: my_info {", caplog.text, re.MULTILINE)
    assert re.search(r"my_node_num: 682584012", caplog.text, re.MULTILINE)


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_handleFromRadio_with_node_info(
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Test _handle_from_radio with node_info."""
    # Note: I captured the '--debug --info' for the bytes below.
    # It "translates" to this:
    # node_info {
    #  num: 682584012
    #  user {
    #    id: "!28af67cc"
    #    long_name: "Unknown 67cc"
    #    short_name: "?CC"
    #    macaddr: "$o(\257g\314"
    #    hw_model: HELTEC_V2_1
    #  }
    #  position {
    #    }
    #  }

    from_radio_bytes = b'"2\x08\xcc\xcf\xbd\xc5\x02\x12(\n\t!28af67cc\x12\x0cUnknown 67cc\x1a\x03?CC"\x06$o(\xafg\xcc0\n\x1a\x00'
    with MeshInterface(noProto=True) as iface:
        with caplog.at_level(logging.DEBUG):
            iface._start_config()
            iface._handle_from_radio(from_radio_bytes)
            assert re.search(
                r"Received from radio: node_info {", caplog.text, re.MULTILINE
            )
            assert re.search(r"682584012", caplog.text, re.MULTILINE)
            # validate some of showNodes() output
            iface.showNodes()
            out, err = capsys.readouterr()
            assert re.search(r" 1 ", out, re.MULTILINE)
            assert re.search(r"│ Unknown 67cc │ ", out, re.MULTILINE)
            assert re.search(r"│\s+!28af67cc\s+│\s+\?CC\s+│", out, re.MULTILINE)
            assert err == ""


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_handleFromRadio_with_node_info_tbeam1(
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Test _handle_from_radio with node_info."""
    # Note: Captured the '--debug --info' for the bytes below.
    # pylint: disable=C0301
    from_radio_bytes = (
        b'"=\x08\x80\xf8\xc8\xf6\x07\x12"\n\t!7ed23c00\x12\x07TBeam 1\x1a\x02T1"'
        b"\x06\x94\xb9~\xd2<\x000\x04\x1a\x07 ]MN\x01\xbea%\xad\x01\xbea=\x00\x00,A"
    )
    with MeshInterface(noProto=True) as iface:
        with caplog.at_level(logging.DEBUG):
            iface._start_config()
            iface._handle_from_radio(from_radio_bytes)
            assert re.search(r"Received nodeinfo", caplog.text, re.MULTILINE)
            assert re.search(r"TBeam 1", caplog.text, re.MULTILINE)
            assert re.search(r"2127707136", caplog.text, re.MULTILINE)
            # validate some of showNodes() output
            iface.showNodes()
            out, err = capsys.readouterr()
            assert re.search(r" 1 ", out, re.MULTILINE)
            assert re.search(r"│ TBeam 1 │ ", out, re.MULTILINE)
            assert re.search(r"│ !7ed23c00 │", out, re.MULTILINE)
            assert err == ""


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_handleFromRadio_with_node_info_tbeam_with_bad_data(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Test _handle_from_radio with node_info with some bad data (issue#172) - ensure we do not throw exception."""
    # Note: Captured the '--debug --info' for the bytes below.
    from_radio_bytes = b'"\x17\x08\xdc\x8a\x8a\xae\x02\x12\x08"\x06\x00\x00\x00\x00\x00\x00\x1a\x00=\x00\x00\xb8@'
    with MeshInterface(noProto=True) as iface:
        with caplog.at_level(logging.DEBUG):
            iface._start_config()
            iface._handle_from_radio(from_radio_bytes)


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_MeshInterface_sendToRadio_no_proto(caplog: pytest.LogCaptureFixture) -> None:
    """Verify the default MeshInterface._send_to_radio_impl logs that subclasses must implement radio sending.

    Asserts that invoking the base implementation produces a log message containing "Subclass must provide toradio".

    """
    with MeshInterface(noProto=True) as iface:
        with caplog.at_level(logging.DEBUG):
            iface._send_to_radio_impl(mesh_pb2.ToRadio())
    assert re.search(r"Subclass must provide toradio", caplog.text, re.MULTILINE)


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_sendData_too_long(caplog: pytest.LogCaptureFixture) -> None:
    """Test when data payload is too big."""
    with MeshInterface(noProto=True) as iface:
        some_large_text = (
            b"This is a long text that will be too long for send text." * 12
        )
        with caplog.at_level(logging.DEBUG):
            with pytest.raises(MeshInterface.MeshInterfaceError) as pytest_wrapped_e:
                iface.sendData(some_large_text)
            assert pytest_wrapped_e.type is MeshInterface.MeshInterfaceError
            assert "Data payload too big" in str(pytest_wrapped_e.value)


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_sendData_unknown_app() -> None:
    """Verify that calling sendData with PortNum.UNKNOWN_APP raises MeshInterface.MeshInterfaceError.

    and the error message contains "A non-zero port number must be specified".
    """
    with MeshInterface(noProto=True) as iface:
        with pytest.raises(MeshInterface.MeshInterfaceError) as pytest_wrapped_e:
            iface.sendData(b"hello", portNum=portnums_pb2.PortNum.UNKNOWN_APP)
    assert pytest_wrapped_e.type is MeshInterface.MeshInterfaceError
    assert "A non-zero port number must be specified" in str(pytest_wrapped_e.value)


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_sendPosition_with_a_position(caplog: pytest.LogCaptureFixture) -> None:
    """Test sendPosition when lat/long/alt."""
    with MeshInterface(noProto=True) as iface:
        with caplog.at_level(logging.DEBUG):
            iface.sendPosition(latitude=40.8, longitude=-111.86, altitude=201)
            assert re.search(r"p.latitude_i:408", caplog.text, re.MULTILINE)
            assert re.search(r"p.longitude_i:-11186", caplog.text, re.MULTILINE)
            assert re.search(r"p.altitude:201", caplog.text, re.MULTILINE)


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_sendPacket_with_no_destination() -> None:
    """Test _send_packet() raises MeshInterfaceError when destinationId is None."""
    with MeshInterface(noProto=True) as iface:
        with pytest.raises(
            MeshInterface.MeshInterfaceError,
            match="destinationId must not be None",
        ):
            mesh_packet = mesh_pb2.MeshPacket()
            iface._send_packet(mesh_packet, destinationId=None)  # type: ignore[arg-type]


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_sendPacket_with_destination_as_int(caplog: pytest.LogCaptureFixture) -> None:
    """Test _send_packet() with int as a destination."""
    with MeshInterface(noProto=True) as iface:
        with caplog.at_level(logging.DEBUG):
            meshPacket = mesh_pb2.MeshPacket()
            iface._send_packet(meshPacket, destinationId=123)
            assert re.search(r"Not sending packet", caplog.text, re.MULTILINE)


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_sendPacket_alias_with_destination_as_int(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Test _sendPacket() compatibility alias delegates to _send_packet()."""
    with MeshInterface(noProto=True) as iface:
        with caplog.at_level(logging.DEBUG):
            meshPacket = mesh_pb2.MeshPacket()
            iface._sendPacket(meshPacket, destinationId=123)
            assert re.search(r"Not sending packet", caplog.text, re.MULTILINE)


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_sendPacket_with_destination_starting_with_a_bang(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Verify that _send_packet ignores destination IDs that begin with '!' and logs the action.

    Asserts that calling _send_packet with a destinationId starting with "!" results in a log entry containing "Not sending packet".

    """
    with MeshInterface(noProto=True) as iface:
        with caplog.at_level(logging.DEBUG):
            meshPacket = mesh_pb2.MeshPacket()
            iface._send_packet(meshPacket, destinationId="!1234")
            assert re.search(r"Not sending packet", caplog.text, re.MULTILINE)


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_sendPacket_with_destination_as_BROADCAST_ADDR(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Test _send_packet() with BROADCAST_ADDR as a destination."""
    with MeshInterface(noProto=True) as iface:
        with caplog.at_level(logging.DEBUG):
            meshPacket = mesh_pb2.MeshPacket()
            iface._send_packet(meshPacket, destinationId=BROADCAST_ADDR)
            assert re.search(r"Not sending packet", caplog.text, re.MULTILINE)


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_sendPacket_with_destination_as_LOCAL_ADDR_no_myInfo() -> None:
    """Test _send_packet() with LOCAL_ADDR raises MeshInterfaceError when myInfo is missing."""
    with MeshInterface(noProto=True) as iface:
        with pytest.raises(
            MeshInterface.MeshInterfaceError,
            match="No myInfo found",
        ):
            meshPacket = mesh_pb2.MeshPacket()
            iface._send_packet(meshPacket, destinationId=LOCAL_ADDR)


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_sendPacket_with_destination_as_LOCAL_ADDR_with_myInfo(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Test _send_packet() with LOCAL_ADDR as a destination with myInfo."""
    with MeshInterface(noProto=True) as iface:
        myInfo = MagicMock()
        iface.myInfo = myInfo
        iface.myInfo.my_node_num = 1
        with caplog.at_level(logging.DEBUG):
            meshPacket = mesh_pb2.MeshPacket()
            iface._send_packet(meshPacket, destinationId=LOCAL_ADDR)
            assert re.search(r"Not sending packet", caplog.text, re.MULTILINE)


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_sendPacket_with_destination_is_blank_with_nodes(
    iface_with_nodes: MeshInterface,
) -> None:
    """Test _send_packet() with '' as a destination raises MeshInterfaceError when node not found."""
    iface = iface_with_nodes
    meshPacket = mesh_pb2.MeshPacket()
    with pytest.raises(
        MeshInterface.MeshInterfaceError,
        match=r"NodeId  not found in DB",
    ):
        iface._send_packet(meshPacket, destinationId="")


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_sendPacket_with_destination_is_blank_without_nodes(
    caplog: pytest.LogCaptureFixture,
    iface_with_nodes: MeshInterface,
) -> None:
    """Test _send_packet() with '' as a destination with myInfo."""
    iface = iface_with_nodes
    iface.nodes = None
    meshPacket = mesh_pb2.MeshPacket()
    with caplog.at_level(logging.WARNING):
        iface._send_packet(meshPacket, destinationId="")
    assert re.search(r"Warning: There were no self.nodes.", caplog.text, re.MULTILINE)


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_sendPacket_raises_when_node_record_lacks_numeric_num(
    iface_with_nodes: MeshInterface,
) -> None:
    """_send_packet should reject DB node records that do not provide an integer num."""
    iface = iface_with_nodes
    iface.nodes = {"bad": {"user": {"id": "bad"}}}
    mesh_packet = mesh_pb2.MeshPacket()
    with pytest.raises(
        MeshInterface.MeshInterfaceError,
        match=r"NodeId bad has no numeric 'num' in DB",
    ):
        iface._send_packet(mesh_packet, destinationId="bad")


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_sendPacket_uses_numeric_num_from_node_record(
    iface_with_nodes: MeshInterface,
) -> None:
    """_send_packet should use node['num'] when destination resolves via DB lookup."""
    iface = iface_with_nodes
    iface.nodes = {"dst": {"num": 4242}}
    mesh_packet = mesh_pb2.MeshPacket()

    sent = iface._send_packet(mesh_packet, destinationId="dst")

    assert sent.to == 4242


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_sendPacket_applies_explicit_hoplimit_and_pki_encrypted_flag() -> None:
    """_send_packet should honor explicit hopLimit and pkiEncrypted parameters."""
    with MeshInterface(noProto=True) as iface:
        mesh_packet = mesh_pb2.MeshPacket()
        sent = iface._send_packet(
            mesh_packet,
            destinationId=123,
            hopLimit=5,
            pkiEncrypted=True,
        )
    assert sent.hop_limit == 5
    assert sent.pki_encrypted is True


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_sendPacket_sets_public_key_when_provided() -> None:
    """_send_packet should populate meshPacket.public_key when provided."""
    with MeshInterface(noProto=True) as iface:
        mesh_packet = mesh_pb2.MeshPacket()
        sent = iface._send_packet(
            mesh_packet,
            destinationId=123,
            publicKey=b"\xaa\xbb",
        )

    assert sent.public_key == b"\xaa\xbb"


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_getMyNodeInfo() -> None:
    """Test getMyNodeInfo()."""
    with MeshInterface(noProto=True) as iface:
        anode = iface.getNode(LOCAL_ADDR)
        iface.nodesByNum = {1: anode}  # type: ignore[dict-item]
        assert iface.nodesByNum.get(1) == anode  # type: ignore[comparison-overlap]
        myInfo = MagicMock()
        iface.myInfo = myInfo
        iface.myInfo.my_node_num = 1
        myinfo = iface.getMyNodeInfo()
    assert myinfo == anode  # type: ignore[comparison-overlap]


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_getCannedMessage() -> None:
    """Test MeshInterface.getCannedMessage()."""
    with MeshInterface(noProto=True) as iface:
        node = MagicMock()
        node.get_canned_message.return_value = "Hi|Bye|Yes"
        iface.localNode = node
        result = iface.getCannedMessage()
    assert result == "Hi|Bye|Yes"


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_getRingtone() -> None:
    """Ensure MeshInterface.getRingtone delegates to the local node and returns its ringtone string.

    The local node's get_ringtone() return value is forwarded unchanged.
    """
    with MeshInterface(noProto=True) as iface:
        node = MagicMock()
        node.get_ringtone.return_value = "foo,bar"
        iface.localNode = node
        result = iface.getRingtone()
    assert result == "foo,bar"


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_generatePacketId() -> None:
    """Test packet-id generation helpers when no currentPacketId (not connected)."""
    with MeshInterface(noProto=True) as iface:
        # not sure when this condition would ever happen... but we can simulate it
        iface.currentPacketId = None  # type: ignore[assignment]
        assert iface.currentPacketId is None
        with pytest.raises(MeshInterface.MeshInterfaceError) as excinfo:
            iface._generate_packet_id()
        with pytest.raises(MeshInterface.MeshInterfaceError) as excinfo_alias:
            iface._generatePacketId()
    assert "Not connected yet, can not generate packet" in str(excinfo.value)
    assert "Not connected yet, can not generate packet" in str(excinfo_alias.value)


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_sendPacket_alias_with_no_destination() -> None:
    """Test _sendPacket() alias raises MeshInterfaceError when destinationId is None."""
    with MeshInterface(noProto=True) as iface:
        with pytest.raises(
            MeshInterface.MeshInterfaceError,
            match="destinationId must not be None",
        ):
            mesh_packet = mesh_pb2.MeshPacket()
            iface._sendPacket(mesh_packet, destinationId=None)  # type: ignore[arg-type]


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_fixupPosition_empty_pos() -> None:
    """Test _fixup_position()."""
    with MeshInterface(noProto=True) as iface:
        pos: dict[str, Any] = {}
        newpos = iface._fixup_position(pos)
    assert newpos == pos


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_fixupPosition_no_changes_needed() -> None:
    """Test _fixup_position()."""
    with MeshInterface(noProto=True) as iface:
        pos = {"latitude": 101, "longitude": 102}
        newpos = iface._fixup_position(pos)
    assert newpos == pos


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_fixupPosition() -> None:
    """Test _fixup_position()."""
    with MeshInterface(noProto=True) as iface:
        pos = {"latitudeI": 1010000000, "longitudeI": 1020000000}
        newpos = iface._fixup_position(pos)
    assert newpos == {
        "latitude": 101.0,
        "latitudeI": 1010000000,
        "longitude": 102.0,
        "longitudeI": 1020000000,
    }


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_nodeNumToId(iface_with_nodes: MeshInterface) -> None:
    """Test _node_num_to_id()."""
    iface = iface_with_nodes
    assert iface.myInfo is not None
    iface.myInfo.my_node_num = 2475227164
    someid = iface._node_num_to_id(2475227164)
    assert someid == "!9388f81c"


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_nodeNumToId_not_found(iface_with_nodes: MeshInterface) -> None:
    """Test _node_num_to_id()."""
    iface = iface_with_nodes
    assert iface.myInfo is not None
    iface.myInfo.my_node_num = 2475227164
    someid = iface._node_num_to_id(123)
    assert someid is None


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_nodeNumToId_to_all(iface_with_nodes: MeshInterface) -> None:
    """Test _node_num_to_id()."""
    iface = iface_with_nodes
    assert iface.myInfo is not None
    iface.myInfo.my_node_num = 2475227164
    someid = iface._node_num_to_id(0xFFFFFFFF)
    assert someid == "^all"


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_getOrCreateByNum_minimal(iface_with_nodes: MeshInterface) -> None:
    """Test _get_or_create_by_num()."""
    iface = iface_with_nodes
    assert iface.myInfo is not None
    iface.myInfo.my_node_num = 2475227164
    tmp = iface._get_or_create_by_num(123)
    assert tmp == {
        "num": 123,
        "user": {
            "hwModel": "UNSET",
            "id": "!0000007b",
            "shortName": "007b",
            "longName": "Meshtastic 007b",
        },
    }


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_getOrCreateByNum_not_found(iface_with_nodes: MeshInterface) -> None:
    """Test _get_or_create_by_num()."""
    iface = iface_with_nodes
    assert iface.myInfo is not None
    iface.myInfo.my_node_num = 2475227164
    with pytest.raises(MeshInterface.MeshInterfaceError) as pytest_wrapped_e:
        iface._get_or_create_by_num(0xFFFFFFFF)
    assert pytest_wrapped_e.type is MeshInterface.MeshInterfaceError


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_getOrCreateByNum(iface_with_nodes: MeshInterface) -> None:
    """Test _get_or_create_by_num()."""
    iface = iface_with_nodes
    assert iface.myInfo is not None
    iface.myInfo.my_node_num = 2475227164
    tmp = iface._get_or_create_by_num(2475227164)
    assert tmp["num"] == 2475227164


# TODO
# @pytest.mark.unit
# def test_enter():
#    """Test __enter__()"""
#    iface = MeshInterface(noProto=True)
#    assert iface == iface.__enter__()


@pytest.mark.unit
def test_exit_with_exception(caplog: pytest.LogCaptureFixture) -> None:
    """Verify that MeshInterface.__exit__ logs the exception type, value, and traceback when an exception is raised inside its context.

    This test intentionally raises a ValueError inside the with-block to verify that
    MeshInterface.__exit__ properly logs exception details.

    Asserts an ERROR-level log entry contains the ValueError message and a traceback that includes the line where the exception was raised.

    Raises
    ------
    ValueError
        Intentionally raised inside the context body to verify __exit__ exception logging.
    """
    with caplog.at_level(logging.ERROR):
        with pytest.raises(ValueError):
            with MeshInterface(noProto=True):
                raise ValueError("Something went wrong")
    assert re.search(
        r"An exception of type <class \'ValueError\'> with value Something went wrong has occurred",
        caplog.text,
        re.MULTILINE,
    )
    assert "Traceback:" in caplog.text
    assert "in test_exit_with_exception" in caplog.text
    assert 'raise ValueError("Something went wrong")' in caplog.text


@pytest.mark.unit
def test_showNodes_exclude_self(
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
    iface_with_nodes: MeshInterface,
) -> None:
    """showNodes(includeSelf=False) should omit the local node from output."""
    with caplog.at_level(logging.DEBUG):
        iface = iface_with_nodes
        iface.localNode.nodeNum = 2475227164
        iface.showNodes()
        out_with_self, _ = capsys.readouterr()
        iface.showNodes(includeSelf=False)
        out_without_self, _ = capsys.readouterr()
        assert "!9388f81c" in out_with_self
        assert "!9388f81c" not in out_without_self


@pytest.mark.unitslow
def test_waitForConfig() -> None:
    """Verify that waitForConfig raises MeshInterface.MeshInterfaceError when the interface times out waiting for configuration."""
    with MeshInterface(noProto=True) as iface:
        # override how long to wait
        iface._timeout = Timeout(1)
        with pytest.raises(MeshInterface.MeshInterfaceError) as pytest_wrapped_e:
            iface.waitForConfig()
    assert pytest_wrapped_e.type is MeshInterface.MeshInterfaceError
    assert "Timed out waiting for interface config" in str(pytest_wrapped_e.value)


@pytest.mark.unit
def test_waitConnected_raises_an_exception() -> None:
    """Test waitConnected()."""
    with MeshInterface(noProto=True) as iface:
        iface.failure = MeshInterface.MeshInterfaceError("warn about something")
        with pytest.raises(MeshInterface.MeshInterfaceError) as excinfo:
            iface._wait_connected(0.01)
    assert "warn about something" in str(excinfo.value)


@pytest.mark.unit
def test_waitConnected_isConnected_timeout() -> None:
    """Verifies that _wait_connected raises a MeshInterfaceError when the connection does not complete within the specified timeout.

    Asserts the raised error message contains "Timed out waiting for connection completion".
    """
    with pytest.raises(MeshInterface.MeshInterfaceError) as excinfo:
        with MeshInterface(noProto=True) as iface:
            iface.noProto = False
            iface._wait_connected(0.01)
    assert "Timed out waiting for connection completion" in str(excinfo.value)


@pytest.mark.unit
def test_timeago() -> None:
    """Test that the _timeago function returns sane values."""
    assert _timeago(0) == "now"
    assert _timeago(1) == "1 sec ago"
    assert _timeago(15) == "15 secs ago"
    assert _timeago(333) == "5 mins ago"
    assert _timeago(99999) == "1 day ago"
    assert _timeago(9999999) == "3 months ago"
    assert _timeago(-999) == "now"


@pytest.mark.unit
@given(seconds=st.integers())
def test_timeago_fuzz(seconds: int) -> None:
    """Fuzz _timeago to ensure it works with any integer."""
    val = _timeago(seconds)
    assert re.fullmatch(r"now|\d+ (secs?|mins?|hours?|days?|months?|years?) ago", val)


# Concurrent access edge case tests
@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_concurrent_packet_id_generation() -> None:
    """Test that packet ID generation is thread-safe."""
    with MeshInterface(noProto=True) as iface:
        packet_ids = []
        errors = []
        packet_ids_lock = threading.Lock()
        errors_lock = threading.Lock()

        def generate_packet_ids() -> None:
            try:
                for _ in range(100):
                    packet_id = iface._generate_packet_id()
                    with packet_ids_lock:
                        packet_ids.append(packet_id)
            except Exception as e:  # noqa: BLE001
                with errors_lock:
                    errors.append(e)

        threads = [threading.Thread(target=generate_packet_ids) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(errors) == 0
        # All packet IDs should be unique
        assert len(packet_ids) == len(set(packet_ids))
        # All packet IDs should be within valid range
        assert all(0 <= pid <= 0xFFFFFFFF for pid in packet_ids)


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_concurrent_node_database_access() -> None:
    """Test that node database access is thread-safe."""
    with MeshInterface(noProto=True) as iface:
        iface.nodes = {}
        iface.nodesByNum = {}
        errors = []
        errors_lock = threading.Lock()

        def update_nodes(node_num: int) -> None:
            try:
                for i in range(50):
                    node_id = f"!{node_num:08x}"
                    node = iface._get_or_create_by_num(node_num)
                    with iface._node_db_lock:
                        node["lastHeard"] = i
                        if iface.nodes is not None:
                            iface.nodes[node_id] = node
                        if iface.nodesByNum is not None:
                            iface.nodesByNum[node_num] = node
            except Exception as e:  # noqa: BLE001
                with errors_lock:
                    errors.append(e)

        threads = [threading.Thread(target=update_nodes, args=(i,)) for i in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(errors) == 0


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_concurrent_queue_operations() -> None:
    """Test that queue operations are thread-safe."""
    with MeshInterface(noProto=True) as iface:
        iface.queue = OrderedDict()
        errors = []
        errors_lock = threading.Lock()

        def add_to_queue(start_id: int) -> None:
            try:
                for i in range(50):
                    packet_id = start_id * 100 + i
                    packet = mesh_pb2.ToRadio()
                    with iface._queue_lock:
                        iface.queue[packet_id] = packet
            except Exception as e:  # noqa: BLE001
                with errors_lock:
                    errors.append(e)

        def remove_from_queue() -> None:
            try:
                for _ in range(25):
                    with iface._queue_lock:
                        if iface.queue:
                            key = next(iter(iface.queue))
                            iface.queue.pop(key, None)
            except Exception as e:  # noqa: BLE001
                with errors_lock:
                    errors.append(e)

        add_threads = [
            threading.Thread(target=add_to_queue, args=(i,)) for i in range(4)
        ]
        remove_threads = [threading.Thread(target=remove_from_queue) for _ in range(4)]

        for t in add_threads + remove_threads:
            t.start()
        for t in add_threads + remove_threads:
            t.join()

        assert len(errors) == 0


@pytest.mark.unit
def test_concurrent_response_handler_registration() -> None:
    """Test that response handler registration is thread-safe."""
    with MeshInterface(noProto=True) as iface:
        iface.responseHandlers = {}
        errors = []
        added_ids = []
        added_ids_lock = threading.Lock()
        errors_lock = threading.Lock()

        def register_handlers(start_id: int) -> None:
            try:
                for i in range(50):
                    request_id = start_id * 100 + i
                    handler = MagicMock()
                    iface._add_response_handler(request_id, handler)
                    with added_ids_lock:
                        added_ids.append(request_id)
            except Exception as e:  # noqa: BLE001
                with errors_lock:
                    errors.append(e)

        threads = [
            threading.Thread(target=register_handlers, args=(i,)) for i in range(10)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(errors) == 0
        # All registered IDs should be in responseHandlers
        for request_id in added_ids:
            assert request_id in iface.responseHandlers


@pytest.mark.unit
def test_concurrent_close_with_packet_id_generation() -> None:
    """Test that close() properly handles concurrent packet ID generation."""
    errors = []
    stop_flag = threading.Event()
    started = threading.Event()
    errors_lock = threading.Lock()

    with MeshInterface(noProto=True) as iface:

        def generate_ids() -> None:
            try:
                while not stop_flag.is_set():
                    iface._generate_packet_id()
                    started.set()
            except Exception as e:  # noqa: BLE001
                with errors_lock:
                    errors.append(e)

        threads = [threading.Thread(target=generate_ids) for _ in range(5)]
        for t in threads:
            t.start()

        assert started.wait(timeout=1.0)
        # Exercise close() while packet-id generation is active.
        iface.close()

        # Signal threads to stop
        stop_flag.set()
        for t in threads:
            t.join(timeout=1.0)
        assert all(not t.is_alive() for t in threads)

    # Close is implicit in context manager exit
    assert len(errors) == 0


@pytest.mark.unit
def test_concurrent_showNodes() -> None:
    """Test that showNodes() is thread-safe."""
    with MeshInterface(noProto=True) as iface:
        iface.nodes = {
            f"!{i:08x}": {
                "num": i,
                "user": {"id": f"!{i:08x}", "longName": f"Node{i}"},
                "position": {},
            }
            for i in range(100)
        }
        iface.nodesByNum = {i: iface.nodes[f"!{i:08x}"] for i in range(100)}
        iface.myInfo = MagicMock()
        iface.myInfo.my_node_num = 0

        errors = []
        errors_lock = threading.Lock()

        def call_show_nodes() -> None:
            try:
                for _ in range(10):
                    iface.showNodes()
            except Exception as e:  # noqa: BLE001
                with errors_lock:
                    errors.append(e)

        threads = [threading.Thread(target=call_show_nodes) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(errors) == 0


@pytest.mark.unit
def test_concurrent_getNode() -> None:
    """Test that getNode() is thread-safe."""
    with MeshInterface(noProto=True) as iface:
        iface.nodesByNum = {
            i: {"num": i, "user": {"id": f"!{i:08x}"}} for i in range(100)
        }
        errors = []
        errors_lock = threading.Lock()

        def get_nodes() -> None:
            try:
                for i in range(50):
                    # Avoid channel/config waits in noProto mode; this test only
                    # validates concurrent access safety for getNode().
                    node = iface.getNode(f"!{i:08x}", requestChannels=False)
                    assert node is not None
            except Exception as e:  # noqa: BLE001
                with errors_lock:
                    errors.append(e)

        threads = [threading.Thread(target=get_nodes) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(errors) == 0


@pytest.mark.unit
def test_packet_id_no_collision_after_many_generations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Test that packet IDs don't collide after many generations."""
    next_random = iter(range(1_000_000))
    monkeypatch.setattr(
        "meshtastic.mesh_interface.random.randint",
        lambda _a, _b: next(next_random),
    )
    with MeshInterface(noProto=True) as iface:
        packet_ids = set()

        # Generate many packet IDs
        for _ in range(10000):
            packet_id = iface._generate_packet_id()
            assert packet_id not in packet_ids
            packet_ids.add(packet_id)

        # Verify all are unique
        assert len(packet_ids) == 10000


@pytest.mark.unit
def test_concurrent_sendText_with_queue() -> None:
    """Test that sendText() with queue is thread-safe."""
    with MeshInterface(noProto=True) as iface:
        iface.myInfo = MagicMock()
        iface.myInfo.my_node_num = 12345
        iface._localChannels = [channel_pb2.Channel(index=0)]
        errors = []
        errors_lock = threading.Lock()

        def send_texts() -> None:
            try:
                for i in range(10):
                    iface.sendText(f"message_{i}", wantAck=True)
            except Exception as e:  # noqa: BLE001
                with errors_lock:
                    errors.append(e)

        threads = [threading.Thread(target=send_texts) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(errors) == 0


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_init_subscribes_log_line_when_debug_output_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """MeshInterface should subscribe log-line printing when debugOut is provided."""
    subscribed: list[tuple[Any, str]] = []

    def _subscribe(handler: Any, topic: str) -> None:
        subscribed.append((handler, topic))

    monkeypatch.setattr(
        mesh_interface_module.pub,  # type: ignore[attr-defined]
        "subscribe",
        _subscribe,
    )

    with MeshInterface(noProto=True, debugOut=io.StringIO()):
        pass

    assert (MeshInterface._print_log_line, "meshtastic.log.line") in subscribed


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_exit_close_failure_paths(caplog: pytest.LogCaptureFixture) -> None:
    """__exit__ should suppress close() failures only while unwinding another exception."""
    iface = MeshInterface(noProto=True)
    iface.close = MagicMock(side_effect=RuntimeError("close failed"))  # type: ignore[method-assign]

    with caplog.at_level(logging.WARNING):
        iface.__exit__(ValueError, ValueError("inner"), None)
    assert "close() failed while unwinding an existing exception." in caplog.text

    with pytest.raises(RuntimeError, match="close failed"):
        iface.__exit__(None, None, None)


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_print_log_line_and_record_handlers(monkeypatch: pytest.MonkeyPatch) -> None:
    """_print_log_line should route by output type and _handle_log_* should normalize output."""
    color_printer = MagicMock()
    monkeypatch.setattr(mesh_interface_module, "print_color", color_printer)

    interface = types.SimpleNamespace(debugOut=io.StringIO())
    MeshInterface._print_log_line("message", interface)
    assert interface.debugOut.getvalue().strip() == "message"

    captured_callable: list[str] = []
    interface.debugOut = captured_callable.append
    MeshInterface._print_log_line("callable", interface)
    assert captured_callable == ["callable"]

    interface.debugOut = mesh_interface_module.sys.stdout  # type: ignore[attr-defined]
    MeshInterface._print_log_line("DEBUG log", interface)
    MeshInterface._print_log_line("INFO log", interface)
    MeshInterface._print_log_line("WARN log", interface)
    MeshInterface._print_log_line("ERR log", interface)
    MeshInterface._print_log_line("OTHER log", interface)
    assert color_printer.print.call_args_list[0].kwargs["color"] == "cyan"
    assert color_printer.print.call_args_list[1].kwargs["color"] == "white"
    assert color_printer.print.call_args_list[2].kwargs["color"] == "yellow"
    assert color_printer.print.call_args_list[3].kwargs["color"] == "red"

    sent_lines: list[str] = []
    monkeypatch.setattr(
        mesh_interface_module.pub,  # type: ignore[attr-defined]
        "sendMessage",
        lambda _topic, **kwargs: sent_lines.append(kwargs["line"]),
    )
    with MeshInterface(noProto=True) as iface:
        iface._handle_log_line("line-with-newline\n")
        record = mesh_pb2.LogRecord()
        record.message = "record-line\n"
        iface._handle_log_record(record)

    assert sent_lines == ["line-with-newline", "record-line"]


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_show_info_includes_metadata_summary() -> None:
    """showInfo() should include metadata output when metadata is present."""
    with MeshInterface(noProto=True) as iface:
        iface.metadata = mesh_pb2.DeviceMetadata(firmware_version="2.7.18")
        summary = iface.showInfo(file=io.StringIO())

    assert "Metadata:" in summary


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_show_nodes_handles_single_level_and_missing_nested_fields(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """showNodes() should handle single-level keys and missing nested paths without introspecting internals."""
    with MeshInterface(noProto=True) as iface:
        iface.nodesByNum = {
            1: {
                "num": 1,
                "shortName": "N1",
                "user": {"id": "!00000001"},
            }
        }
        iface.nodes = {"!00000001": iface.nodesByNum[1]}
        iface.localNode.nodeNum = 999
        table = iface.showNodes(
            showFields=["shortName", "user.id", "missing.path", "position.latitude"]
        )
        _ = capsys.readouterr()

    assert "shortName" in table
    assert "N1" in table
    assert "!00000001" in table
    assert "N/A" in table


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_show_nodes_formats_powered_battery_and_future_since(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """showNodes() should render battery sentinel values and future timestamps safely."""
    future_ts = int(time.time()) + 600
    with MeshInterface(noProto=True) as iface:
        iface.nodesByNum = {
            1: {
                "num": 1,
                "user": {
                    "id": "!00000001",
                    "longName": "Node1",
                    "shortName": "N1",
                    "hwModel": "UNSET",
                    "publicKey": "x",
                    "role": "CLIENT",
                },
                "deviceMetrics": {"batteryLevel": 101},
                "lastHeard": future_ts,
            }
        }
        iface.nodes = {"!00000001": iface.nodesByNum[1]}
        iface.localNode.nodeNum = 999
        table = iface.showNodes(
            showFields=["deviceMetrics.batteryLevel", "since", "user.id"]
        )
        _ = capsys.readouterr()

    assert "Powered" in table
    assert "N/A" in table


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_get_node_resets_retry_budget_on_new_channel_progress() -> None:
    """getNode() should reset retry countdown when partial channel progress is observed."""

    class _FakeNode:
        def __init__(self) -> None:
            self.partialChannels: list[int] = []
            self.request_calls: list[int] = []
            self.wait_calls = 0

        def requestChannels(self, startingIndex: int = 0) -> None:
            """Track channel request starting indexes."""
            self.request_calls.append(startingIndex)

        def waitForConfig(self) -> bool:
            """Return False once before succeeding to simulate partial progress."""
            self.wait_calls += 1
            if self.wait_calls == 1:
                self.partialChannels = [1]
                return False
            return True

    fake_node = _FakeNode()
    with MeshInterface(noProto=True) as iface:
        with patch("meshtastic.node.Node", return_value=fake_node):
            result = iface.getNode("!00112233", requestChannelAttempts=2)

    assert cast(Any, result) is fake_node
    assert fake_node.request_calls == [0, 1]


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_send_alert_and_mqtt_proxy_paths(monkeypatch: pytest.MonkeyPatch) -> None:
    """sendAlert() and sendMqttClientProxyMessage() should delegate with expected payloads."""
    with MeshInterface(noProto=True) as iface:
        send_data = MagicMock(return_value=mesh_pb2.MeshPacket())
        monkeypatch.setattr(iface, "sendData", send_data)
        response_cb = MagicMock()
        iface.sendAlert(
            "SOS",
            destinationId=42,
            onResponse=response_cb,
            channelIndex=2,
            hopLimit=3,
        )

        assert send_data.call_count == 1
        send_args = send_data.call_args
        assert send_args.args[0] == b"SOS"
        assert send_args.kwargs["portNum"] == portnums_pb2.PortNum.ALERT_APP
        assert send_args.kwargs["priority"] == mesh_pb2.MeshPacket.Priority.ALERT

        sent_to_radio: list[mesh_pb2.ToRadio] = []
        monkeypatch.setattr(iface, "_send_to_radio", sent_to_radio.append)
        iface.sendMqttClientProxyMessage("mesh/topic", b"payload")

        assert sent_to_radio
        assert sent_to_radio[0].mqttClientProxyMessage.topic == "mesh/topic"
        assert sent_to_radio[0].mqttClientProxyMessage.data == b"payload"


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_send_data_sets_reply_id_field() -> None:
    """sendData() should preserve the caller-provided reply id."""
    with MeshInterface(noProto=True) as iface:
        packet = iface.sendData(b"ok", destinationId=123, replyId=77)
    assert packet.decoded.reply_id == 77


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_send_position_waits_when_response_requested(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """sendPosition(wantResponse=True) should wire response callback and wait for position."""
    with MeshInterface(noProto=True) as iface:
        send_data = MagicMock(return_value=mesh_pb2.MeshPacket())
        wait_for_position = MagicMock()
        monkeypatch.setattr(iface, "sendData", send_data)
        monkeypatch.setattr(iface, "waitForPosition", wait_for_position)

        iface.sendPosition(
            latitude=47.0,
            longitude=-122.0,
            altitude=100,
            wantResponse=True,
        )

        on_response = send_data.call_args.kwargs["onResponse"]
        assert getattr(on_response, "__self__", None) is iface
        assert (
            getattr(on_response, "__func__", None) is MeshInterface.onResponsePosition
        )
        wait_for_position.assert_called_once()


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_on_response_position_success_and_routing_error(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """onResponsePosition() should log parsed position and raise on NO_RESPONSE routing errors."""
    with MeshInterface(noProto=True) as iface:
        position = mesh_pb2.Position()
        position.latitude_i = 471234567
        position.longitude_i = -971234567
        position.altitude = 250
        position.precision_bits = 32
        with caplog.at_level(logging.INFO, logger=mesh_interface_module.__name__):
            iface.onResponsePosition(
                {
                    "decoded": {
                        "portnum": portnums_pb2.PortNum.Name(
                            portnums_pb2.PortNum.POSITION_APP
                        ),
                        "payload": position.SerializeToString(),
                    }
                }
            )
        assert "Position received:" in caplog.text
        assert "full precision" in caplog.text

        unknown_position = mesh_pb2.Position()
        unknown_position.precision_bits = 5
        caplog.clear()
        with caplog.at_level(logging.INFO, logger=mesh_interface_module.__name__):
            iface.onResponsePosition(
                {
                    "decoded": {
                        "portnum": portnums_pb2.PortNum.Name(
                            portnums_pb2.PortNum.POSITION_APP
                        ),
                        "payload": unknown_position.SerializeToString(),
                    }
                }
            )
        assert "(unknown)" in caplog.text
        assert "precision:5" in caplog.text

        disabled_position = mesh_pb2.Position()
        disabled_position.precision_bits = 0
        caplog.clear()
        with caplog.at_level(logging.INFO, logger=mesh_interface_module.__name__):
            iface.onResponsePosition(
                {
                    "decoded": {
                        "portnum": portnums_pb2.PortNum.Name(
                            portnums_pb2.PortNum.POSITION_APP
                        ),
                        "payload": disabled_position.SerializeToString(),
                    }
                }
            )
        assert "position disabled" in caplog.text

        with pytest.raises(MeshInterface.MeshInterfaceError, match="No response"):
            iface.onResponsePosition(
                {
                    "decoded": {
                        "portnum": portnums_pb2.PortNum.Name(
                            portnums_pb2.PortNum.ROUTING_APP
                        ),
                        "routing": {"errorReason": "NO_RESPONSE"},
                    }
                }
            )


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_send_traceroute_and_response_rendering(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Trace-route send/wait logic and response logging should execute end-to-end."""
    with MeshInterface(noProto=True) as iface:
        iface.nodes = {
            "!1": {"num": 1},
            "!2": {"num": 2},
            "!3": {"num": 3},
        }
        send_data = MagicMock()
        wait_for_traceroute = MagicMock()
        monkeypatch.setattr(iface, "sendData", send_data)
        monkeypatch.setattr(iface, "waitForTraceRoute", wait_for_traceroute)
        iface.sendTraceRoute(dest=123, hopLimit=3, channelIndex=1)
        wait_for_traceroute.assert_called_once_with(2)

        route = mesh_pb2.RouteDiscovery()
        route.route.extend([11])
        route.snr_towards.extend([8, 12])
        route.route_back.extend([12])
        route.snr_back.extend([16, 20])
        with caplog.at_level(logging.INFO, logger=mesh_interface_module.__name__):
            iface.onResponseTraceRoute(
                {
                    "decoded": {"payload": route.SerializeToString()},
                    "to": 20,
                    "from": 21,
                    "hopStart": 1,
                }
            )

    assert "Route traced towards destination:" in caplog.text
    assert "Route traced back to us:" in caplog.text
    assert iface._acknowledgment.receivedTraceRoute is True


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_send_telemetry_supported_and_fallback_paths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """sendTelemetry() should populate each supported payload and warn/fallback for unknown values."""
    telemetry_calls: list[tuple[telemetry_pb2.Telemetry, dict[str, Any]]] = []
    with MeshInterface(noProto=True) as iface:
        iface.localNode.nodeNum = 77
        iface.nodesByNum = {
            77: {
                "deviceMetrics": {
                    "batteryLevel": 55,
                    "voltage": 4.1,
                    "channelUtilization": 1.5,
                    "airUtilTx": 0.5,
                    "uptimeSeconds": 123,
                }
            }
        }
        monkeypatch.setattr(
            iface,
            "sendData",
            lambda payload, *_args, **kwargs: telemetry_calls.append((payload, kwargs)),
        )
        wait_for_telemetry = MagicMock()
        monkeypatch.setattr(iface, "waitForTelemetry", wait_for_telemetry)

        iface.sendTelemetry(telemetryType="environment_metrics")
        iface.sendTelemetry(telemetryType="air_quality_metrics")
        iface.sendTelemetry(telemetryType="power_metrics")
        iface.sendTelemetry(telemetryType="local_stats")
        iface.sendTelemetry(telemetryType="device_metrics")
        with pytest.warns(DeprecationWarning):
            iface.sendTelemetry(telemetryType="invalid")
        with pytest.warns(DeprecationWarning):
            iface.sendTelemetry(telemetryType="invalid2")
        iface.sendTelemetry(telemetryType="device_metrics", wantResponse=True)

    assert telemetry_calls[0][0].HasField("environment_metrics")
    assert telemetry_calls[1][0].HasField("air_quality_metrics")
    assert telemetry_calls[2][0].HasField("power_metrics")
    assert telemetry_calls[3][0].HasField("local_stats")
    assert telemetry_calls[4][0].HasField("device_metrics")
    assert telemetry_calls[5][0].HasField("device_metrics")
    assert telemetry_calls[6][0].HasField("device_metrics")
    assert telemetry_calls[7][1]["onResponse"] is not None
    wait_for_telemetry.assert_called_once()


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_on_response_telemetry_paths(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """onResponseTelemetry() should handle device metrics, non-device metrics, and routing errors."""
    with MeshInterface(noProto=True) as iface:
        device_t = telemetry_pb2.Telemetry()
        device_t.device_metrics.battery_level = 95
        device_t.device_metrics.voltage = 4.23
        with caplog.at_level(logging.INFO, logger=mesh_interface_module.__name__):
            iface.onResponseTelemetry(
                {
                    "decoded": {
                        "portnum": portnums_pb2.PortNum.Name(
                            portnums_pb2.PortNum.TELEMETRY_APP
                        ),
                        "payload": device_t.SerializeToString(),
                    }
                }
            )
        assert "Telemetry received:" in caplog.text
        assert "Battery level:" in caplog.text

        env_t = telemetry_pb2.Telemetry()
        env_t.environment_metrics.temperature = 21.5
        caplog.clear()
        with caplog.at_level(logging.INFO, logger=mesh_interface_module.__name__):
            iface.onResponseTelemetry(
                {
                    "decoded": {
                        "portnum": portnums_pb2.PortNum.Name(
                            portnums_pb2.PortNum.TELEMETRY_APP
                        ),
                        "payload": env_t.SerializeToString(),
                    }
                }
            )
        assert "environmentMetrics:" in caplog.text

        with pytest.raises(MeshInterface.MeshInterfaceError, match="No response"):
            iface.onResponseTelemetry(
                {
                    "decoded": {
                        "portnum": portnums_pb2.PortNum.Name(
                            portnums_pb2.PortNum.ROUTING_APP
                        ),
                        "routing": {"errorReason": "NO_RESPONSE"},
                    }
                }
            )


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_on_response_waypoint_paths(caplog: pytest.LogCaptureFixture) -> None:
    """onResponseWaypoint() should log waypoint payloads and raise on routing NO_RESPONSE."""
    with MeshInterface(noProto=True) as iface:
        waypoint = mesh_pb2.Waypoint(name="WPT", id=5)
        with caplog.at_level(logging.INFO, logger=mesh_interface_module.__name__):
            iface.onResponseWaypoint(
                {
                    "decoded": {
                        "portnum": portnums_pb2.PortNum.Name(
                            portnums_pb2.PortNum.WAYPOINT_APP
                        ),
                        "payload": waypoint.SerializeToString(),
                    }
                }
            )
        assert "Waypoint received:" in caplog.text
        assert iface._acknowledgment.receivedWaypoint is True

        with pytest.raises(MeshInterface.MeshInterfaceError, match="No response"):
            iface.onResponseWaypoint(
                {
                    "decoded": {
                        "portnum": portnums_pb2.PortNum.Name(
                            portnums_pb2.PortNum.ROUTING_APP
                        ),
                        "routing": {"errorReason": "NO_RESPONSE"},
                    }
                }
            )


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_send_and_delete_waypoint_response_paths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """sendWaypoint()/deleteWaypoint() should set payload fields and wait when response is requested."""
    sent_payloads: list[mesh_pb2.Waypoint] = []
    with MeshInterface(noProto=True) as iface:
        wait_for_waypoint = MagicMock()
        monkeypatch.setattr(iface, "waitForWaypoint", wait_for_waypoint)

        def _capture_send_data(
            payload: mesh_pb2.Waypoint, *_args: Any, **_kwargs: Any
        ) -> mesh_pb2.MeshPacket:
            sent_payloads.append(payload)
            return mesh_pb2.MeshPacket()

        monkeypatch.setattr(iface, "sendData", _capture_send_data)
        monkeypatch.setattr(
            mesh_interface_module.secrets,  # type: ignore[attr-defined]
            "randbits",
            lambda _n: (1 << 32) - 1,
        )

        iface.sendWaypoint(
            name="A",
            description="B",
            icon=1,
            expire=60,
            waypoint_id=None,
            latitude=47.1,
            longitude=-96.2,
            wantResponse=True,
        )
        iface.sendWaypoint(
            name="C",
            description="D",
            icon=2,
            expire=120,
            waypoint_id=7,
            wantResponse=False,
        )
        iface.deleteWaypoint(9, wantResponse=True)
        iface.deleteWaypoint(10, wantResponse=False)

    assert sent_payloads[0].id != 0
    assert sent_payloads[0].latitude_i != 0
    assert sent_payloads[0].longitude_i != 0
    assert sent_payloads[1].id == 7
    assert sent_payloads[2].id == 9 and sent_payloads[2].expire == 0
    assert sent_payloads[3].id == 10 and sent_payloads[3].expire == 0
    assert wait_for_waypoint.call_count == 2


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_send_packet_calls_transport_when_proto_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """_send_packet() should invoke _send_to_radio() when protocol I/O is enabled."""
    with MeshInterface(noProto=True) as iface:
        iface.noProto = False
        iface.myInfo = MagicMock()
        iface.myInfo.my_node_num = 1
        sent: list[mesh_pb2.ToRadio] = []
        monkeypatch.setattr(iface, "_send_to_radio", sent.append)
        iface._send_packet(mesh_pb2.MeshPacket(), destinationId=1)
        assert sent


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_wait_helpers_raise_expected_timeout_errors() -> None:
    """waitFor* helper methods should raise MeshInterfaceError on timeout."""
    with MeshInterface(noProto=True) as iface:
        iface._timeout = MagicMock()
        iface._timeout.waitForAckNak.return_value = False
        iface._timeout.waitForTraceRoute.return_value = False
        iface._timeout.waitForTelemetry.return_value = False
        iface._timeout.waitForPosition.return_value = False
        iface._timeout.waitForWaypoint.return_value = False

        with pytest.raises(MeshInterface.MeshInterfaceError, match="acknowledgment"):
            iface.waitForAckNak()
        with pytest.raises(MeshInterface.MeshInterfaceError, match="traceroute"):
            iface.waitForTraceRoute(1)
        with pytest.raises(MeshInterface.MeshInterfaceError, match="telemetry"):
            iface.waitForTelemetry()
        with pytest.raises(MeshInterface.MeshInterfaceError, match="position"):
            iface.waitForPosition()
        with pytest.raises(MeshInterface.MeshInterfaceError, match="waypoint"):
            iface.waitForWaypoint()


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_public_key_and_optional_getters_none_paths(
    iface_with_nodes: MeshInterface,
) -> None:
    """GetPublicKey should return user key while optional local-node getters return None when absent."""
    iface = iface_with_nodes
    assert iface.myInfo is not None
    iface.myInfo.my_node_num = 2475227164
    assert iface.nodesByNum is not None
    node = iface.nodesByNum[2475227164]
    node["user"]["publicKey"] = b"abc"
    assert iface.getPublicKey() == b"abc"
    node["user"] = {}
    assert iface.getPublicKey() is None
    iface.myInfo = None
    assert iface.getPublicKey() is None

    iface.localNode = None  # type: ignore[assignment]
    assert iface.getCannedMessage() is None
    assert iface.getRingtone() is None


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_send_heartbeat_builds_to_radio_heartbeat(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """sendHeartbeat() should send a ToRadio with heartbeat field populated."""
    with MeshInterface(noProto=True) as iface:
        sent: list[mesh_pb2.ToRadio] = []
        monkeypatch.setattr(iface, "_send_to_radio", sent.append)
        iface.sendHeartbeat()
        assert sent[0].HasField("heartbeat")


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_start_config_skips_reserved_nodeless_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """_start_config() should bump generated config id if it equals NODELESS_WANT_CONFIG_ID."""
    with MeshInterface(noProto=True) as iface:
        monkeypatch.setattr(
            mesh_interface_module.random,  # type: ignore[attr-defined]
            "randint",
            lambda _a, _b: NODELESS_WANT_CONFIG_ID,
        )
        sent: list[mesh_pb2.ToRadio] = []
        monkeypatch.setattr(iface, "_send_to_radio", sent.append)
        iface._start_config()
    assert iface.configId == NODELESS_WANT_CONFIG_ID + 1
    assert sent[0].want_config_id == NODELESS_WANT_CONFIG_ID + 1


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_queue_helpers_cover_state_transitions() -> None:
    """Queue helper methods should cover unknown status, full queue, and pop/decrement logic."""
    with MeshInterface(noProto=True) as iface:
        iface.queueStatus = None
        assert iface._queue_has_free_space() is True
        iface._queue_claim()

        iface.queueStatus = mesh_pb2.QueueStatus(free=1, maxlen=2)
        assert iface._queue_has_free_space() is True
        iface._queue_claim()
        assert iface.queueStatus.free == 0

        iface.queue = OrderedDict()
        assert iface._queue_pop_for_send() is None

        iface.queue[1] = mesh_pb2.ToRadio()
        iface.queueStatus.free = 0
        assert iface._queue_pop_for_send() is None
        iface.queueStatus.free = 1
        popped = iface._queue_pop_for_send()
        assert popped is not None
        assert popped[0] == 1
        assert iface.queueStatus.free == 0


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_send_to_radio_waits_resends_and_tracks_requeue(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """_send_to_radio() should wait for queue space, resend queued packets, and requeue unacked items."""
    with MeshInterface(noProto=True) as iface:
        iface.noProto = False
        iface.queueStatus = mesh_pb2.QueueStatus(free=0, maxlen=10)
        existing = mesh_pb2.ToRadio()
        existing.packet.id = 100
        iface.queue[100] = existing
        iface.queue[150] = False

        incoming = mesh_pb2.ToRadio()
        incoming.packet.id = 200

        sent_ids: list[int] = []

        def _send_impl(msg: mesh_pb2.ToRadio) -> None:
            sent_ids.append(msg.packet.id if msg.HasField("packet") else -1)

        monkeypatch.setattr(iface, "_send_to_radio_impl", _send_impl)

        def _sleep_and_free(_seconds: float) -> None:
            assert iface.queueStatus is not None
            iface.queueStatus.free = 10

        monkeypatch.setattr(
            mesh_interface_module.time,  # type: ignore[attr-defined]
            "sleep",
            _sleep_and_free,
        )

        with caplog.at_level(logging.DEBUG):
            iface._send_to_radio(incoming)

        assert "Waiting for free space in TX Queue" in caplog.text
        assert 100 in sent_ids
        assert 200 in sent_ids

    class _RequeueQueue(OrderedDict[int, mesh_pb2.ToRadio | bool]):
        def __bool__(self) -> bool:
            return False

        def pop(  # type: ignore[override]
            self, key: int, default: mesh_pb2.ToRadio | bool = False
        ) -> mesh_pb2.ToRadio | bool:
            if key == 123:
                return True
            return super().pop(key, default)

    with MeshInterface(noProto=True) as iface:
        iface.noProto = False
        iface.queue = _RequeueQueue()
        packet = mesh_pb2.ToRadio()
        packet.packet.id = 123
        monkeypatch.setattr(iface, "_send_to_radio_impl", lambda _msg: None)
        pops = iter([(123, packet), None])
        original_pop = iface._queue_pop_for_send
        monkeypatch.setattr(iface, "_queue_pop_for_send", lambda: next(pops))
        iface._send_to_radio(mesh_pb2.ToRadio())
        monkeypatch.setattr(iface, "_queue_pop_for_send", original_pop)
        assert 123 in iface.queue


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_handle_config_complete_and_queue_status_branches() -> None:
    """_handle_config_complete() and _handle_queue_status_from_radio() should execute all key branches."""
    with MeshInterface(noProto=True) as iface:
        channel = channel_pb2.Channel(index=1)
        iface._localChannels = [channel]
        iface.localNode = MagicMock()
        iface._connected = MagicMock()  # type: ignore[method-assign]
        iface._handle_config_complete()
        iface.localNode.setChannels.assert_called_once_with([channel])
        iface._connected.assert_called_once()

        queued = mesh_pb2.ToRadio()
        queued.packet.id = 111
        iface.queue[111] = queued

        status_hit = mesh_pb2.QueueStatus(free=1, maxlen=4, res=0, mesh_packet_id=111)
        iface._handle_queue_status_from_radio(status_hit)
        assert 111 not in iface.queue

        status_unexpected = mesh_pb2.QueueStatus(
            free=1, maxlen=4, res=0, mesh_packet_id=222
        )
        iface._handle_queue_status_from_radio(status_unexpected)
        assert iface.queue[222] is False

        status_res = mesh_pb2.QueueStatus(free=1, maxlen=4, res=1, mesh_packet_id=222)
        iface._handle_queue_status_from_radio(status_res)
        assert iface.queue[222] is False


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_handle_from_radio_branch_matrix(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """_handle_from_radio() should handle metadata/node-info and non-config branch dispatch paths."""
    published_topics: list[str] = []
    monkeypatch.setattr(
        mesh_interface_module.publishingThread,  # type: ignore[attr-defined]
        "queueWork",
        lambda callback: callback(),
    )
    monkeypatch.setattr(
        mesh_interface_module.pub,  # type: ignore[attr-defined]
        "sendMessage",
        lambda topic, **_kwargs: published_topics.append(topic),
    )

    with MeshInterface(noProto=True) as iface:
        iface._start_config()

        metadata_msg = mesh_pb2.FromRadio()
        metadata_msg.metadata.firmware_version = "2.7.18"
        iface._handle_from_radio(metadata_msg.SerializeToString())
        assert iface.metadata is not None
        assert iface.metadata.firmware_version == "2.7.18"

        node_info_msg = mesh_pb2.FromRadio()
        node_info_msg.node_info.num = 999
        node_info_msg.node_info.user.id = "!000003e7"
        node_info_msg.node_info.user.long_name = "N999"
        node_info_msg.node_info.user.short_name = "N9"
        with caplog.at_level(logging.DEBUG):
            iface._handle_from_radio(node_info_msg.SerializeToString())
        assert "Node has no position key" in caplog.text

        handle_config_complete = MagicMock()
        handle_channel = MagicMock()
        handle_packet = MagicMock()
        handle_log_record = MagicMock()
        handle_queue_status = MagicMock()
        monkeypatch.setattr(iface, "_handle_config_complete", handle_config_complete)
        monkeypatch.setattr(iface, "_handle_channel", handle_channel)
        monkeypatch.setattr(iface, "_handle_packet_from_radio", handle_packet)
        monkeypatch.setattr(iface, "_handle_log_record", handle_log_record)
        monkeypatch.setattr(
            iface, "_handle_queue_status_from_radio", handle_queue_status
        )

        config_complete_msg = mesh_pb2.FromRadio()
        assert iface.configId is not None
        config_complete_msg.config_complete_id = iface.configId
        iface._handle_from_radio(config_complete_msg.SerializeToString())
        handle_config_complete.assert_called_once()

        channel_msg = mesh_pb2.FromRadio()
        channel_msg.channel.index = 1
        iface._handle_from_radio(channel_msg.SerializeToString())
        handle_channel.assert_called_once()

        packet_msg = mesh_pb2.FromRadio()
        packet_msg.packet.id = 10
        iface._handle_from_radio(packet_msg.SerializeToString())
        handle_packet.assert_called_once()

        log_msg = mesh_pb2.FromRadio()
        log_msg.log_record.message = "hello"
        iface._handle_from_radio(log_msg.SerializeToString())
        handle_log_record.assert_called_once()

        queue_msg = mesh_pb2.FromRadio()
        queue_msg.queueStatus.free = 1
        queue_msg.queueStatus.maxlen = 5
        iface._handle_from_radio(queue_msg.SerializeToString())
        handle_queue_status.assert_called_once()

        notif_msg = mesh_pb2.FromRadio()
        notif_msg.clientNotification.reply_id = 1
        iface._handle_from_radio(notif_msg.SerializeToString())

        mqtt_msg = mesh_pb2.FromRadio()
        mqtt_msg.mqttClientProxyMessage.topic = "t"
        iface._handle_from_radio(mqtt_msg.SerializeToString())

        xmodem_msg = mesh_pb2.FromRadio()
        xmodem_msg.xmodemPacket.control = cast(Any, 1)
        iface._handle_from_radio(xmodem_msg.SerializeToString())

        disconnected_calls: list[int] = []
        monkeypatch.setattr(
            MeshInterface,
            "_disconnected",
            lambda _iface: disconnected_calls.append(1),
        )
        restart_config = MagicMock()
        monkeypatch.setattr(iface, "_start_config", restart_config)
        rebooted_msg = mesh_pb2.FromRadio(rebooted=True)
        iface._handle_from_radio(rebooted_msg.SerializeToString())
        assert disconnected_calls == [1]
        restart_config.assert_called_once()

        with caplog.at_level(logging.DEBUG):
            iface._handle_from_radio(mesh_pb2.FromRadio().SerializeToString())
        assert "Unexpected FromRadio payload" in caplog.text

    assert "meshtastic.node.updated" in published_topics
    assert "meshtastic.clientNotification" in published_topics
    assert "meshtastic.mqttclientproxymessage" in published_topics
    assert "meshtastic.xmodempacket" in published_topics


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_handle_from_radio_config_and_module_config_branches() -> None:
    """_handle_from_radio() should copy each config/moduleConfig branch into localNode caches."""
    config_fields = [
        "device",
        "position",
        "power",
        "network",
        "display",
        "lora",
        "bluetooth",
        "security",
    ]
    module_fields = [
        "mqtt",
        "serial",
        "external_notification",
        "store_forward",
        "range_test",
        "telemetry",
        "canned_message",
        "audio",
        "remote_hardware",
        "neighbor_info",
        "detection_sensor",
        "ambient_lighting",
        "paxcounter",
        "traffic_management",
    ]

    with MeshInterface(noProto=True) as iface:
        for field in config_fields:
            msg = mesh_pb2.FromRadio()
            getattr(msg.config, field).SetInParent()
            iface._handle_from_radio(msg.SerializeToString())
            assert iface.localNode.localConfig.HasField(cast(Any, field))

        for field in module_fields:
            msg = mesh_pb2.FromRadio()
            getattr(msg.moduleConfig, field).SetInParent()
            iface._handle_from_radio(msg.SerializeToString())
            assert iface.localNode.moduleConfig.HasField(cast(Any, field))


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_node_num_to_id_invalid_user_payloads() -> None:
    """_node_num_to_id() should return None when user payload is missing or has invalid id type."""
    with MeshInterface(noProto=True) as iface:
        iface.nodesByNum = {
            1: {"num": 1, "user": "bad-user"},
            2: {"num": 2, "user": {"id": 123}},
        }
        assert iface._node_num_to_id(1) is None
        assert iface._node_num_to_id(2) is None


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_get_or_create_by_num_requires_initialized_database() -> None:
    """_get_or_create_by_num() should raise when nodesByNum is not initialized."""
    with MeshInterface(noProto=True) as iface:
        iface.nodesByNum = None
        with pytest.raises(MeshInterface.MeshInterfaceError, match="not initialized"):
            iface._get_or_create_by_num(5)


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_handle_channel_appends_to_local_channel_list() -> None:
    """_handle_channel() should append received channels to _localChannels."""
    with MeshInterface(noProto=True) as iface:
        channel = channel_pb2.Channel(index=3)
        iface._handle_channel(channel)
        assert iface._localChannels[-1].index == 3


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_handle_packet_from_radio_toid_warning_and_response_handler_paths(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """_handle_packet_from_radio() should log toId failures and execute protobuf/response-handler paths."""
    monkeypatch.setattr(
        mesh_interface_module.publishingThread,  # type: ignore[attr-defined]
        "queueWork",
        lambda callback: callback(),
    )

    with MeshInterface(noProto=True) as iface:
        packet_for_toid = mesh_pb2.MeshPacket()
        setattr(packet_for_toid, "from", 1)
        packet_for_toid.to = 2
        with patch.object(
            iface,
            "_node_num_to_id",
            side_effect=["!00000001", RuntimeError("toId failure")],
        ):
            with caplog.at_level(logging.WARNING):
                iface._handle_packet_from_radio(packet_for_toid, hack=True)
        assert "Not populating toId" in caplog.text

        on_receive_calls: list[int] = []
        on_ack_calls: list[int] = []
        ack_permitted_calls: list[int] = []

        def _on_receive(_iface: MeshInterface, _packet: dict[str, Any]) -> None:
            on_receive_calls.append(1)

        def _raising_callback(_packet: dict[str, Any]) -> None:
            raise RuntimeError(  # noqa: TRY003 - intentional test sentinel
                "handler boom"
            )

        def onAckNak(_packet: dict[str, Any]) -> None:  # noqa: N802
            on_ack_calls.append(1)

        def _ack_permitted_callback(_packet: dict[str, Any]) -> None:
            ack_permitted_calls.append(1)

        fake_protocol = types.SimpleNamespace(
            name="routing",
            protobufFactory=mesh_pb2.Routing,
            onReceive=_on_receive,
        )
        monkeypatch.setattr(
            mesh_interface_module,
            "protocols",
            {portnums_pb2.PortNum.ROUTING_APP: fake_protocol},
        )

        routing = mesh_pb2.Routing()
        routing.error_reason = mesh_pb2.Routing.Error.NONE

        p1 = mesh_pb2.MeshPacket()
        setattr(p1, "from", 10)
        p1.to = 11
        p1.decoded.portnum = portnums_pb2.PortNum.ROUTING_APP
        p1.decoded.payload = routing.SerializeToString()
        p1.decoded.request_id = 77
        iface.responseHandlers[77] = ResponseHandler(
            callback=_raising_callback, ackPermitted=True
        )
        iface._handle_packet_from_radio(p1, hack=True)

        p2 = mesh_pb2.MeshPacket()
        setattr(p2, "from", 12)
        p2.to = 13
        p2.decoded.portnum = portnums_pb2.PortNum.ROUTING_APP
        p2.decoded.payload = routing.SerializeToString()
        p2.decoded.request_id = 78
        iface.responseHandlers[78] = ResponseHandler(
            callback=onAckNak, ackPermitted=False
        )
        iface._handle_packet_from_radio(p2, hack=True)

        p3 = mesh_pb2.MeshPacket()
        setattr(p3, "from", 14)
        p3.to = 15
        p3.decoded.portnum = portnums_pb2.PortNum.ROUTING_APP
        p3.decoded.payload = routing.SerializeToString()
        p3.decoded.request_id = 79
        iface.responseHandlers[79] = ResponseHandler(
            callback=_ack_permitted_callback, ackPermitted=True
        )
        iface._handle_packet_from_radio(p3, hack=True)

    assert on_receive_calls == [1, 1, 1]
    assert on_ack_calls == [1]
    assert ack_permitted_calls == [1]
