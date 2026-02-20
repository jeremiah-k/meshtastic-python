"""Meshtastic unit tests for node.py."""

# pylint: disable=too-many-lines

import base64
import logging
import re
from unittest.mock import MagicMock, patch

import pytest

from ..mesh_interface import MeshInterface
from ..node import Node
from ..protobuf import admin_pb2, apponly_pb2, config_pb2, localonly_pb2
from ..protobuf.channel_pb2 import Channel  # pylint: disable=E0611
from ..serial_interface import SerialInterface
from ..util import Timeout


@pytest.mark.unit
def test_node(capsys, mock_serial_interface):
    """Test that we can instantiate a Node."""
    with patch(
        "meshtastic.serial_interface.SerialInterface",
        return_value=mock_serial_interface,
    ) as mo:
        anode = Node(mo, "!12345678", noProto=True)
        lc = localonly_pb2.LocalConfig()
        anode.localConfig = lc
        lc.lora.CopyFrom(config_pb2.Config.LoRaConfig())
        anode.moduleConfig = localonly_pb2.LocalModuleConfig()
        anode.showInfo()
        out, err = capsys.readouterr()
        assert re.search(r"Preferences", out)
        assert re.search(r"Module preferences", out)
        assert re.search(r"Channels", out)
        assert re.search(r"Primary channel URL", out)
        assert not re.search(r"remote node", out)
        assert err == ""


# TODO
# @pytest.mark.unit
# def test_node_requestConfig(capsys):
#    """Test run requestConfig"""
#    iface = MagicMock(autospec=SerialInterface)
#    amesg = MagicMock(autospec=AdminMessage)
#    with patch('meshtastic.serial_interface.SerialInterface', return_value=iface) as mo:
#        with patch('meshtastic.admin_pb2.AdminMessage', return_value=amesg):
#            anode = Node(mo, 'bar')
#            anode.requestConfig()
#    out, err = capsys.readouterr()
#    assert re.search(r'Requesting preferences from remote node', out, re.MULTILINE)
#    assert err == ''


# @pytest.mark.unit
# def test_node_get_canned_message_with_all_parts(capsys):
#    """Test run get_canned_message()"""
#    iface = MagicMock(autospec=SerialInterface)
#    amesg = MagicMock(autospec=AdminMessage)
#    with patch('meshtastic.serial_interface.SerialInterface', return_value=iface) as mo:
#        with patch('meshtastic.admin_pb2.AdminMessage', return_value=amesg):
#            # we have a sleep in this method, so override it so it goes fast
#            with patch('time.sleep'):
#                anode = Node(mo, 'bar')
#                anode.cannedPluginMessagePart1 = 'a'
#                anode.cannedPluginMessagePart2 = 'b'
#                anode.cannedPluginMessagePart3 = 'c'
#                anode.cannedPluginMessagePart4 = 'd'
#                anode.cannedPluginMessagePart5 = 'e'
#                anode.get_canned_message()
#    out, err = capsys.readouterr()
#    assert re.search(r'canned_plugin_message:abcde', out, re.MULTILINE)
#    assert err == ''
#
#
# @pytest.mark.unit
# def test_node_get_canned_message_with_some_parts(capsys):
#    """Test run get_canned_message()"""
#    iface = MagicMock(autospec=SerialInterface)
#    amesg = MagicMock(autospec=AdminMessage)
#    with patch('meshtastic.serial_interface.SerialInterface', return_value=iface) as mo:
#        with patch('meshtastic.admin_pb2.AdminMessage', return_value=amesg):
#            # we have a sleep in this method, so override it so it goes fast
#            with patch('time.sleep'):
#                anode = Node(mo, 'bar')
#                anode.cannedPluginMessagePart1 = 'a'
#                anode.get_canned_message()
#    out, err = capsys.readouterr()
#    assert re.search(r'canned_plugin_message:a', out, re.MULTILINE)
#    assert err == ''
#
#
# @pytest.mark.unit
# def test_node_set_canned_message_one_part(caplog):
#    """Test run set_canned_message()"""
#    iface = MagicMock(autospec=SerialInterface)
#    amesg = MagicMock(autospec=AdminMessage)
#    with caplog.at_level(logging.DEBUG):
#        with patch('meshtastic.serial_interface.SerialInterface', return_value=iface) as mo:
#            with patch('meshtastic.admin_pb2.AdminMessage', return_value=amesg):
#                anode = Node(mo, 'bar')
#                anode.set_canned_message('foo')
#    assert re.search(r"Setting canned message 'foo' part 1", caplog.text, re.MULTILINE)
#    assert not re.search(r"Setting canned message '' part 2", caplog.text, re.MULTILINE)
#
#
# @pytest.mark.unit
# def test_node_set_canned_message_200(caplog):
#    """Test run set_canned_message() 200 characters long"""
#    iface = MagicMock(autospec=SerialInterface)
#    amesg = MagicMock(autospec=AdminMessage)
#    with caplog.at_level(logging.DEBUG):
#        with patch('meshtastic.serial_interface.SerialInterface', return_value=iface) as mo:
#            with patch('meshtastic.admin_pb2.AdminMessage', return_value=amesg):
#                anode = Node(mo, 'bar')
#                message_200_chars_long = 'a' * 200
#                anode.set_canned_message(message_200_chars_long)
#    assert re.search(r" part 1", caplog.text, re.MULTILINE)
#    assert not re.search(r"Setting canned message '' part 2", caplog.text, re.MULTILINE)
#
#
# @pytest.mark.unit
# def test_node_set_canned_message_201(caplog):
#    """Test run set_canned_message() 201 characters long"""
#    iface = MagicMock(autospec=SerialInterface)
#    amesg = MagicMock(autospec=AdminMessage)
#    with caplog.at_level(logging.DEBUG):
#        with patch('meshtastic.serial_interface.SerialInterface', return_value=iface) as mo:
#            with patch('meshtastic.admin_pb2.AdminMessage', return_value=amesg):
#                anode = Node(mo, 'bar')
#                message_201_chars_long = 'a' * 201
#                anode.set_canned_message(message_201_chars_long)
#    assert re.search(r" part 1", caplog.text, re.MULTILINE)
#    assert re.search(r"Setting canned message 'a' part 2", caplog.text, re.MULTILINE)
#
#
# @pytest.mark.unit
# def test_node_set_canned_message_1000(caplog):
#    """Test run set_canned_message() 1000 characters long"""
#    iface = MagicMock(autospec=SerialInterface)
#    amesg = MagicMock(autospec=AdminMessage)
#    with caplog.at_level(logging.DEBUG):
#        with patch('meshtastic.serial_interface.SerialInterface', return_value=iface) as mo:
#            with patch('meshtastic.admin_pb2.AdminMessage', return_value=amesg):
#                anode = Node(mo, 'bar')
#                message_1000_chars_long = 'a' * 1000
#                anode.set_canned_message(message_1000_chars_long)
#    assert re.search(r" part 1", caplog.text, re.MULTILINE)
#    assert re.search(r" part 2", caplog.text, re.MULTILINE)
#    assert re.search(r" part 3", caplog.text, re.MULTILINE)
#    assert re.search(r" part 4", caplog.text, re.MULTILINE)
#    assert re.search(r" part 5", caplog.text, re.MULTILINE)
#
#
# @pytest.mark.unit
# def test_node_set_canned_message_1001(capsys):
#    """Test run set_canned_message() 1001 characters long"""
#    iface = MagicMock(autospec=SerialInterface)
#    with pytest.raises(SystemExit) as pytest_wrapped_e:
#        with patch('meshtastic.serial_interface.SerialInterface', return_value=iface) as mo:
#            anode = Node(mo, 'bar')
#            message_1001_chars_long = 'a' * 1001
#            anode.set_canned_message(message_1001_chars_long)
#    assert pytest_wrapped_e.type is SystemExit
#    assert pytest_wrapped_e.value.code == 1
#    out, err = capsys.readouterr()
#    assert re.search(r'Warning: The canned message', out, re.MULTILINE)
#    assert err == ''


@pytest.mark.unit
def test_exitSimulator(caplog):
    """Test exitSimulator."""
    interface = MeshInterface()
    interface.nodesByNum = {}
    anode = Node(interface, "!ba400000", noProto=True)
    with caplog.at_level(logging.DEBUG):
        anode.exitSimulator()
    assert re.search(r"in exitSimulator", caplog.text, re.MULTILINE)


