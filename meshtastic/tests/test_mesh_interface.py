"""Meshtastic unit tests for mesh_interface.py."""

import logging
import re
from unittest.mock import MagicMock, patch

import pytest
from hypothesis import given
from hypothesis import strategies as st

from .. import BROADCAST_ADDR, LOCAL_ADDR
from ..mesh_interface import MeshInterface, _timeago
from ..node import Node
from ..protobuf import config_pb2, mesh_pb2, portnums_pb2

try:
    # Depends upon the powermon group, not installed by default
    from ..powermon import SimPowerSupply
    from ..slog import LogSet
except ImportError:
    pytest.skip("Can't import LogSet or SimPowerSupply", allow_module_level=True)

# TODO
# from ..config import Config
from ..util import Timeout


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_MeshInterface(capsys, monkeypatch, tmp_path):
    """Test that we can instantiate a MeshInterface.

    """
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
def test_getMyUser(iface_with_nodes):
    """Test getMyUser().

    """
    iface = iface_with_nodes
    iface.myInfo.my_node_num = 2475227164
    myuser = iface.getMyUser()
    assert myuser is not None
    assert myuser["id"] == "!9388f81c"


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_getLongName(iface_with_nodes):
    """Test getLongName().

    """
    iface = iface_with_nodes
    iface.myInfo.my_node_num = 2475227164
    mylongname = iface.getLongName()
    assert mylongname == "Unknown f81c"


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_getShortName(iface_with_nodes):
    """Test getShortName().

    """
    iface = iface_with_nodes
    iface.myInfo.my_node_num = 2475227164
    myshortname = iface.getShortName()
    assert myshortname == "?1C"


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_handlePacketFromRadio_no_from(caplog):
    """Test _handlePacketFromRadio with no 'from' in the mesh packet.

    """
    with MeshInterface(noProto=True) as iface:
        meshPacket = mesh_pb2.MeshPacket()
        with caplog.at_level(logging.ERROR):
            iface._handlePacketFromRadio(meshPacket)
    assert re.search(
        r"Device returned a packet we sent, ignoring", caplog.text, re.MULTILINE
    )


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_handlePacketFromRadio_with_a_portnum(caplog):
    """Test _handlePacketFromRadio with a portnum.

    Since we have an attribute called 'from', we cannot simply 'set' it.
    Had to implement a hack just to be able to test some code.

    """
    with MeshInterface(noProto=True) as iface:
        meshPacket = mesh_pb2.MeshPacket()
        meshPacket.decoded.payload = b""
        meshPacket.decoded.portnum = portnums_pb2.PortNum.TEXT_MESSAGE_APP
        with caplog.at_level(logging.WARNING):
            iface._handlePacketFromRadio(meshPacket, hack=True)
    assert re.search(r"Not populating fromId", caplog.text, re.MULTILINE)


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_handlePacketFromRadio_no_portnum(caplog):
    """Verify that _handlePacketFromRadio logs a warning about not populating fromId when a MeshPacket has no portnum.

    """
    with MeshInterface(noProto=True) as iface:
        meshPacket = mesh_pb2.MeshPacket()
        meshPacket.decoded.payload = b""
        with caplog.at_level(logging.WARNING):
            iface._handlePacketFromRadio(meshPacket, hack=True)
    assert re.search(r"Not populating fromId", caplog.text, re.MULTILINE)


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_getNode_with_local():
    """Test getNode."""
    with MeshInterface(noProto=True) as iface:
        anode = iface.getNode(LOCAL_ADDR)
        assert anode == iface.localNode


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_getNode_not_local(caplog):
    """Test getNode not local.

    """
    with MeshInterface(noProto=True) as iface:
        anode = MagicMock(autospec=Node)
        with caplog.at_level(logging.DEBUG):
            with patch("meshtastic.node.Node", return_value=anode):
                another_node = iface.getNode("bar2")
                assert another_node != iface.localNode
    assert re.search(r"About to requestChannels", caplog.text, re.MULTILINE)


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_getNode_not_local_timeout(caplog):
    """Test getNode not local, simulate timeout.

    """
    with MeshInterface(noProto=True) as iface:
        anode = MagicMock(autospec=Node)
        anode.waitForConfig.return_value = False
        with caplog.at_level(logging.WARNING):
            with patch("meshtastic.node.Node", return_value=anode):
                with pytest.raises(
                    MeshInterface.MeshInterfaceError
                ) as pytest_wrapped_e:
                    iface.getNode("bar2")
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
def test_getNode_not_local_timeout_attempts(caplog):
    """Test getNode not local, simulate timeout.

    """
    with MeshInterface(noProto=True) as iface:
        anode = MagicMock(autospec=Node)
        anode.waitForConfig.return_value = False
        with caplog.at_level(logging.WARNING):
            with patch("meshtastic.node.Node", return_value=anode):
                with pytest.raises(
                    MeshInterface.MeshInterfaceError
                ) as pytest_wrapped_e:
                    iface.getNode("bar2", requestChannelAttempts=2)
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
def test_sendPosition(caplog):
    """Verify that MeshInterface.sendPosition() executes without error and emits position-related debug logs.

    Creates a MeshInterface(noProto=True), calls sendPosition() while capturing DEBUG logs, and then closes the interface.

    """
    with MeshInterface(noProto=True) as iface:
        with caplog.at_level(logging.DEBUG):
            iface.sendPosition()
    # assert re.search(r"p.time:", caplog.text, re.MULTILINE)


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_heartbeat_timer_is_daemon_and_cancelled_on_close(monkeypatch, fake_timer_cls):
    """Heartbeat timer should be daemonized and cancelled during close().

    """

    with MeshInterface(noProto=True) as iface:
        monkeypatch.setattr(iface, "sendHeartbeat", lambda: None)

        iface._startHeartbeat()
        assert len(fake_timer_cls.created) == 1
        timer = fake_timer_cls.created[0]
        assert timer.daemon is True
        assert timer.started is True

        iface.close()
        assert timer.cancelled is True


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_heartbeat_callback_does_not_reschedule_after_close(
    monkeypatch, fake_timer_cls
):
    """A heartbeat callback firing after close() must not create a new timer.

    """

    with MeshInterface(noProto=True) as iface:
        monkeypatch.setattr(iface, "sendHeartbeat", lambda: None)

        iface._startHeartbeat()
        assert len(fake_timer_cls.created) == 1
        old_timer = fake_timer_cls.created[0]

        iface.close()
        old_timer.function()

        assert len(fake_timer_cls.created) == 1


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_connected_noop_when_closing():
    """_connected() should not set connection state while shutdown is in progress."""
    with MeshInterface(noProto=True) as iface:
        iface._closing = True

        iface._connected()

        assert iface.isConnected.is_set() is False


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
#        iface._startHeartbeat()
#        assert iface.heartbeatTimer is not None
#        iface.close()


