"""Meshtastic unit tests for node.py."""

import base64
import logging
import re
from collections.abc import Callable
from typing import Any, Protocol, cast
from unittest.mock import MagicMock, create_autospec, patch

import pytest

from ..mesh_interface import MeshInterface
from ..node import Node
from ..protobuf import admin_pb2, apponly_pb2, config_pb2, localonly_pb2, mesh_pb2
from ..protobuf.channel_pb2 import Channel  # pylint: disable=E0611
from ..serial_interface import SerialInterface
from ..util import Timeout


class _FakeSendAdminProtocol(Protocol):
    """Callable protocol for fake _send_admin helpers with optional parameters."""

    def __call__(
        self,
        msg: admin_pb2.AdminMessage,
        wantResponse: bool = False,
        onResponse: Callable[[dict[str, Any]], Any] | None = None,
        adminIndex: int = 0,
    ) -> mesh_pb2.MeshPacket | None: ...


def _autospec_with_local_node(spec_class: type[Any]) -> Any:
    """Create an autospecced interface mock with a localNode attribute."""
    iface = create_autospec(spec_class, instance=True)
    local_node = MagicMock(spec=["_get_admin_channel_index"])
    local_node._get_admin_channel_index.return_value = 0
    iface.localNode = local_node
    return iface


def _make_fake_send_admin(
    *,
    sent_messages: list[admin_pb2.AdminMessage] | None = None,
    captured: dict[str, object] | None = None,
    expected_want_response: bool | None = None,
    response_payload: dict[str, Any] | None = None,
    return_packet: mesh_pb2.MeshPacket | None = None,
) -> _FakeSendAdminProtocol:
    """Create a configurable fake for Node._send_admin used by canned-message tests."""

    def _fake_send_admin(
        msg: admin_pb2.AdminMessage,
        wantResponse: bool = False,
        onResponse: Callable[[dict[str, Any]], Any] | None = None,
        adminIndex: int = 0,
    ) -> mesh_pb2.MeshPacket | None:
        _ = adminIndex
        if sent_messages is not None:
            sent_messages.append(msg)
        if captured is not None:
            captured["msg"] = msg
            captured["wantResponse"] = wantResponse
            captured["onResponse"] = onResponse
            captured["adminIndex"] = adminIndex
        if expected_want_response is not None:
            assert wantResponse is expected_want_response
        if response_payload is not None:
            assert onResponse is not None
            onResponse(response_payload)
        return return_packet

    return _fake_send_admin


@pytest.mark.unit
def test_node(capsys, mock_serial_interface):
    """Test that we can instantiate a Node."""
    anode = Node(mock_serial_interface, "!12345678", noProto=True)
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


@pytest.mark.unit
def test_get_canned_message_returns_cached_value(mock_serial_interface: Any) -> None:
    """get_canned_message should return the cached message without sending."""
    anode = Node(mock_serial_interface, "!12345678", noProto=True)
    anode.cannedPluginMessage = "cached message"

    send_admin = MagicMock()
    anode._send_admin = send_admin  # type: ignore[method-assign]

    assert anode.get_canned_message() == "cached message"
    send_admin.assert_not_called()


@pytest.mark.unit
def test_get_canned_message_requests_and_caches_value(
    mock_serial_interface: Any,
) -> None:
    """get_canned_message should request, cache, and return the response payload."""
    anode = Node(mock_serial_interface, "!12345678", noProto=True)
    response_raw = admin_pb2.AdminMessage()
    response_raw.get_canned_message_module_messages_response = "hello world"
    sent_messages: list[admin_pb2.AdminMessage] = []
    request_packet = mesh_pb2.MeshPacket()
    response_payload: dict[str, Any] = {"decoded": {"admin": {"raw": response_raw}}}
    fake_send_admin = _make_fake_send_admin(
        sent_messages=sent_messages,
        expected_want_response=True,
        response_payload=response_payload,
        return_packet=request_packet,
    )
    anode._send_admin = fake_send_admin  # type: ignore[method-assign,assignment]

    assert anode.get_canned_message() == "hello world"
    assert anode.cannedPluginMessage == "hello world"
    assert len(sent_messages) == 1
    assert sent_messages[0].get_canned_message_module_messages_request is True

    # A second call should use cache and avoid another request.
    assert anode.get_canned_message() == "hello world"
    assert len(sent_messages) == 1