@pytest.mark.unit
def test_reboot(caplog):
    """Test reboot."""
    interface = MeshInterface()
    interface.nodesByNum = {}
    anode = Node(interface, 1234567890, noProto=True)
    with caplog.at_level(logging.DEBUG):
        anode.reboot()
    assert re.search(r"Telling node to reboot", caplog.text, re.MULTILINE)


@pytest.mark.unit
def test_shutdown(caplog):
    """Test shutdown."""
    interface = MeshInterface()
    interface.nodesByNum = {}
    anode = Node(interface, 1234567890, noProto=True)
    with caplog.at_level(logging.DEBUG):
        anode.shutdown()
    assert re.search(r"Telling node to shutdown", caplog.text, re.MULTILINE)


@pytest.mark.unit
def test_setURL_empty_url():
    """Test setURL with an empty URL."""
    anode = Node(MagicMock(autospec=MeshInterface), "!12345678", noProto=True)
    with pytest.raises(
        MeshInterface.MeshInterfaceError, match="Warning: config or channels not loaded"
    ):
        anode.setURL("")


# TODO
# @pytest.mark.unit
# def test_setURL_valid_URL(caplog):
#    """Test setURL"""
#    iface = MagicMock(autospec=SerialInterface)
#    url = "https://www.meshtastic.org/d/#CgUYAyIBAQ"
#    with caplog.at_level(logging.DEBUG):
#        anode = Node(iface, 'bar', noProto=True)
#        anode.radioConfig = 'baz'
#        channels = ['zoo']
#        anode.channels = channels
#        anode.setURL(url)
#    assert re.search(r'Channel i:0', caplog.text, re.MULTILINE)
#    assert re.search(r'modem_config: MidSlow', caplog.text, re.MULTILINE)
#    assert re.search(r'psk: "\\001"', caplog.text, re.MULTILINE)
#    assert re.search(r'role: PRIMARY', caplog.text, re.MULTILINE)


@pytest.mark.unit
def test_setURL_valid_URL_but_no_settings():
    """Test setURL."""
    iface = MagicMock(autospec=SerialInterface)
    url = "https://www.meshtastic.org/d/#"
    anode = Node(iface, "!12345678", noProto=True)
    with pytest.raises(
        MeshInterface.MeshInterfaceError, match="Warning: config or channels not loaded"
    ):
        anode.setURL(url)


@pytest.mark.unit
def test_setURL_ignores_channels_over_device_limit(caplog):
    """Test that setURL ignores channels beyond the fixed device channel limit."""
    iface = MagicMock(autospec=MeshInterface)
    anode = Node(iface, "!12345678", noProto=True)
    anode.channels = [Channel(index=i, role=Channel.Role.DISABLED) for i in range(8)]

    channel_set = apponly_pb2.ChannelSet()
    for i in range(9):
        settings = channel_set.settings.add()
        settings.name = f"ch{i}"
        settings.psk = b"\x01"

    encoded = base64.urlsafe_b64encode(channel_set.SerializeToString()).decode("ascii")
    encoded = encoded.replace("=", "")
    url = f"https://meshtastic.org/e/#{encoded}"

    with caplog.at_level(logging.WARNING):
        anode.setURL(url)

    assert re.search(r"URL contains more than 8 channels", caplog.text, re.MULTILINE)
    assert anode.channels[0].settings.name == "ch0"
    assert anode.channels[7].settings.name == "ch7"


@pytest.mark.unit
def test_get_ringtone_times_out_without_response(caplog):
    """Test get_ringtone returns None if the response callback is never invoked."""
    anode = Node(MagicMock(autospec=MeshInterface), "!12345678", noProto=True)
    anode.module_available = MagicMock(return_value=True)  # type: ignore[method-assign]
    anode._timeout.expireTimeout = 0.01
    anode._sendAdmin = MagicMock()  # type: ignore[method-assign]

    with caplog.at_level(logging.WARNING):
        result = anode.get_ringtone()

    assert result is None
    assert re.search(
        r"Timed out waiting for ringtone response", caplog.text, re.MULTILINE
    )


@pytest.mark.unit
def test_get_canned_message_times_out_without_response(caplog):
    """Test get_canned_message returns None if the response callback is never invoked."""
    anode = Node(MagicMock(autospec=MeshInterface), "!12345678", noProto=True)
    anode.module_available = MagicMock(return_value=True)  # type: ignore[method-assign]
    anode._timeout.expireTimeout = 0.01
    anode._sendAdmin = MagicMock()  # type: ignore[method-assign]

    with caplog.at_level(logging.WARNING):
        result = anode.get_canned_message()

    assert result is None
    assert re.search(
        r"Timed out waiting for canned message response", caplog.text, re.MULTILINE
    )


# TODO
# @pytest.mark.unit
# def test_showChannels(capsys):
#    """Test showChannels"""
#    anode = Node('foo', 'bar')
#
#    # primary channel
#    # role: 0=Disabled, 1=Primary, 2=Secondary
#    # modem_config: 0-5
#    # role: 0=Disabled, 1=Primary, 2=Secondary
#    channel1 = Channel(index=1, role=1)
#    channel1.settings.modem_config = 3
#    channel1.settings.psk = b'\x01'
#
#    channel2 = Channel(index=2, role=2)
#    channel2.settings.psk = b'\x8a\x94y\x0e\xc6\xc9\x1e5\x91\x12@\xa60\xa8\xb43\x87\x00\xf2K\x0e\xe7\x7fAz\xcd\xf5\xb0\x900\xa84'
#    channel2.settings.name = 'testing'
#
#    channel3 = Channel(index=3, role=0)
#    channel4 = Channel(index=4, role=0)
#    channel5 = Channel(index=5, role=0)
#    channel6 = Channel(index=6, role=0)
#    channel7 = Channel(index=7, role=0)
#    channel8 = Channel(index=8, role=0)
#
#    channels = [ channel1, channel2, channel3, channel4, channel5, channel6, channel7, channel8 ]
#
#    anode.channels = channels
#    anode.showChannels()
#    out, err = capsys.readouterr()
#    assert re.search(r'Channels:', out, re.MULTILINE)
#    # primary channel
#    assert re.search(r'Primary channel URL', out, re.MULTILINE)
#    assert re.search(r'PRIMARY psk=default ', out, re.MULTILINE)
#    assert re.search(r'"modemConfig": "MidSlow"', out, re.MULTILINE)
#    assert re.search(r'"psk": "AQ=="', out, re.MULTILINE)
#    # secondary channel
#    assert re.search(r'SECONDARY psk=secret ', out, re.MULTILINE)
#    assert re.search(r'"psk": "ipR5DsbJHjWREkCmMKi0M4cA8ksO539Bes31sJAwqDQ="', out, re.MULTILINE)
#    assert err == ''


@pytest.mark.unit
def test_getChannelByChannelIndex():
    """Test getChannelByChannelIndex()."""
    anode = Node(MagicMock(autospec=MeshInterface), "!12345678", noProto=True)

    channel1 = Channel(index=1, role=Channel.Role.PRIMARY)  # primary channel
    channel2 = Channel(index=2, role=Channel.Role.SECONDARY)  # secondary channel
    channel3 = Channel(index=3, role=Channel.Role.DISABLED)
    channel4 = Channel(index=4, role=Channel.Role.DISABLED)
    channel5 = Channel(index=5, role=Channel.Role.DISABLED)
    channel6 = Channel(index=6, role=Channel.Role.DISABLED)
    channel7 = Channel(index=7, role=Channel.Role.DISABLED)
    channel8 = Channel(index=8, role=Channel.Role.DISABLED)

    channels = [
        channel1,
        channel2,
        channel3,
        channel4,
        channel5,
        channel6,
        channel7,
        channel8,
    ]

    anode.channels = channels

    # test primary
    assert anode.getChannelByChannelIndex(0) is not None
    # test secondary
    assert anode.getChannelByChannelIndex(1) is not None
    # test disabled
    assert anode.getChannelByChannelIndex(2) is not None
    # test invalid values
    assert anode.getChannelByChannelIndex(-1) is None
    assert anode.getChannelByChannelIndex(9) is None