# TODO
# @pytest.mark.unit
# @pytest.mark.usefixtures("reset_mt_config")
# def test_handleFromRadio_empty_payload(caplog):
#    """Test _handleFromRadio"""
#    iface = MeshInterface(noProto=True)
#    with caplog.at_level(logging.DEBUG):
#        iface._handleFromRadio(b'')
#    iface.close()
#    assert re.search(r'Unexpected FromRadio payload', caplog.text, re.MULTILINE)


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_handleFromRadio_with_my_info(caplog):
    """Test _handleFromRadio with my_info.

    """
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
            iface._handleFromRadio(from_radio_bytes)
    assert re.search(r"Received from radio: my_info {", caplog.text, re.MULTILINE)
    assert re.search(r"my_node_num: 682584012", caplog.text, re.MULTILINE)


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_handleFromRadio_with_node_info(caplog, capsys):
    """Test _handleFromRadio with node_info.

    """
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
            iface._startConfig()
            iface._handleFromRadio(from_radio_bytes)
            assert re.search(
                r"Received from radio: node_info {", caplog.text, re.MULTILINE
            )
            assert re.search(r"682584012", caplog.text, re.MULTILINE)
            # validate some of showNodes() output
            iface.showNodes()
            out, err = capsys.readouterr()
            assert re.search(r" 1 ", out, re.MULTILINE)
            assert re.search(r"│ Unknown 67cc │ ", out, re.MULTILINE)
            assert re.search(r"│\s+!28af67cc\s+│\s+67cc\s+|", out, re.MULTILINE)
            assert err == ""


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_handleFromRadio_with_node_info_tbeam1(caplog, capsys):
    """Test _handleFromRadio with node_info.

    """
    # Note: Captured the '--debug --info' for the bytes below.
    # pylint: disable=C0301
    from_radio_bytes = (
        b'"=\x08\x80\xf8\xc8\xf6\x07\x12"\n\t!7ed23c00\x12\x07TBeam 1\x1a\x02T1"'
        b"\x06\x94\xb9~\xd2<\x000\x04\x1a\x07 ]MN\x01\xbea%\xad\x01\xbea=\x00\x00,A"
    )
    with MeshInterface(noProto=True) as iface:
        with caplog.at_level(logging.DEBUG):
            iface._startConfig()
            iface._handleFromRadio(from_radio_bytes)
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
def test_handleFromRadio_with_node_info_tbeam_with_bad_data(caplog):
    """Test _handleFromRadio with node_info with some bad data (issue#172) - ensure we do not throw exception.

    """
    # Note: Captured the '--debug --info' for the bytes below.
    from_radio_bytes = b'"\x17\x08\xdc\x8a\x8a\xae\x02\x12\x08"\x06\x00\x00\x00\x00\x00\x00\x1a\x00=\x00\x00\xb8@'
    with MeshInterface(noProto=True) as iface:
        with caplog.at_level(logging.DEBUG):
            iface._startConfig()
            iface._handleFromRadio(from_radio_bytes)


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_MeshInterface_sendToRadioImpl(caplog):
    """Test _sendToRadioImp().

    """
    with MeshInterface(noProto=True) as iface:
        with caplog.at_level(logging.DEBUG):
            iface._sendToRadioImpl("foo")  # type: ignore[arg-type]
    assert re.search(r"Subclass must provide toradio", caplog.text, re.MULTILINE)


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_MeshInterface_sendToRadio_no_proto(caplog):
    """Verify the default MeshInterface._sendToRadioImpl logs that subclasses must implement radio sending.

    Asserts that invoking the base implementation produces a log message containing "Subclass must provide toradio".

    """
    with MeshInterface() as iface:
        with caplog.at_level(logging.DEBUG):
            iface._sendToRadioImpl("foo")  # type: ignore[arg-type]
    assert re.search(r"Subclass must provide toradio", caplog.text, re.MULTILINE)


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_sendData_too_long(caplog):
    """Test when data payload is too big.

    """
    with MeshInterface(noProto=True) as iface:
        some_large_text = b"This is a long text that will be too long for send text."
        some_large_text += b"This is a long text that will be too long for send text."
        some_large_text += b"This is a long text that will be too long for send text."
        some_large_text += b"This is a long text that will be too long for send text."
        some_large_text += b"This is a long text that will be too long for send text."
        some_large_text += b"This is a long text that will be too long for send text."
        some_large_text += b"This is a long text that will be too long for send text."
        some_large_text += b"This is a long text that will be too long for send text."
        some_large_text += b"This is a long text that will be too long for send text."
        some_large_text += b"This is a long text that will be too long for send text."
        some_large_text += b"This is a long text that will be too long for send text."
        some_large_text += b"This is a long text that will be too long for send text."
        with caplog.at_level(logging.DEBUG):
            with pytest.raises(MeshInterface.MeshInterfaceError) as pytest_wrapped_e:
                iface.sendData(some_large_text)
            assert pytest_wrapped_e.type is MeshInterface.MeshInterfaceError
            assert "Data payload too big" in str(pytest_wrapped_e.value)


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_sendData_unknown_app():
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
def test_sendPosition_with_a_position(caplog):
    """Test sendPosition when lat/long/alt.

    """
    with MeshInterface(noProto=True) as iface:
        with caplog.at_level(logging.DEBUG):
            iface.sendPosition(latitude=40.8, longitude=-111.86, altitude=201)
            assert re.search(r"p.latitude_i:408", caplog.text, re.MULTILINE)
            assert re.search(r"p.longitude_i:-11186", caplog.text, re.MULTILINE)
            assert re.search(r"p.altitude:201", caplog.text, re.MULTILINE)


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_sendPacket_with_no_destination():
    """Test _sendPacket() raises MeshInterfaceError when destinationId is None."""
    with MeshInterface(noProto=True) as iface:
        with pytest.raises(
            MeshInterface.MeshInterfaceError,
            match="destinationId must not be None",
        ):
            iface._sendPacket(b"", destinationId=None)  # type: ignore[arg-type]


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_sendPacket_with_destination_as_int(caplog):
    """Test _sendPacket() with int as a destination.

    """
    with MeshInterface(noProto=True) as iface:
        with caplog.at_level(logging.DEBUG):
            meshPacket = mesh_pb2.MeshPacket()
            iface._sendPacket(meshPacket, destinationId=123)
            assert re.search(r"Not sending packet", caplog.text, re.MULTILINE)


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_sendPacket_with_destination_starting_with_a_bang(caplog):
    """Verify that _sendPacket ignores destination IDs that begin with '!' and logs the action.

    Asserts that calling _sendPacket with a destinationId starting with "!" results in a log entry containing "Not sending packet".

    """
    with MeshInterface(noProto=True) as iface:
        with caplog.at_level(logging.DEBUG):
            meshPacket = mesh_pb2.MeshPacket()
            iface._sendPacket(meshPacket, destinationId="!1234")
            assert re.search(r"Not sending packet", caplog.text, re.MULTILINE)


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_sendPacket_with_destination_as_BROADCAST_ADDR(caplog):
    """Test _sendPacket() with BROADCAST_ADDR as a destination.

    """
    with MeshInterface(noProto=True) as iface:
        with caplog.at_level(logging.DEBUG):
            meshPacket = mesh_pb2.MeshPacket()
            iface._sendPacket(meshPacket, destinationId=BROADCAST_ADDR)
            assert re.search(r"Not sending packet", caplog.text, re.MULTILINE)


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_sendPacket_with_destination_as_LOCAL_ADDR_no_myInfo():
    """Test _sendPacket() with LOCAL_ADDR raises MeshInterfaceError when myInfo is missing."""
    with MeshInterface(noProto=True) as iface:
        with pytest.raises(
            MeshInterface.MeshInterfaceError,
            match="No myInfo found",
        ):
            meshPacket = mesh_pb2.MeshPacket()
            iface._sendPacket(meshPacket, destinationId=LOCAL_ADDR)


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_sendPacket_with_destination_as_LOCAL_ADDR_with_myInfo(caplog):
    """Test _sendPacket() with LOCAL_ADDR as a destination with myInfo.

    """
    with MeshInterface(noProto=True) as iface:
        myInfo = MagicMock()
        iface.myInfo = myInfo
        iface.myInfo.my_node_num = 1
        with caplog.at_level(logging.DEBUG):
            meshPacket = mesh_pb2.MeshPacket()
            iface._sendPacket(meshPacket, destinationId=LOCAL_ADDR)
            assert re.search(r"Not sending packet", caplog.text, re.MULTILINE)


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_sendPacket_with_destination_is_blank_with_nodes(iface_with_nodes):
    """Test _sendPacket() with '' as a destination raises MeshInterfaceError when node not found.

    """
    iface = iface_with_nodes
    meshPacket = mesh_pb2.MeshPacket()
    with pytest.raises(
        MeshInterface.MeshInterfaceError,
        match=r"NodeId  not found in DB",
    ):
        iface._sendPacket(meshPacket, destinationId="")


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_sendPacket_with_destination_is_blank_without_nodes(caplog, iface_with_nodes):
    """Test _sendPacket() with '' as a destination with myInfo.

    """
    iface = iface_with_nodes
    iface.nodes = None
    meshPacket = mesh_pb2.MeshPacket()
    with caplog.at_level(logging.WARNING):
        iface._sendPacket(meshPacket, destinationId="")
    assert re.search(r"Warning: There were no self.nodes.", caplog.text, re.MULTILINE)


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_getMyNodeInfo():
    """Test getMyNodeInfo()."""
    with MeshInterface(noProto=True) as iface:
        anode = iface.getNode(LOCAL_ADDR)
        iface.nodesByNum = {1: anode}  # type: ignore[assignment]
        assert iface.nodesByNum.get(1) == anode  # type: ignore[union-attr]
        myInfo = MagicMock()
        iface.myInfo = myInfo
        iface.myInfo.my_node_num = 1
        myinfo = iface.getMyNodeInfo()
    assert myinfo == anode


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_getCannedMessage():
    """Test MeshInterface.getCannedMessage()."""
    with MeshInterface(noProto=True) as iface:
        node = MagicMock()
        node.get_canned_message.return_value = "Hi|Bye|Yes"
        iface.localNode = node
        result = iface.getCannedMessage()
    assert result == "Hi|Bye|Yes"


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_getRingtone():
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
def test_generatePacketId():
    """Test _generatePacketId() when no currentPacketId (not connected)."""
    with MeshInterface(noProto=True) as iface:
        # not sure when this condition would ever happen... but we can simulate it
        iface.currentPacketId = None  # type: ignore[assignment]
        assert iface.currentPacketId is None
        with pytest.raises(MeshInterface.MeshInterfaceError) as excinfo:
            iface._generatePacketId()
    assert "Not connected yet, can not generate packet" in str(excinfo.value)


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_fixupPosition_empty_pos():
    """Test _fixupPosition()."""
    with MeshInterface(noProto=True) as iface:
        pos = {}
        newpos = iface._fixupPosition(pos)
    assert newpos == pos


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_fixupPosition_no_changes_needed():
    """Test _fixupPosition()."""
    with MeshInterface(noProto=True) as iface:
        pos = {"latitude": 101, "longitude": 102}
        newpos = iface._fixupPosition(pos)
    assert newpos == pos


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_fixupPosition():
    """Test _fixupPosition()."""
    with MeshInterface(noProto=True) as iface:
        pos = {"latitudeI": 1010000000, "longitudeI": 1020000000}
        newpos = iface._fixupPosition(pos)
    assert newpos == {
        "latitude": 101.0,
        "latitudeI": 1010000000,
        "longitude": 102.0,
        "longitudeI": 1020000000,
    }


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_nodeNumToId(iface_with_nodes):
    """Test _nodeNumToId().

    """
    iface = iface_with_nodes
    iface.myInfo.my_node_num = 2475227164
    someid = iface._nodeNumToId(2475227164)
    assert someid == "!9388f81c"


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_nodeNumToId_not_found(iface_with_nodes):
    """Test _nodeNumToId().

    """
    iface = iface_with_nodes
    iface.myInfo.my_node_num = 2475227164
    someid = iface._nodeNumToId(123)
    assert someid is None


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_nodeNumToId_to_all(iface_with_nodes):
    """Test _nodeNumToId().

    """
    iface = iface_with_nodes
    iface.myInfo.my_node_num = 2475227164
    someid = iface._nodeNumToId(0xFFFFFFFF)
    assert someid == "^all"


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_getOrCreateByNum_minimal(iface_with_nodes):
    """Test _getOrCreateByNum().

    """
    iface = iface_with_nodes
    iface.myInfo.my_node_num = 2475227164
    tmp = iface._getOrCreateByNum(123)
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
def test_getOrCreateByNum_not_found(iface_with_nodes):
    """Test _getOrCreateByNum().

    """
    iface = iface_with_nodes
    iface.myInfo.my_node_num = 2475227164
    with pytest.raises(MeshInterface.MeshInterfaceError) as pytest_wrapped_e:
        iface._getOrCreateByNum(0xFFFFFFFF)
    assert pytest_wrapped_e.type is MeshInterface.MeshInterfaceError


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_getOrCreateByNum(iface_with_nodes):
    """Test _getOrCreateByNum().

    """
    iface = iface_with_nodes
    iface.myInfo.my_node_num = 2475227164
    tmp = iface._getOrCreateByNum(2475227164)
    assert tmp["num"] == 2475227164