@pytest.mark.unit
def test_set_canned_message_sends_payload_and_invalidates_cache(
    mock_serial_interface: Any,
) -> None:
    """set_canned_message should send payload and clear cached message values."""
    anode = Node(mock_serial_interface, "!12345678", noProto=True)
    anode.cannedPluginMessage = "stale"
    anode.cannedPluginMessageMessages = "stale-part"

    captured: dict[str, object] = {}
    sent_packet = mesh_pb2.MeshPacket()

    anode.ensureSessionKey = MagicMock()  # type: ignore[method-assign]
    fake_send_admin = _make_fake_send_admin(
        captured=captured,
        expected_want_response=False,
        return_packet=sent_packet,
    )
    anode._send_admin = fake_send_admin  # type: ignore[method-assign,assignment]

    result = anode.set_canned_message("fresh")

    assert result is sent_packet
    sent_msg = cast(admin_pb2.AdminMessage, captured["msg"])
    assert sent_msg.set_canned_message_module_messages == "fresh"
    assert captured["wantResponse"] is False
    on_response = captured["onResponse"]
    assert callable(on_response)
    assert getattr(on_response, "__self__", None) is anode
    assert getattr(on_response, "__func__", None) is Node.onAckNak
    assert captured["adminIndex"] == 0
    assert anode.cannedPluginMessage is None
    assert anode.cannedPluginMessageMessages is None


@pytest.mark.unit
def test_set_canned_message_over_limit_raises(mock_serial_interface):
    """set_canned_message should reject messages longer than 200 chars."""
    anode = Node(mock_serial_interface, "!12345678", noProto=True)
    with pytest.raises(
        MeshInterface.MeshInterfaceError,
        match="The canned message must be 200 characters or fewer",
    ):
        anode.set_canned_message("a" * 201)


@pytest.mark.unit
def test_exitSimulator(caplog):
    """Verify that calling exitSimulator logs an indicative debug message.

    Asserts that a DEBUG-level log record contains the text "in exitSimulator".

    """
    with MeshInterface() as interface:
        interface.nodesByNum = {}
        anode = Node(interface, "!ba400000", noProto=True)
        with caplog.at_level(logging.DEBUG):
            anode.exitSimulator()
    assert re.search(r"in exitSimulator", caplog.text, re.MULTILINE)


@pytest.mark.unit
def test_reboot(caplog):
    """Test reboot."""
    with MeshInterface() as interface:
        interface.nodesByNum = {}
        anode = Node(interface, 1234567890, noProto=True)
        with caplog.at_level(logging.DEBUG):
            anode.reboot()
    assert re.search(r"Telling node to reboot", caplog.text, re.MULTILINE)


@pytest.mark.unit
def test_shutdown(caplog):
    """Test shutdown."""
    with MeshInterface() as interface:
        interface.nodesByNum = {}
        anode = Node(interface, 1234567890, noProto=True)
        with caplog.at_level(logging.DEBUG):
            anode.shutdown()
    assert re.search(r"Telling node to shutdown", caplog.text, re.MULTILINE)


@pytest.mark.unit
def test_setURL_raises_when_channels_not_loaded() -> None:
    """Test setURL raises when config/channels are not loaded."""
    anode = Node(_autospec_with_local_node(MeshInterface), "!12345678", noProto=True)
    with pytest.raises(
        MeshInterface.MeshInterfaceError, match="Config or channels not loaded"
    ):
        anode.setURL("")


@pytest.mark.unit
def test_setURL_valid_URL_but_no_settings():
    """Test setURL."""
    iface = _autospec_with_local_node(SerialInterface)
    url = "https://www.meshtastic.org/d/#"
    anode = Node(iface, "!12345678", noProto=True)
    with pytest.raises(
        MeshInterface.MeshInterfaceError, match="Config or channels not loaded"
    ):
        anode.setURL(url)


@pytest.mark.unit
def test_setURL_ignores_channels_over_device_limit(caplog):
    """Test that setURL ignores channels beyond the fixed device channel limit."""
    iface = _autospec_with_local_node(MeshInterface)
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
    assert len(anode.channels) == 8
    assert anode.channels[0].settings.name == "ch0"
    assert anode.channels[7].settings.name == "ch7"