# TODO
# @pytest.mark.unit
# def test_deleteChannel_try_to_delete_primary_channel(capsys):
#    """Try to delete primary channel."""
#    anode = Node('foo', 'bar')
#
#    channel1 = Channel(index=1, role=1)
#    channel1.settings.modem_config = 3
#    channel1.settings.psk = b'\x01'
#
#    # no secondary channels
#    channel2 = Channel(index=2, role=0)
#    channel3 = Channel(index=3, role=0)
#    channel4 = Channel(index=4, role=0)
#    channel5 = Channel(index=5, role=0)
#    channel6 = Channel(index=6, role=0)
#    channel7 = Channel(index=7, role=0)
#    channel8 = Channel(index=8, role=0)
#
#    channels = [ channel1, channel2, channel3, channel4, channel5, channel6, channel7, channel8 ]
#
#    anode.channels = channels
#    with pytest.raises(SystemExit) as pytest_wrapped_e:
#        anode.deleteChannel(0)
#    assert pytest_wrapped_e.type is SystemExit
#    assert pytest_wrapped_e.value.code == 1
#    out, err = capsys.readouterr()
#    assert re.search(r'Warning: Only SECONDARY channels can be deleted', out, re.MULTILINE)
#    assert err == ''


# TODO
# @pytest.mark.unit
# def test_deleteChannel_secondary():
#    """Try to delete a secondary channel."""
#
#    channel1 = Channel(index=1, role=1)
#    channel1.settings.modem_config = 3
#    channel1.settings.psk = b'\x01'
#
#    channel2 = Channel(index=2, role=2)
#    channel2.settings.psk = b'\x8a\x94y\x0e\xc6\xc9\x1e5\x91\x12@\xa60\xa8\xb43\x87\x00\xf2K\x0e\xe7\x7fAz\xcd\xf5\xb0\x900\xa84'
#    channel2.settings.name = 'testing'
#
#    channel3 = Channel(index=3, role=0)
#    channel4 = Channel(index=4, role=0)
#    channel5 = Channel(index=5, role=0)
#    channel6 = Channel(index=6, role=0)
#    channel7 = Channel(index=7, role=0)
#    channel8 = Channel(index=8, role=0)
#
#    channels = [ channel1, channel2, channel3, channel4, channel5, channel6, channel7, channel8 ]
#
#
#    iface = MagicMock(autospec=SerialInterface)
#    with patch('meshtastic.serial_interface.SerialInterface', return_value=iface) as mo:
#        mo.localNode.getChannelByName.return_value = None
#        mo.myInfo.max_channels = 8
#        anode = Node(mo, 'bar', noProto=True)
#
#        anode.channels = channels
#        assert len(anode.channels) == 8
#        assert channels[0].settings.modem_config == 3
#        assert channels[1].settings.name == 'testing'
#        assert channels[2].settings.name == ''
#        assert channels[3].settings.name == ''
#        assert channels[4].settings.name == ''
#        assert channels[5].settings.name == ''
#        assert channels[6].settings.name == ''
#        assert channels[7].settings.name == ''
#
#        anode.deleteChannel(1)
#
#        assert len(anode.channels) == 8
#        assert channels[0].settings.modem_config == 3
#        assert channels[1].settings.name == ''
#        assert channels[2].settings.name == ''
#        assert channels[3].settings.name == ''
#        assert channels[4].settings.name == ''
#        assert channels[5].settings.name == ''
#        assert channels[6].settings.name == ''
#        assert channels[7].settings.name == ''


# TODO
# @pytest.mark.unit
# def test_deleteChannel_secondary_with_admin_channel_after_testing():
#    """Try to delete a secondary channel where there is an admin channel."""
#
#    channel1 = Channel(index=1, role=1)
#    channel1.settings.modem_config = 3
#    channel1.settings.psk = b'\x01'
#
#    channel2 = Channel(index=2, role=2)
#    channel2.settings.psk = b'\x8a\x94y\x0e\xc6\xc9\x1e5\x91\x12@\xa60\xa8\xb43\x87\x00\xf2K\x0e\xe7\x7fAz\xcd\xf5\xb0\x900\xa84'
#    channel2.settings.name = 'testing'
#
#    channel3 = Channel(index=3, role=2)
#    channel3.settings.name = 'admin'
#
#    channel4 = Channel(index=4, role=0)
#    channel5 = Channel(index=5, role=0)
#    channel6 = Channel(index=6, role=0)
#    channel7 = Channel(index=7, role=0)
#    channel8 = Channel(index=8, role=0)
#
#    channels = [ channel1, channel2, channel3, channel4, channel5, channel6, channel7, channel8 ]
#
#
#    iface = MagicMock(autospec=SerialInterface)
#    with patch('meshtastic.serial_interface.SerialInterface', return_value=iface) as mo:
#        mo.localNode.getChannelByName.return_value = None
#        mo.myInfo.max_channels = 8
#        anode = Node(mo, 'bar', noProto=True)
#
#        # Note: Have to do this next line because every call to MagicMock object/method returns a new magic mock
#        mo.localNode = anode
#
#        assert mo.localNode == anode
#
#        anode.channels = channels
#        assert len(anode.channels) == 8
#        assert channels[0].settings.modem_config == 3
#        assert channels[1].settings.name == 'testing'
#        assert channels[2].settings.name == 'admin'
#        assert channels[3].settings.name == ''
#        assert channels[4].settings.name == ''
#        assert channels[5].settings.name == ''
#        assert channels[6].settings.name == ''
#        assert channels[7].settings.name == ''
#
#        anode.deleteChannel(1)
#
#        assert len(anode.channels) == 8
#        assert channels[0].settings.modem_config == 3
#        assert channels[1].settings.name == 'admin'
#        assert channels[2].settings.name == ''
#        assert channels[3].settings.name == ''
#        assert channels[4].settings.name == ''
#        assert channels[5].settings.name == ''
#        assert channels[6].settings.name == ''
#        assert channels[7].settings.name == ''


# TODO
# @pytest.mark.unit
# def test_deleteChannel_secondary_with_admin_channel_before_testing():
#    """Try to delete a secondary channel where there is an admin channel."""
#
#    channel1 = Channel(index=1, role=1)
#    channel1.settings.modem_config = 3
#    channel1.settings.psk = b'\x01'
#
#    channel2 = Channel(index=2, role=2)
#    channel2.settings.psk = b'\x8a\x94y\x0e\xc6\xc9\x1e5\x91\x12@\xa60\xa8\xb43\x87\x00\xf2K\x0e\xe7\x7fAz\xcd\xf5\xb0\x900\xa84'
#    channel2.settings.name = 'admin'
#
#    channel3 = Channel(index=3, role=2)
#    channel3.settings.name = 'testing'
#
#    channel4 = Channel(index=4, role=0)
#    channel5 = Channel(index=5, role=0)
#    channel6 = Channel(index=6, role=0)
#    channel7 = Channel(index=7, role=0)
#    channel8 = Channel(index=8, role=0)
#
#    channels = [ channel1, channel2, channel3, channel4, channel5, channel6, channel7, channel8 ]
#
#
#    iface = MagicMock(autospec=SerialInterface)
#    with patch('meshtastic.serial_interface.SerialInterface', return_value=iface) as mo:
#        mo.localNode.getChannelByName.return_value = None
#        mo.myInfo.max_channels = 8
#        anode = Node(mo, 'bar', noProto=True)
#
#        anode.channels = channels
#        assert len(anode.channels) == 8
#        assert channels[0].settings.modem_config == 3
#        assert channels[1].settings.name == 'admin'
#        assert channels[2].settings.name == 'testing'
#        assert channels[3].settings.name == ''
#        assert channels[4].settings.name == ''
#        assert channels[5].settings.name == ''
#        assert channels[6].settings.name == ''
#        assert channels[7].settings.name == ''
#
#        anode.deleteChannel(2)
#
#        assert len(anode.channels) == 8
#        assert channels[0].settings.modem_config == 3
#        assert channels[1].settings.name == 'admin'
#        assert channels[2].settings.name == ''
#        assert channels[3].settings.name == ''
#        assert channels[4].settings.name == ''
#        assert channels[5].settings.name == ''
#        assert channels[6].settings.name == ''
#        assert channels[7].settings.name == ''
#
#
# @pytest.mark.unit
# def test_getChannelByName():
#    """Get a channel by the name."""
#    anode = Node('foo', 'bar')
#
#    channel1 = Channel(index=1, role=1)
#    channel1.settings.modem_config = 3
#    channel1.settings.psk = b'\x01'
#
#    channel2 = Channel(index=2, role=2)
#    channel2.settings.psk = b'\x8a\x94y\x0e\xc6\xc9\x1e5\x91\x12@\xa60\xa8\xb43\x87\x00\xf2K\x0e\xe7\x7fAz\xcd\xf5\xb0\x900\xa84'
#    channel2.settings.name = 'admin'
#
#    channel3 = Channel(index=3, role=0)
#    channel4 = Channel(index=4, role=0)
#    channel5 = Channel(index=5, role=0)
#    channel6 = Channel(index=6, role=0)
#    channel7 = Channel(index=7, role=0)
#    channel8 = Channel(index=8, role=0)
#
#    channels = [ channel1, channel2, channel3, channel4, channel5, channel6, channel7, channel8 ]
#
#    anode.channels = channels
#    ch = anode.getChannelByName('admin')
#    assert ch.index == 2