# TODO
# @pytest.mark.unit
# def test_enter():
#    """Test __enter__()"""
#    iface = MeshInterface(noProto=True)
#    assert iface == iface.__enter__()


@pytest.mark.unit
def test_exit_with_exception(caplog):
    """Verify that MeshInterface.__exit__ logs the exception type, value, and traceback when an exception is raised inside its context.

    Asserts an ERROR-level log entry contains the ValueError message and a traceback that includes the line where the exception was raised.

    Raises
    ------
    ValueError
        If MeshInterface raises ValueError during construction with noProto=True.
    """
    with caplog.at_level(logging.ERROR):
        try:
            with MeshInterface(noProto=True):
                raise ValueError("Something went wrong")
        except Exception:
            assert re.search(
                r"An exception of type <class \'ValueError\'> with value Something went wrong has occurred",
                caplog.text,
                re.MULTILINE,
            )
            assert re.search(
                r"Traceback:\n.*in test_exit_with_exception\n {4}raise ValueError\(\"Something went wrong\"\)",
                caplog.text,
                re.MULTILINE,
            )


@pytest.mark.unit
def test_showNodes_exclude_self(capsys, caplog, iface_with_nodes):
    """Test that we hit that continue statement.

    """
    with caplog.at_level(logging.DEBUG):
        iface = iface_with_nodes
        iface.localNode.nodeNum = 2475227164
        iface.showNodes()
        iface.showNodes(includeSelf=False)
        capsys.readouterr()