@pytest.mark.unit
def test_get_ringtone_times_out_without_response(caplog):
    """Verify get_ringtone times out when no response callback is invoked."""
    anode = Node(_autospec_with_local_node(MeshInterface), "!12345678", noProto=True)
    anode.module_available = MagicMock(return_value=True)  # type: ignore[method-assign]
    anode._timeout = Timeout(maxSecs=0.01)
    anode._timeout.sleepInterval = 0.001
    anode._send_admin = MagicMock()  # type: ignore[method-assign]

    with caplog.at_level(logging.WARNING):
        result = anode.get_ringtone()

    assert result is None
    assert re.search(
        r"Timed out waiting for ringtone response", caplog.text, re.MULTILINE
    )


@pytest.mark.unit
def test_get_canned_message_times_out_without_response(caplog):
    """Test get_canned_message returns None if the response callback is never invoked."""
    anode = Node(_autospec_with_local_node(MeshInterface), "!12345678", noProto=True)
    anode.module_available = MagicMock(return_value=True)  # type: ignore[method-assign]
    anode._timeout = Timeout(maxSecs=0.01)
    anode._timeout.sleepInterval = 0.001
    anode._send_admin = MagicMock()  # type: ignore[method-assign]

    with caplog.at_level(logging.WARNING):
        result = anode.get_canned_message()

    assert result is None
    assert re.search(
        r"Timed out waiting for canned message response", caplog.text, re.MULTILINE
    )


@pytest.mark.unit
def test_getChannelByChannelIndex():
    """Test getChannelByChannelIndex()."""
    anode = Node(_autospec_with_local_node(MeshInterface), "!12345678", noProto=True)

    channel1 = Channel(index=0, role=Channel.Role.PRIMARY)  # primary channel
    channel2 = Channel(index=1, role=Channel.Role.SECONDARY)  # secondary channel
    channel3 = Channel(index=2, role=Channel.Role.DISABLED)
    channel4 = Channel(index=3, role=Channel.Role.DISABLED)
    channel5 = Channel(index=4, role=Channel.Role.DISABLED)
    channel6 = Channel(index=5, role=Channel.Role.DISABLED)
    channel7 = Channel(index=6, role=Channel.Role.DISABLED)
    channel8 = Channel(index=7, role=Channel.Role.DISABLED)

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


@pytest.mark.unit
def test_writeConfig_with_no_radioConfig():
    """Test writeConfig raises MeshInterfaceError for invalid config name."""
    anode = Node(_autospec_with_local_node(MeshInterface), "!12345678", noProto=True)

    with pytest.raises(
        MeshInterface.MeshInterfaceError,
        match="Error: No valid config with name foo",
    ):
        anode.writeConfig("foo")


@pytest.mark.unit
def test_writeChannel_with_no_channels_raises_mesh_error():
    """Test writeChannel raises when channels have not been loaded."""
    anode = Node(_autospec_with_local_node(MeshInterface), "!12345678", noProto=True)
    anode.channels = None

    with pytest.raises(
        MeshInterface.MeshInterfaceError, match="Error: No channels have been read"
    ):
        anode.writeChannel(0)


@pytest.mark.unit
def test_requestChannel_not_localNode(caplog, mock_serial_interface):
    """Verify that requesting channel 0 on a non-local node logs a remote channel info request.

    Sets up a mocked SerialInterface and a Node that is not the local node, configures max channels,
    calls _request_channel(0), and asserts that an INFO log contains "Requesting channel 0 info".

    """
    iface = mock_serial_interface
    anode = Node(iface, "!12345678", noProto=True)
    with caplog.at_level(logging.INFO):
        anode._request_channel(0)
        assert re.search(
            r"Requesting channel 0 info from remote node", caplog.text, re.MULTILINE
        )


@pytest.mark.unit
def test_requestChannel_localNode(caplog, mock_serial_interface):
    """Verify that a local node logs a local channel request when _request_channel is called.

    Checks that the log contains "Requesting channel 0" and does not include "from remote node".

    """
    iface = mock_serial_interface
    anode = Node(iface, "!12345678", noProto=True)
    iface.localNode = anode

    with caplog.at_level(logging.DEBUG):
        anode._request_channel(0)
        assert re.search(r"Requesting channel 0", caplog.text, re.MULTILINE)
        assert not re.search(r"from remote node", caplog.text, re.MULTILINE)