# TODO
# @pytest.mark.unit
# def test_getChannelByName_invalid_name():
#    """Get a channel by the name but one that is not present."""
#    anode = Node('foo', 'bar')
#
#    channel1 = Channel(index=1, role=1)
#    channel1.settings.modem_config = 3
#    channel1.settings.psk = b'\x01'
#
#    channel2 = Channel(index=2, role=2)
#    channel2.settings.psk = b'\x8a\x94y\x0e\xc6\xc9\x1e5\x91\x12@\xa60\xa8\xb43\x87\x00\xf2K\x0e\xe7\x7fAz\xcd\xf5\xb0\x900\xa84'
#    channel2.settings.name = 'admin'
#
#    channel3 = Channel(index=3, role=0)
#    channel4 = Channel(index=4, role=0)
#    channel5 = Channel(index=5, role=0)
#    channel6 = Channel(index=6, role=0)
#    channel7 = Channel(index=7, role=0)
#    channel8 = Channel(index=8, role=0)
#
#    channels = [ channel1, channel2, channel3, channel4, channel5, channel6, channel7, channel8 ]
#
#    anode.channels = channels
#    ch = anode.getChannelByName('testing')
#    assert ch is None
#
#
# @pytest.mark.unit
# def test_getDisabledChannel():
#    """Get the first disabled channel."""
#    anode = Node('foo', 'bar')
#
#    channel1 = Channel(index=1, role=1)
#    channel1.settings.modem_config = 3
#    channel1.settings.psk = b'\x01'
#
#    channel2 = Channel(index=2, role=2)
#    channel2.settings.psk = b'\x8a\x94y\x0e\xc6\xc9\x1e5\x91\x12@\xa60\xa8\xb43\x87\x00\xf2K\x0e\xe7\x7fAz\xcd\xf5\xb0\x900\xa84'
#    channel2.settings.name = 'testingA'
#
#    channel3 = Channel(index=3, role=2)
#    channel3.settings.psk = b'\x8a\x94y\x0e\xc6\xc9\x1e5\x91\x12@\xa60\xa8\xb43\x87\x00\xf2K\x0e\xe7\x7fAz\xcd\xf5\xb0\x900\xa84'
#    channel3.settings.name = 'testingB'
#
#    channel4 = Channel(index=4, role=0)
#    channel5 = Channel(index=5, role=0)
#    channel6 = Channel(index=6, role=0)
#    channel7 = Channel(index=7, role=0)
#    channel8 = Channel(index=8, role=0)
#
#    channels = [ channel1, channel2, channel3, channel4, channel5, channel6, channel7, channel8 ]
#
#    anode.channels = channels
#    ch = anode.getDisabledChannel()
#    assert ch.index == 4


# TODO
# @pytest.mark.unit
# def test_getDisabledChannel_where_all_channels_are_used():
#    """Get the first disabled channel."""
#    anode = Node('foo', 'bar')
#
#    channel1 = Channel(index=1, role=1)
#    channel1.settings.modem_config = 3
#    channel1.settings.psk = b'\x01'
#
#    channel2 = Channel(index=2, role=2)
#    channel3 = Channel(index=3, role=2)
#    channel4 = Channel(index=4, role=2)
#    channel5 = Channel(index=5, role=2)
#    channel6 = Channel(index=6, role=2)
#    channel7 = Channel(index=7, role=2)
#    channel8 = Channel(index=8, role=2)
#
#    channels = [ channel1, channel2, channel3, channel4, channel5, channel6, channel7, channel8 ]
#
#    anode.channels = channels
#    ch = anode.getDisabledChannel()
#    assert ch is None


# TODO
# @pytest.mark.unit
# def test_getAdminChannelIndex():
#    """Get the 'admin' channel index."""
#    anode = Node('foo', 'bar')
#
#    channel1 = Channel(index=1, role=1)
#    channel1.settings.modem_config = 3
#    channel1.settings.psk = b'\x01'
#
#    channel2 = Channel(index=2, role=2)
#    channel2.settings.psk = b'\x8a\x94y\x0e\xc6\xc9\x1e5\x91\x12@\xa60\xa8\xb43\x87\x00\xf2K\x0e\xe7\x7fAz\xcd\xf5\xb0\x900\xa84'
#    channel2.settings.name = 'admin'
#
#    channel3 = Channel(index=3, role=0)
#    channel4 = Channel(index=4, role=0)
#    channel5 = Channel(index=5, role=0)
#    channel6 = Channel(index=6, role=0)
#    channel7 = Channel(index=7, role=0)
#    channel8 = Channel(index=8, role=0)
#
#    channels = [ channel1, channel2, channel3, channel4, channel5, channel6, channel7, channel8 ]
#
#    anode.channels = channels
#    i = anode._getAdminChannelIndex()
#    assert i == 2


# TODO
# @pytest.mark.unit
# def test_getAdminChannelIndex_when_no_admin_named_channel():
#    """Get the 'admin' channel when there is not one."""
#    anode = Node('foo', 'bar')
#
#    channel1 = Channel(index=1, role=1)
#    channel1.settings.modem_config = 3
#    channel1.settings.psk = b'\x01'
#
#    channel2 = Channel(index=2, role=0)
#    channel3 = Channel(index=3, role=0)
#    channel4 = Channel(index=4, role=0)
#    channel5 = Channel(index=5, role=0)
#    channel6 = Channel(index=6, role=0)
#    channel7 = Channel(index=7, role=0)
#    channel8 = Channel(index=8, role=0)
#
#    channels = [ channel1, channel2, channel3, channel4, channel5, channel6, channel7, channel8 ]
#
#    anode.channels = channels
#    i = anode._getAdminChannelIndex()
#    assert i == 0


# TODO
# TODO: should we check if we need to turn it off?
# @pytest.mark.unit
# def test_turnOffEncryptionOnPrimaryChannel(capsys):
#    """Turn off encryption when there is a psk."""
#    anode = Node('foo', 'bar', noProto=True)
#
#    channel1 = Channel(index=1, role=1)
#    channel1.settings.modem_config = 3
#    # value from using "--ch-set psk 0x1a1a1a1a2b2b2b2b1a1a1a1a2b2b2b2b1a1a1a1a2b2b2b2b1a1a1a1a2b2b2b2b "
#    channel1.settings.psk = b'\x1a\x1a\x1a\x1a++++\x1a\x1a\x1a\x1a++++\x1a\x1a\x1a\x1a++++\x1a\x1a\x1a\x1a++++'
#
#    channel2 = Channel(index=2, role=0)
#    channel3 = Channel(index=3, role=0)
#    channel4 = Channel(index=4, role=0)
#    channel5 = Channel(index=5, role=0)
#    channel6 = Channel(index=6, role=0)
#    channel7 = Channel(index=7, role=0)
#    channel8 = Channel(index=8, role=0)
#
#    channels = [ channel1, channel2, channel3, channel4, channel5, channel6, channel7, channel8 ]
#
#    anode.channels = channels
#    anode.turnOffEncryptionOnPrimaryChannel()
#    out, err = capsys.readouterr()
#    assert re.search(r'Writing modified channels to device', out)
#    assert err == ''