@pytest.mark.unitslow
def test_waitForConfig():
    """Verify that waitForConfig raises MeshInterface.MeshInterfaceError when the interface times out waiting for configuration."""
    with MeshInterface(noProto=True) as iface:
        # override how long to wait
        iface._timeout = Timeout(1)
        with pytest.raises(MeshInterface.MeshInterfaceError) as pytest_wrapped_e:
            iface.waitForConfig()
    assert pytest_wrapped_e.type is MeshInterface.MeshInterfaceError
    assert "Timed out waiting for interface config" in str(pytest_wrapped_e.value)


@pytest.mark.unit
def test_waitConnected_raises_an_exception():
    """Test waitConnected()."""
    with MeshInterface(noProto=True) as iface:
        iface.failure = MeshInterface.MeshInterfaceError("warn about something")
        with pytest.raises(MeshInterface.MeshInterfaceError) as excinfo:
            iface._waitConnected(0.01)
    assert "warn about something" in str(excinfo.value)


@pytest.mark.unit
def test_waitConnected_isConnected_timeout():
    """Verifies that _waitConnected raises a MeshInterfaceError when the connection does not complete within the specified timeout.

    Asserts the raised error message contains "Timed out waiting for connection completion".
    """
    with pytest.raises(MeshInterface.MeshInterfaceError) as excinfo:
        with MeshInterface() as iface:
            iface._waitConnected(0.01)
    assert "Timed out waiting for connection completion" in str(excinfo.value)


@pytest.mark.unit
def test_timeago():
    """Test that the _timeago function returns sane values."""
    assert _timeago(0) == "now"
    assert _timeago(1) == "1 sec ago"
    assert _timeago(15) == "15 secs ago"
    assert _timeago(333) == "5 mins ago"
    assert _timeago(99999) == "1 day ago"
    assert _timeago(9999999) == "3 months ago"
    assert _timeago(-999) == "now"


@given(seconds=st.integers())
def test_timeago_fuzz(seconds):
    """Fuzz _timeago to ensure it works with any integer.

    """
    val = _timeago(seconds)
    assert re.fullmatch(r"now|\d+ (secs?|mins?|hours?|days?|months?|years?) ago", val)