@pytest.mark.unit
def test_requestChannels_non_localNode(caplog, mock_serial_interface):
    """Test requestChannels() with a starting index of 0."""
    iface = mock_serial_interface
    anode = Node(iface, "!12345678", noProto=True)
    # Set a sentinel value to verify it gets reset
    anode.partialChannels = [Channel()]
    with caplog.at_level(logging.DEBUG):
        anode.requestChannels(0)
        assert re.search(
            "Requesting channel 0 info from remote node", caplog.text, re.MULTILINE
        )
        assert not anode.partialChannels


@pytest.mark.unit
def test_requestChannels_non_localNode_starting_index(caplog, mock_serial_interface):
    """Test requestChannels() with a starting index of non-0."""
    iface = mock_serial_interface
    anode = Node(iface, "!12345678", noProto=True)
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


@pytest.mark.unit
@pytest.mark.parametrize("favorite", ["!1dec0ded", 502009325])
def test_set_favorite(favorite):
    """Verify setFavorite sends an admin message marking the given node as a favorite and transmits it.

    Parameters
    ----------
    favorite : str | int
        Node ID to mark as favorite.
    """
    iface = _autospec_with_local_node(SerialInterface)
    node = Node(iface, 12345678)
    amesg = admin_pb2.AdminMessage()
    with patch("meshtastic.node.admin_pb2.AdminMessage", return_value=amesg):
        node.setFavorite(favorite)
    assert amesg.set_favorite_node == 502009325
    iface.sendData.assert_called_once()


@pytest.mark.unit
@pytest.mark.parametrize("favorite", ["!1dec0ded", 502009325])
def test_remove_favorite(favorite):
    """Verify that removing a favorite node creates an AdminMessage with the expected node ID and sends it via the interface.

    Parameters
    ----------
    favorite : str | int
        Identifier of the favorite node to remove; used to populate the admin message sent to the interface.
    """
    iface = _autospec_with_local_node(SerialInterface)
    node = Node(iface, 12345678)
    amesg = admin_pb2.AdminMessage()
    with patch("meshtastic.node.admin_pb2.AdminMessage", return_value=amesg):
        node.removeFavorite(favorite)

    assert amesg.remove_favorite_node == 502009325
    iface.sendData.assert_called_once()


@pytest.mark.unit
@pytest.mark.parametrize("ignored", ["!1dec0ded", 502009325])
def test_set_ignored(ignored):
    """Verify that Node.setIgnored constructs an AdminMessage marking the given node ID as ignored and sends it.

    Parameters
    ----------
    ignored : str | int
        Node identifier passed to setIgnored.
    """
    iface = _autospec_with_local_node(SerialInterface)
    node = Node(iface, 12345678)
    amesg = admin_pb2.AdminMessage()
    with patch("meshtastic.node.admin_pb2.AdminMessage", return_value=amesg):
        node.setIgnored(ignored)
    assert amesg.set_ignored_node == 502009325
    iface.sendData.assert_called_once()


@pytest.mark.unit
@pytest.mark.parametrize("ignored", ["!1dec0ded", 502009325])
def test_remove_ignored(ignored):
    """Verify that calling removeIgnored sends an admin message to remove a node from the ignored list and transmits it.

    Parameters
    ----------
    ignored : str | int
        Node identifier (e.g., node ID or address) that will be encoded into `remove_ignored_node` on the AdminMessage.
    """
    iface = _autospec_with_local_node(SerialInterface)
    node = Node(iface, 12345678)
    amesg = admin_pb2.AdminMessage()
    with patch("meshtastic.node.admin_pb2.AdminMessage", return_value=amesg):
        node.removeIgnored(ignored)

    assert amesg.remove_ignored_node == 502009325
    iface.sendData.assert_called_once()


@pytest.mark.unit
def test_setOwner_whitespace_only_long_name():
    """Test setOwner with whitespace-only long name."""
    iface = _autospec_with_local_node(MeshInterface)
    anode = Node(iface, 123, noProto=True)

    with pytest.raises(
        MeshInterface.MeshInterfaceError,
        match="Long Name cannot be empty or contain only whitespace characters",
    ):
        anode.setOwner(long_name="   ")


@pytest.mark.unit
def test_setOwner_empty_long_name():
    """Test setOwner with empty long name."""
    iface = _autospec_with_local_node(MeshInterface)
    anode = Node(iface, 123, noProto=True)

    with pytest.raises(
        MeshInterface.MeshInterfaceError,
        match="Long Name cannot be empty or contain only whitespace characters",
    ):
        anode.setOwner(long_name="")