@pytest.mark.unit
def test_writeConfig_with_no_radioConfig():
    """Test writeConfig raises MeshInterfaceError for invalid config name."""
    anode = Node(MagicMock(autospec=MeshInterface), "!12345678", noProto=True)

    with pytest.raises(
        MeshInterface.MeshInterfaceError,
        match="Error: No valid config with name foo",
    ):
        anode.writeConfig("foo")


@pytest.mark.unit
def test_writeChannel_with_no_channels_raises_mesh_error():
    """Test writeChannel raises when channels have not been loaded."""
    anode = Node(MagicMock(autospec=MeshInterface), "!12345678", noProto=True)
    anode.channels = None

    with pytest.raises(
        MeshInterface.MeshInterfaceError, match="Error: No channels have been read"
    ):
        anode.writeChannel(0)


# TODO
# @pytest.mark.unit
# def test_writeConfig(caplog):
#    """Test writeConfig"""
#    anode = Node('foo', 'bar', noProto=True)
#    radioConfig = RadioConfig()
#    anode.radioConfig = radioConfig
#
#    with caplog.at_level(logging.DEBUG):
#        anode.writeConfig()
#    assert re.search(r'Wrote config', caplog.text, re.MULTILINE)


@pytest.mark.unit
def test_requestChannel_not_localNode(caplog):
    """
    Verify that requesting channel 0 on a non-local node logs a remote channel info request.

    Sets up a mocked SerialInterface and a Node that is not the local node, configures max channels,
    calls _requestChannel(0), and asserts that an INFO log contains "Requesting channel 0 info".
    """
    iface = MagicMock(autospec=SerialInterface)
    with patch("meshtastic.serial_interface.SerialInterface", return_value=iface) as mo:
        mo.localNode.getChannelByName.return_value = None
        mo.myInfo.max_channels = 8
        anode = Node(mo, "!12345678", noProto=True)
        with caplog.at_level(logging.INFO):
            anode._requestChannel(0)
            assert re.search(
                r"Requesting channel 0 info from remote node", caplog.text, re.MULTILINE
            )


@pytest.mark.unit
def test_requestChannel_localNode(caplog):
    """Test _requestChannel()."""
    iface = MagicMock(autospec=SerialInterface)
    with patch("meshtastic.serial_interface.SerialInterface", return_value=iface) as mo:
        mo.localNode.getChannelByName.return_value = None
        mo.myInfo.max_channels = 8
        anode = Node(mo, "!12345678", noProto=True)

        # Note: Have to do this next line because every call to MagicMock object/method returns a new magic mock
        mo.localNode = anode

        with caplog.at_level(logging.DEBUG):
            anode._requestChannel(0)
            assert re.search(r"Requesting channel 0", caplog.text, re.MULTILINE)
            assert not re.search(r"from remote node", caplog.text, re.MULTILINE)


@pytest.mark.unit
def test_requestChannels_non_localNode(caplog):
    """Test requestChannels() with a starting index of 0."""
    iface = MagicMock(autospec=SerialInterface)
    with patch("meshtastic.serial_interface.SerialInterface", return_value=iface) as mo:
        mo.localNode.getChannelByName.return_value = None
        mo.myInfo.max_channels = 8
        anode = Node(mo, "!12345678", noProto=True)
        # Set a sentinel value to verify it gets reset
        anode.partialChannels = [Channel()]
        with caplog.at_level(logging.DEBUG):
            anode.requestChannels(0)
            assert re.search(
                "Requesting channel 0 info from remote node", caplog.text, re.MULTILINE
            )
            assert not anode.partialChannels


@pytest.mark.unit
def test_requestChannels_non_localNode_starting_index(caplog):
    """Test requestChannels() with a starting index of non-0."""
    iface = MagicMock(autospec=SerialInterface)
    with patch("meshtastic.serial_interface.SerialInterface", return_value=iface) as mo:
        mo.localNode.getChannelByName.return_value = None
        mo.myInfo.max_channels = 8
        anode = Node(mo, "!12345678", noProto=True)
        sentinel_channel = Channel()
        anode.partialChannels = [sentinel_channel]
        with caplog.at_level(logging.DEBUG):
            anode.requestChannels(3)
            assert re.search(
                "Requesting channel 3 info from remote node", caplog.text, re.MULTILINE
            )
            # make sure it hasn't been initialized (identity check ensures list wasn't replaced)
            assert (
                len(anode.partialChannels) == 1
                and anode.partialChannels[0] is sentinel_channel
            )


# @pytest.mark.unit
# def test_onResponseRequestCannedMessagePluginMesagePart1(caplog):
#    """Test onResponseRequestCannedMessagePluginMessagePart1()"""
#
#    part1 = CannedMessagePluginMessagePart1()
#    part1.text = 'foo1'
#
#    msg1 = MagicMock(autospec=AdminMessage)
#    msg1.get_canned_message_plugin_part1_response = part1
#
#    packet = {
#            'from': 682968612,
#            'to': 682968612,
#            'decoded': {
#                'portnum': 'ADMIN_APP',
#                'payload': 'faked',
#                'requestId': 927039000,
#                'admin': {
#                    'getCannedMessagePluginPart1Response': {'text': 'foo1'},
#                    'raw': msg1
#                    }
#                },
#            'id': 589440320,
#            'rxTime': 1642710843,
#            'hopLimit': 3,
#            'priority': 'RELIABLE',
#            'raw': 'faked',
#            'fromId': '!28b54624',
#            'toId': '!28b54624'
#            }
#
#    iface = MagicMock(autospec=SerialInterface)
#    with patch('meshtastic.serial_interface.SerialInterface', return_value=iface) as mo:
#        anode = Node(mo, 'bar', noProto=True)
#        # Note: Have to do this next line because every call to MagicMock object/method returns a new magic mock
#        mo.localNode = anode
#
#        with caplog.at_level(logging.DEBUG):
#            anode.onResponseRequestCannedMessagePluginMessagePart1(packet)
#            assert re.search(r'onResponseRequestCannedMessagePluginMessagePart1', caplog.text, re.MULTILINE)
#            assert anode.cannedPluginMessagePart1 == 'foo1'


# @pytest.mark.unit
# def test_onResponseRequestCannedMessagePluginMesagePart2(caplog):
#    """Test onResponseRequestCannedMessagePluginMessagePart2()"""
#
#    part2 = CannedMessagePluginMessagePart2()
#    part2.text = 'foo2'
#
#    msg2 = MagicMock(autospec=AdminMessage)
#    msg2.get_canned_message_plugin_part2_response = part2
#
#    packet = {
#            'from': 682968612,
#            'to': 682968612,
#            'decoded': {
#                'portnum': 'ADMIN_APP',
#                'payload': 'faked',
#                'requestId': 927039000,
#                'admin': {
#                    'getCannedMessagePluginPart2Response': {'text': 'foo2'},
#                    'raw': msg2
#                    }
#                },
#            'id': 589440320,
#            'rxTime': 1642710843,
#            'hopLimit': 3,
#            'priority': 'RELIABLE',
#            'raw': 'faked',
#            'fromId': '!28b54624',
#            'toId': '!28b54624'
#            }
#
#    iface = MagicMock(autospec=SerialInterface)
#    with patch('meshtastic.serial_interface.SerialInterface', return_value=iface) as mo:
#        anode = Node(mo, 'bar', noProto=True)
#        # Note: Have to do this next line because every call to MagicMock object/method returns a new magic mock
#        mo.localNode = anode
#
#        with caplog.at_level(logging.DEBUG):
#            anode.onResponseRequestCannedMessagePluginMessagePart2(packet)
#            assert re.search(r'onResponseRequestCannedMessagePluginMessagePart2', caplog.text, re.MULTILINE)
#            assert anode.cannedPluginMessagePart2 == 'foo2'


# @pytest.mark.unit
# def test_onResponseRequestCannedMessagePluginMesagePart3(caplog):
#    """Test onResponseRequestCannedMessagePluginMessagePart3()"""
#
#    part3 = CannedMessagePluginMessagePart3()
#    part3.text = 'foo3'
#
#    msg3 = MagicMock(autospec=AdminMessage)
#    msg3.get_canned_message_plugin_part3_response = part3
#
#    packet = {
#            'from': 682968612,
#            'to': 682968612,
#            'decoded': {
#                'portnum': 'ADMIN_APP',
#                'payload': 'faked',
#                'requestId': 927039000,
#                'admin': {
#                    'getCannedMessagePluginPart3Response': {'text': 'foo3'},
#                    'raw': msg3
#                    }
#                },
#            'id': 589440320,
#            'rxTime': 1642710843,
#            'hopLimit': 3,
#            'priority': 'RELIABLE',
#            'raw': 'faked',
#            'fromId': '!28b54624',
#            'toId': '!28b54624'
#            }
#
#    iface = MagicMock(autospec=SerialInterface)
#    with patch('meshtastic.serial_interface.SerialInterface', return_value=iface) as mo:
#        anode = Node(mo, 'bar', noProto=True)
#        # Note: Have to do this next line because every call to MagicMock object/method returns a new magic mock
#        mo.localNode = anode
#
#        with caplog.at_level(logging.DEBUG):
#            anode.onResponseRequestCannedMessagePluginMessagePart3(packet)
#            assert re.search(r'onResponseRequestCannedMessagePluginMessagePart3', caplog.text, re.MULTILINE)
#            assert anode.cannedPluginMessagePart3 == 'foo3'


# @pytest.mark.unit
# def test_onResponseRequestCannedMessagePluginMesagePart4(caplog):
#    """Test onResponseRequestCannedMessagePluginMessagePart4()"""
#
#    part4 = CannedMessagePluginMessagePart4()
#    part4.text = 'foo4'
#
#    msg4 = MagicMock(autospec=AdminMessage)
#    msg4.get_canned_message_plugin_part4_response = part4
#
#    packet = {
#            'from': 682968612,
#            'to': 682968612,
#            'decoded': {
#                'portnum': 'ADMIN_APP',
#                'payload': 'faked',
#                'requestId': 927039000,
#                'admin': {
#                    'getCannedMessagePluginPart4Response': {'text': 'foo4'},
#                    'raw': msg4
#                    }
#                },
#            'id': 589440320,
#            'rxTime': 1642710843,
#            'hopLimit': 3,
#            'priority': 'RELIABLE',
#            'raw': 'faked',
#            'fromId': '!28b54624',
#            'toId': '!28b54624'
#            }
#
#    iface = MagicMock(autospec=SerialInterface)
#    with patch('meshtastic.serial_interface.SerialInterface', return_value=iface) as mo:
#        anode = Node(mo, 'bar', noProto=True)
#        # Note: Have to do this next line because every call to MagicMock object/method returns a new magic mock
#        mo.localNode = anode
#
#        with caplog.at_level(logging.DEBUG):
#            anode.onResponseRequestCannedMessagePluginMessagePart4(packet)
#            assert re.search(r'onResponseRequestCannedMessagePluginMessagePart4', caplog.text, re.MULTILINE)
#            assert anode.cannedPluginMessagePart4 == 'foo4'


# @pytest.mark.unit
# def test_onResponseRequestCannedMessagePluginMesagePart5(caplog):
#    """Test onResponseRequestCannedMessagePluginMessagePart5()"""
#
#    part5 = CannedMessagePluginMessagePart5()
#    part5.text = 'foo5'
#
#    msg5 = MagicMock(autospec=AdminMessage)
#    msg5.get_canned_message_plugin_part5_response = part5
#
#
#    packet = {
#            'from': 682968612,
#            'to': 682968612,
#            'decoded': {
#                'portnum': 'ADMIN_APP',
#                'payload': 'faked',
#                'requestId': 927039000,
#                'admin': {
#                    'getCannedMessagePluginPart5Response': {'text': 'foo5'},
#                    'raw': msg5
#                    }
#                },
#            'id': 589440320,
#            'rxTime': 1642710843,
#            'hopLimit': 3,
#            'priority': 'RELIABLE',
#            'raw': 'faked',
#            'fromId': '!28b54624',
#            'toId': '!28b54624'
#            }
#
#    iface = MagicMock(autospec=SerialInterface)
#    with patch('meshtastic.serial_interface.SerialInterface', return_value=iface) as mo:
#        anode = Node(mo, 'bar', noProto=True)
#        # Note: Have to do this next line because every call to MagicMock object/method returns a new magic mock
#        mo.localNode = anode
#
#        with caplog.at_level(logging.DEBUG):
#            anode.onResponseRequestCannedMessagePluginMessagePart5(packet)
#            assert re.search(r'onResponseRequestCannedMessagePluginMessagePart5', caplog.text, re.MULTILINE)
#            assert anode.cannedPluginMessagePart5 == 'foo5'


# @pytest.mark.unit
# def test_onResponseRequestCannedMessagePluginMesagePart1_error(caplog, capsys):
#    """Test onResponseRequestCannedMessagePluginMessagePart1() with error"""
#
#    packet = {
#            'decoded': {
#                'routing': {
#                    'errorReason': 'some made up error',
#                    },
#                },
#            }
#
#    iface = MagicMock(autospec=SerialInterface)
#    with patch('meshtastic.serial_interface.SerialInterface', return_value=iface) as mo:
#        anode = Node(mo, 'bar', noProto=True)
#        # Note: Have to do this next line because every call to MagicMock object/method returns a new magic mock
#        mo.localNode = anode
#
#        with caplog.at_level(logging.DEBUG):
#            anode.onResponseRequestCannedMessagePluginMessagePart1(packet)
#            assert re.search(r'onResponseRequestCannedMessagePluginMessagePart1', caplog.text, re.MULTILINE)
#        out, err = capsys.readouterr()
#        assert re.search(r'Error on response', out)
#        assert err == ''


# @pytest.mark.unit
# def test_onResponseRequestCannedMessagePluginMesagePart2_error(caplog, capsys):
#    """Test onResponseRequestCannedMessagePluginMessagePart2() with error"""
#
#    packet = {
#            'decoded': {
#                'routing': {
#                    'errorReason': 'some made up error',
#                    },
#                },
#            }
#
#    iface = MagicMock(autospec=SerialInterface)
#    with patch('meshtastic.serial_interface.SerialInterface', return_value=iface) as mo:
#        anode = Node(mo, 'bar', noProto=True)
#        # Note: Have to do this next line because every call to MagicMock object/method returns a new magic mock
#        mo.localNode = anode
#
#        with caplog.at_level(logging.DEBUG):
#            anode.onResponseRequestCannedMessagePluginMessagePart2(packet)
#            assert re.search(r'onResponseRequestCannedMessagePluginMessagePart2', caplog.text, re.MULTILINE)
#        out, err = capsys.readouterr()
#        assert re.search(r'Error on response', out)
#        assert err == ''


# @pytest.mark.unit
# def test_onResponseRequestCannedMessagePluginMesagePart3_error(caplog, capsys):
#    """Test onResponseRequestCannedMessagePluginMessagePart3() with error"""
#
#    packet = {
#            'decoded': {
#                'routing': {
#                    'errorReason': 'some made up error',
#                    },
#                },
#            }
#
#    iface = MagicMock(autospec=SerialInterface)
#    with patch('meshtastic.serial_interface.SerialInterface', return_value=iface) as mo:
#        anode = Node(mo, 'bar', noProto=True)
#        # Note: Have to do this next line because every call to MagicMock object/method returns a new magic mock
#        mo.localNode = anode
#
#        with caplog.at_level(logging.DEBUG):
#            anode.onResponseRequestCannedMessagePluginMessagePart3(packet)
#            assert re.search(r'onResponseRequestCannedMessagePluginMessagePart3', caplog.text, re.MULTILINE)
#        out, err = capsys.readouterr()
#        assert re.search(r'Error on response', out)
#        assert err == ''
#
#
# @pytest.mark.unit
# def test_onResponseRequestCannedMessagePluginMesagePart4_error(caplog, capsys):
#    """Test onResponseRequestCannedMessagePluginMessagePart4() with error"""
#
#    packet = {
#            'decoded': {
#                'routing': {
#                    'errorReason': 'some made up error',
#                    },
#                },
#            }
#
#    iface = MagicMock(autospec=SerialInterface)
#    with patch('meshtastic.serial_interface.SerialInterface', return_value=iface) as mo:
#        anode = Node(mo, 'bar', noProto=True)
#        # Note: Have to do this next line because every call to MagicMock object/method returns a new magic mock
#        mo.localNode = anode
#
#        with caplog.at_level(logging.DEBUG):
#            anode.onResponseRequestCannedMessagePluginMessagePart4(packet)
#            assert re.search(r'onResponseRequestCannedMessagePluginMessagePart4', caplog.text, re.MULTILINE)
#        out, err = capsys.readouterr()
#        assert re.search(r'Error on response', out)
#        assert err == ''
#
#
# @pytest.mark.unit
# def test_onResponseRequestCannedMessagePluginMesagePart5_error(caplog, capsys):
#    """Test onResponseRequestCannedMessagePluginMessagePart5() with error"""
#
#    packet = {
#            'decoded': {
#                'routing': {
#                    'errorReason': 'some made up error',
#                    },
#                },
#            }
#
#    iface = MagicMock(autospec=SerialInterface)
#    with patch('meshtastic.serial_interface.SerialInterface', return_value=iface) as mo:
#        anode = Node(mo, 'bar', noProto=True)
#        # Note: Have to do this next line because every call to MagicMock object/method returns a new magic mock
#        mo.localNode = anode
#
#        with caplog.at_level(logging.DEBUG):
#            anode.onResponseRequestCannedMessagePluginMessagePart5(packet)
#            assert re.search(r'onResponseRequestCannedMessagePluginMessagePart5', caplog.text, re.MULTILINE)
#        out, err = capsys.readouterr()
#        assert re.search(r'Error on response', out)
#        assert err == ''