@pytest.mark.unit
def test_setOwner_whitespace_only_short_name():
    """Test setOwner with whitespace-only short name."""
    iface = _autospec_with_local_node(MeshInterface)
    anode = Node(iface, 123, noProto=True)

    with pytest.raises(
        MeshInterface.MeshInterfaceError,
        match="Short Name cannot be empty or contain only whitespace characters",
    ):
        anode.setOwner(short_name="   ")


@pytest.mark.unit
def test_setOwner_empty_short_name():
    """Test setOwner with empty short name."""
    iface = _autospec_with_local_node(MeshInterface)
    anode = Node(iface, 123, noProto=True)

    with pytest.raises(
        MeshInterface.MeshInterfaceError,
        match="Short Name cannot be empty or contain only whitespace characters",
    ):
        anode.setOwner(short_name="")


@pytest.mark.unit
def test_setOwner_valid_names(caplog):
    """Test setOwner with valid names."""
    iface = _autospec_with_local_node(MeshInterface)
    anode = Node(iface, 123, noProto=True)

    with caplog.at_level(logging.DEBUG):
        anode.setOwner(long_name="ValidName", short_name="VN")

    # Should not raise any exceptions
    # Note: When noProto=True, _send_admin is not called as the method returns early
    assert re.search(r"p\.set_owner\.long_name:ValidName:", caplog.text, re.MULTILINE)
    assert re.search(r"p\.set_owner\.short_name:VN:", caplog.text, re.MULTILINE)


@pytest.mark.unit
def test_setOwner_short_name_only(caplog):
    """Test setOwner with only short name provided."""
    iface = _autospec_with_local_node(MeshInterface)
    anode = Node(iface, 123, noProto=True)

    with caplog.at_level(logging.DEBUG):
        anode.setOwner(short_name="TST")

    assert re.search(r"p\.set_owner\.short_name:TST:", caplog.text, re.MULTILINE)


@pytest.mark.unit
def test_setOwner_long_name_truncates_short_name(caplog):
    """Test setOwner truncates long short names to 4 characters."""
    iface = _autospec_with_local_node(MeshInterface)
    anode = Node(iface, 123, noProto=True)

    with caplog.at_level(logging.DEBUG):
        anode.setOwner(long_name="TestUser", short_name="TOOLONG")

    # Short name should be truncated to 4 chars
    assert re.search(r"p\.set_owner\.short_name:TOOL:", caplog.text, re.MULTILINE)


@pytest.mark.unit
def test_setOwner_with_is_licensed(caplog):
    """Test setOwner sets is_licensed flag when long_name is provided."""
    iface = _autospec_with_local_node(MeshInterface)
    anode = Node(iface, 123, noProto=True)

    with caplog.at_level(logging.DEBUG):
        anode.setOwner(long_name="LicensedUser", is_licensed=True)

    assert re.search(r"p\.set_owner\.is_licensed:True:", caplog.text, re.MULTILINE)


@pytest.mark.unit
def test_setOwner_with_is_unmessagable(caplog):
    """Test setOwner sets is_unmessagable flag."""
    iface = _autospec_with_local_node(MeshInterface)
    anode = Node(iface, 123, noProto=True)

    with caplog.at_level(logging.DEBUG):
        anode.setOwner(long_name="TestUser", is_unmessagable=True)

    assert re.search(r"p\.set_owner\.is_unmessagable:True:", caplog.text, re.MULTILINE)


@pytest.mark.unit
def test_waitForConfig_timeout():
    """Test waitForConfig returns False on timeout."""
    iface = _autospec_with_local_node(MeshInterface)
    anode = Node(iface, 123, noProto=True)
    anode._timeout = Timeout(maxSecs=0.01)

    result = anode.waitForConfig()
    assert result is False


@pytest.mark.unit
def test_waitForConfig_success():
    """Test waitForConfig returns True when config is available."""
    iface = _autospec_with_local_node(MeshInterface)
    anode = Node(iface, 123, noProto=True)

    # Set up the config to be "available"
    anode.localConfig = localonly_pb2.LocalConfig()

    # Mock the timeout to return True
    anode._timeout = MagicMock()
    anode._timeout.waitForSet.return_value = True

    result = anode.waitForConfig()
    assert result is True