# TODO
# @pytest.mark.unit
# def test_onResponseRequestChannel(caplog):
#    """Test onResponseRequestChannel()"""
#
#    channel1 = Channel(index=1, role=1)
#    channel1.settings.modem_config = 3
#    channel1.settings.psk = b'\x01'
#
#    msg1 = MagicMock(autospec=AdminMessage)
#    msg1.get_channel_response = channel1
#
#    msg2 = MagicMock(autospec=AdminMessage)
#    channel2 = Channel(index=2, role=0) # disabled
#    msg2.get_channel_response = channel2
#
#    # default primary channel
#    packet1 = {
#        'from': 2475227164,
#        'to': 2475227164,
#        'decoded': {
#            'portnum': 'ADMIN_APP',
#            'payload': b':\t\x12\x05\x18\x03"\x01\x01\x18\x01',
#            'requestId': 2615094405,
#            'admin': {
#                'getChannelResponse': {
#                    'settings': {
#                        'modemConfig': 'Bw125Cr48Sf4096',
#                        'psk': 'AQ=='
#                    },
#                    'role': 'PRIMARY'
#                },
#                'raw': msg1,
#            }
#        },
#        'id': 1692918436,
#        'hopLimit': 3,
#        'priority': 'RELIABLE',
#        'raw': 'fake',
#        'fromId': '!9388f81c',
#        'toId': '!9388f81c'
#        }
#
#    # no other channels
#    packet2 = {
#        'from': 2475227164,
#        'to': 2475227164,
#        'decoded': {
#            'portnum': 'ADMIN_APP',
#            'payload': b':\x04\x08\x02\x12\x00',
#            'requestId': 743049663,
#            'admin': {
#                'getChannelResponse': {
#                    'index': 2,
#                    'settings': {}
#                },
#                'raw': msg2,
#            }
#        },
#        'id': 1692918456,
#        'rxTime': 1640202239,
#        'hopLimit': 3,
#        'priority': 'RELIABLE',
#        'raw': 'faked',
#        'fromId': '!9388f81c',
#        'toId': '!9388f81c'
#    }
#
#    iface = MagicMock(autospec=SerialInterface)
#    with patch('meshtastic.serial_interface.SerialInterface', return_value=iface) as mo:
#        mo.localNode.getChannelByName.return_value = None
#        mo.myInfo.max_channels = 8
#        anode = Node(mo, 'bar', noProto=True)
#
#        radioConfig = RadioConfig()
#        anode.radioConfig = radioConfig
#
#        # Note: Have to do this next line because every call to MagicMock object/method returns a new magic mock
#        mo.localNode = anode
#
#        with caplog.at_level(logging.DEBUG):
#            anode.requestConfig()
#            anode.onResponseRequestChannel(packet1)
#            assert re.search(r'Received channel', caplog.text, re.MULTILINE)
#            anode.onResponseRequestChannel(packet2)
#            assert re.search(r'Received channel', caplog.text, re.MULTILINE)
#            assert re.search(r'Finished downloading channels', caplog.text, re.MULTILINE)
#            assert len(anode.channels) == 8
#            assert anode.channels[0].settings.modem_config == 3
#            assert anode.channels[1].settings.name == ''
#            assert anode.channels[2].settings.name == ''
#            assert anode.channels[3].settings.name == ''
#            assert anode.channels[4].settings.name == ''
#            assert anode.channels[5].settings.name == ''
#            assert anode.channels[6].settings.name == ''
#            assert anode.channels[7].settings.name == ''


# TODO
# @pytest.mark.unit
# def test_onResponseRequestSetting(caplog):
#    """Test onResponseRequestSetting()"""
#    # Note: Split out the get_radio_response to a MagicMock
#    # so it could be "returned" (not really sure how to do that
#    # in a python dict.
#    amsg = MagicMock(autospec=AdminMessage)
#    amsg.get_radio_response = """{
#  preferences {
#    phone_timeout_secs: 900
#    ls_secs: 300
#    position_broadcast_smart: true
#    position_flags: 35
#  }
# }"""
#    packet = {
#        'from': 2475227164,
#        'to': 2475227164,
#        'decoded': {
#            'portnum': 'ADMIN_APP',
#            'payload': b'*\x0e\n\x0c0\x84\x07P\xac\x02\x88\x01\x01\xb0\t#',
#            'requestId': 3145147848,
#            'admin': {
#                'getRadioResponse': {
#                    'preferences': {
#                        'phoneTimeoutSecs': 900,
#                        'lsSecs': 300,
#                        'positionBroadcastSmart': True,
#                        'positionFlags': 35
#                     }
#                },
#                'raw': amsg
#            },
#            'id': 365963704,
#            'rxTime': 1640195197,
#            'hopLimit': 3,
#            'priority': 'RELIABLE',
#            'raw': 'faked',
#            'fromId': '!9388f81c',
#            'toId': '!9388f81c'
#        }
#    }
#    iface = MagicMock(autospec=SerialInterface)
#    with patch('meshtastic.serial_interface.SerialInterface', return_value=iface) as mo:
#        mo.localNode.getChannelByName.return_value = None
#        mo.myInfo.max_channels = 8
#        anode = Node(mo, 'bar', noProto=True)
#
#        radioConfig = RadioConfig()
#        anode.radioConfig = radioConfig
#
#        # Note: Have to do this next line because every call to MagicMock object/method returns a new magic mock
#        mo.localNode = anode
#
#        with caplog.at_level(logging.DEBUG):
#            anode.onResponseRequestSettings(packet)
#            assert re.search(r'Received radio config, now fetching channels..', caplog.text, re.MULTILINE)


# TODO
# @pytest.mark.unit
# def test_onResponseRequestSetting_with_error(capsys):
#    """Test onResponseRequestSetting() with an error"""
#    packet = {
#        'from': 2475227164,
#        'to': 2475227164,
#        'decoded': {
#            'portnum': 'ADMIN_APP',
#            'payload': b'*\x0e\n\x0c0\x84\x07P\xac\x02\x88\x01\x01\xb0\t#',
#            'requestId': 3145147848,
#            'routing': {
#                'errorReason': 'some made up error',
#            },
#            'admin': {
#                'getRadioResponse': {
#                    'preferences': {
#                        'phoneTimeoutSecs': 900,
#                        'lsSecs': 300,
#                        'positionBroadcastSmart': True,
#                        'positionFlags': 35
#                     }
#                },
#            },
#            'id': 365963704,
#            'rxTime': 1640195197,
#            'hopLimit': 3,
#            'priority': 'RELIABLE',
#            'fromId': '!9388f81c',
#            'toId': '!9388f81c'
#        }
#    }
#    iface = MagicMock(autospec=SerialInterface)
#    with patch('meshtastic.serial_interface.SerialInterface', return_value=iface) as mo:
#        mo.localNode.getChannelByName.return_value = None
#        mo.myInfo.max_channels = 8
#        anode = Node(mo, 'bar', noProto=True)
#
#        radioConfig = RadioConfig()
#        anode.radioConfig = radioConfig
#
#        # Note: Have to do this next line because every call to MagicMock object/method returns a new magic mock
#        mo.localNode = anode
#
#        anode.onResponseRequestSettings(packet)
#        out, err = capsys.readouterr()
#        assert re.search(r'Error on response', out)
#        assert err == ''


@pytest.mark.unit
@pytest.mark.parametrize("favorite", ["!1dec0ded", 502009325])
def test_set_favorite(favorite):
    """
    Verify setFavorite sends an admin message marking the given node as a favorite and transmits it.

    Parameters
    ----------
        favorite (str | int): Node ID to mark as favorite.

    """
    iface = MagicMock(autospec=SerialInterface)
    node = Node(iface, 12345678)
    amesg = admin_pb2.AdminMessage()
    with patch("meshtastic.admin_pb2.AdminMessage", return_value=amesg):
        node.setFavorite(favorite)
    assert amesg.set_favorite_node == 502009325
    iface.sendData.assert_called_once()


@pytest.mark.unit
@pytest.mark.parametrize("favorite", ["!1dec0ded", 502009325])
def test_remove_favorite(favorite):
    """
    Verify that removing a favorite node creates an AdminMessage with the expected node ID and sends it via the interface.

    Parameters
    ----------
        favorite (str | int): Identifier of the favorite node to remove; used to populate the admin message sent to the interface.

    """
    iface = MagicMock(autospec=SerialInterface)
    node = Node(iface, 12345678)
    amesg = admin_pb2.AdminMessage()
    with patch("meshtastic.admin_pb2.AdminMessage", return_value=amesg):
        node.removeFavorite(favorite)

    assert amesg.remove_favorite_node == 502009325
    iface.sendData.assert_called_once()


@pytest.mark.unit
@pytest.mark.parametrize("ignored", ["!1dec0ded", 502009325])
def test_set_ignored(ignored):
    """
    Verify that Node.setIgnored constructs an AdminMessage marking the given node ID as ignored and sends it.

    Parameters
    ----------
        ignored (str | int): Node identifier passed to setIgnored.

    """
    iface = MagicMock(autospec=SerialInterface)
    node = Node(iface, 12345678)
    amesg = admin_pb2.AdminMessage()
    with patch("meshtastic.admin_pb2.AdminMessage", return_value=amesg):
        node.setIgnored(ignored)
    assert amesg.set_ignored_node == 502009325
    iface.sendData.assert_called_once()


@pytest.mark.unit
@pytest.mark.parametrize("ignored", ["!1dec0ded", 502009325])
def test_remove_ignored(ignored):
    """
    Verify that calling removeIgnored sends an admin message to remove a node from the ignored list and transmits it.

    Parameters
    ----------
        ignored: Node identifier (e.g., node ID or address) that will be encoded into `remove_ignored_node` on the AdminMessage.

    """
    iface = MagicMock(autospec=SerialInterface)
    node = Node(iface, 12345678)
    amesg = admin_pb2.AdminMessage()
    with patch("meshtastic.admin_pb2.AdminMessage", return_value=amesg):
        node.removeIgnored(ignored)

    assert amesg.remove_ignored_node == 502009325
    iface.sendData.assert_called_once()


@pytest.mark.unit
def test_setOwner_whitespace_only_long_name():
    """Test setOwner with whitespace-only long name."""
    iface = MagicMock(autospec=MeshInterface)
    anode = Node(iface, 123, noProto=True)

    with pytest.raises(
        MeshInterface.MeshInterfaceError,
        match="Long Name cannot be empty or contain only whitespace characters",
    ):
        anode.setOwner(long_name="   ")


@pytest.mark.unit
def test_setOwner_empty_long_name():
    """Test setOwner with empty long name."""
    iface = MagicMock(autospec=MeshInterface)
    anode = Node(iface, 123, noProto=True)

    with pytest.raises(
        MeshInterface.MeshInterfaceError,
        match="Long Name cannot be empty or contain only whitespace characters",
    ):
        anode.setOwner(long_name="")


@pytest.mark.unit
def test_setOwner_whitespace_only_short_name():
    """Test setOwner with whitespace-only short name."""
    iface = MagicMock(autospec=MeshInterface)
    anode = Node(iface, 123, noProto=True)

    with pytest.raises(
        MeshInterface.MeshInterfaceError,
        match="Short Name cannot be empty or contain only whitespace characters",
    ):
        anode.setOwner(short_name="   ")


@pytest.mark.unit
def test_setOwner_empty_short_name():
    """Test setOwner with empty short name."""
    iface = MagicMock(autospec=MeshInterface)
    anode = Node(iface, 123, noProto=True)

    with pytest.raises(
        MeshInterface.MeshInterfaceError,
        match="Short Name cannot be empty or contain only whitespace characters",
    ):
        anode.setOwner(short_name="")


@pytest.mark.unit
def test_setOwner_valid_names(caplog):
    """Test setOwner with valid names."""
    iface = MagicMock(autospec=MeshInterface)
    anode = Node(iface, 123, noProto=True)

    with caplog.at_level(logging.DEBUG):
        anode.setOwner(long_name="ValidName", short_name="VN")

    # Should not raise any exceptions
    # Note: When noProto=True, _sendAdmin is not called as the method returns early
    assert re.search(r"p\.set_owner\.long_name:ValidName:", caplog.text, re.MULTILINE)
    assert re.search(r"p\.set_owner\.short_name:VN:", caplog.text, re.MULTILINE)


@pytest.mark.unit
def test_setOwner_short_name_only(caplog):
    """Test setOwner with only short name provided."""
    iface = MagicMock(autospec=MeshInterface)
    anode = Node(iface, 123, noProto=True)

    with caplog.at_level(logging.DEBUG):
        anode.setOwner(short_name="TST")

    assert re.search(r"p\.set_owner\.short_name:TST:", caplog.text, re.MULTILINE)


@pytest.mark.unit
def test_setOwner_long_name_truncates_short_name(caplog):
    """Test setOwner truncates long short names to 4 characters."""
    iface = MagicMock(autospec=MeshInterface)
    anode = Node(iface, 123, noProto=True)

    with caplog.at_level(logging.DEBUG):
        anode.setOwner(long_name="TestUser", short_name="TOOLONG")

    # Short name should be truncated to 4 chars
    assert re.search(r"p\.set_owner\.short_name:TOOL:", caplog.text, re.MULTILINE)


@pytest.mark.unit
def test_setOwner_with_is_licensed(caplog):
    """Test setOwner sets is_licensed flag when long_name is provided."""
    iface = MagicMock(autospec=MeshInterface)
    anode = Node(iface, 123, noProto=True)

    with caplog.at_level(logging.DEBUG):
        anode.setOwner(long_name="LicensedUser", is_licensed=True)

    assert re.search(r"p\.set_owner\.is_licensed:True", caplog.text, re.MULTILINE)


@pytest.mark.unit
def test_setOwner_with_is_unmessagable(caplog):
    """Test setOwner sets is_unmessagable flag."""
    iface = MagicMock(autospec=MeshInterface)
    anode = Node(iface, 123, noProto=True)

    with caplog.at_level(logging.DEBUG):
        anode.setOwner(long_name="TestUser", is_unmessagable=True)

    assert re.search(r"p\.set_owner\.is_unmessagable:True:", caplog.text, re.MULTILINE)


@pytest.mark.unit
def test_waitForConfig_timeout():
    """Test waitForConfig returns False on timeout."""
    iface = MagicMock(autospec=MeshInterface)
    anode = Node(iface, 123, noProto=True)
    anode._timeout = Timeout(maxSecs=0.01)

    result = anode.waitForConfig()
    assert result is False


@pytest.mark.unit
def test_waitForConfig_success():
    """Test waitForConfig returns True when config is available."""
    iface = MagicMock(autospec=MeshInterface)
    anode = Node(iface, 123, noProto=True)

    # Set up the config to be "available"
    anode.localConfig = localonly_pb2.LocalConfig()

    # Mock the timeout to return True
    anode._timeout = MagicMock()
    anode._timeout.waitForSet.return_value = True

    result = anode.waitForConfig()
    assert result is True
