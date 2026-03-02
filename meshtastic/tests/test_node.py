"""Meshtastic unit tests for node.py."""
# pylint: disable=C0302

import base64
import logging
import re
from collections.abc import Callable
from typing import Any, Protocol, cast
from unittest.mock import MagicMock, patch

import pytest
from pytest import CaptureFixture, LogCaptureFixture

from ..mesh_interface import MeshInterface
from ..node import Node
from ..protobuf import admin_pb2, apponly_pb2, config_pb2, localonly_pb2, mesh_pb2
from ..protobuf.channel_pb2 import Channel  # pylint: disable=E0611
from ..serial_interface import SerialInterface
from ..util import Acknowledgment


class _FakeSendAdminProtocol(Protocol):
    """Callable protocol for fake _send_admin helpers with optional parameters."""

    def __call__(
        self,
        msg: admin_pb2.AdminMessage,
        wantResponse: bool = False,
        onResponse: Callable[[dict[str, Any]], Any] | None = None,
        adminIndex: int = 0,
    ) -> mesh_pb2.MeshPacket | None: ...


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
def test_node(capsys: CaptureFixture[str], mock_serial_interface: MagicMock) -> None:
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
def test_get_canned_message_returns_cached_value(
    mock_serial_interface: MagicMock,
) -> None:
    """get_canned_message should return the cached message without sending."""
    anode = Node(mock_serial_interface, "!12345678", noProto=True)
    anode.cannedPluginMessage = "cached message"

    send_admin = MagicMock()
    anode._send_admin = send_admin  # type: ignore[method-assign]

    assert anode.get_canned_message() == "cached message"
    send_admin.assert_not_called()


@pytest.mark.unit
def test_get_canned_message_requests_and_caches_value(
    mock_serial_interface: MagicMock,
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
    mock_serial_interface: MagicMock,
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
    on_response = cast(Callable[[dict[str, Any]], Any], captured["onResponse"])
    assert callable(on_response)
    acknowledgment = Acknowledgment()
    anode.iface._acknowledgment = acknowledgment
    anode.iface.localNode.nodeNum = 999
    on_response({"decoded": {"routing": {"errorReason": "NONE"}}, "from": 123})
    assert acknowledgment.receivedAck is True
    assert captured["adminIndex"] == 0
    assert anode.cannedPluginMessage is None
    assert anode.cannedPluginMessageMessages is None


@pytest.mark.unit
def test_set_canned_message_over_limit_raises(mock_serial_interface: MagicMock) -> None:
    """set_canned_message should reject messages longer than 200 chars."""
    anode = Node(mock_serial_interface, "!12345678", noProto=True)
    with pytest.raises(
        MeshInterface.MeshInterfaceError,
        match="The canned message must be 200 characters or fewer",
    ):
        anode.set_canned_message("a" * 201)


@pytest.mark.unit
def test_exitSimulator(caplog: LogCaptureFixture) -> None:
    """Verify that calling exitSimulator logs an indicative debug message.

    Asserts that a DEBUG-level log record contains the text "in exitSimulator".

    """
    with MeshInterface(noProto=True) as interface:
        interface.nodesByNum = {}
        anode = Node(interface, "!ba400000", noProto=True)
        with caplog.at_level(logging.DEBUG):
            anode.exitSimulator()
    assert re.search(r"in exitSimulator", caplog.text, re.MULTILINE)


@pytest.mark.unit
def test_reboot(caplog: LogCaptureFixture) -> None:
    """Test reboot."""
    with MeshInterface(noProto=True) as interface:
        interface.nodesByNum = {}
        anode = Node(interface, 1234567890, noProto=True)
        with caplog.at_level(logging.DEBUG):
            anode.reboot()
    assert re.search(r"Telling node to reboot", caplog.text, re.MULTILINE)


@pytest.mark.unit
def test_shutdown(caplog: LogCaptureFixture) -> None:
    """Test shutdown."""
    with MeshInterface(noProto=True) as interface:
        interface.nodesByNum = {}
        anode = Node(interface, 1234567890, noProto=True)
        with caplog.at_level(logging.DEBUG):
            anode.shutdown()
    assert re.search(r"Telling node to shutdown", caplog.text, re.MULTILINE)


@pytest.mark.unit
def test_setURL_raises_when_channels_not_loaded(
    autospec_local_node_iface: Callable[[type[Any]], MagicMock],
) -> None:
    """Test setURL raises when config/channels are not loaded."""
    anode = Node(autospec_local_node_iface(MeshInterface), "!12345678", noProto=True)
    with pytest.raises(
        MeshInterface.MeshInterfaceError, match="Config or channels not loaded"
    ):
        anode.setURL("")


@pytest.mark.unit
def test_setURL_valid_URL_but_no_settings(
    autospec_local_node_iface: Callable[[type[Any]], MagicMock],
) -> None:
    """Test setURL."""
    iface = autospec_local_node_iface(SerialInterface)
    url = "https://www.meshtastic.org/d/#"
    anode = Node(iface, "!12345678", noProto=True)
    with pytest.raises(
        MeshInterface.MeshInterfaceError, match="Config or channels not loaded"
    ):
        anode.setURL(url)


@pytest.mark.unit
def test_setURL_ignores_channels_over_device_limit(
    caplog: LogCaptureFixture,
    autospec_local_node_iface: Callable[[type[Any]], MagicMock],
) -> None:
    """Test that setURL ignores channels beyond the fixed device channel limit."""
    iface = autospec_local_node_iface(MeshInterface)
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


def _configure_immediate_admin_timeout(anode: Node) -> None:
    """Configure admin timeout mocks so wait-based admin reads fail immediately."""
    anode.module_available = MagicMock(return_value=True)  # type: ignore[method-assign]
    timeout_mock = MagicMock()
    timeout_mock.waitForSet.return_value = False
    timeout_mock.expireTimeout = 0
    anode._timeout = timeout_mock
    anode._send_admin = MagicMock()  # type: ignore[method-assign]


@pytest.mark.unit
def test_get_ringtone_times_out_without_response(
    caplog: LogCaptureFixture,
    autospec_local_node_iface: Callable[[type[Any]], MagicMock],
) -> None:
    """Verify get_ringtone times out when no response callback is invoked."""
    anode = Node(autospec_local_node_iface(MeshInterface), "!12345678", noProto=True)
    _configure_immediate_admin_timeout(anode)

    with caplog.at_level(logging.WARNING):
        result = anode.get_ringtone()

    assert result is None
    assert re.search(
        r"Timed out waiting for ringtone response", caplog.text, re.MULTILINE
    )


@pytest.mark.unit
def test_get_canned_message_times_out_without_response(
    caplog: LogCaptureFixture,
    autospec_local_node_iface: Callable[[type[Any]], MagicMock],
) -> None:
    """Test get_canned_message returns None if the response callback is never invoked."""
    anode = Node(autospec_local_node_iface(MeshInterface), "!12345678", noProto=True)
    _configure_immediate_admin_timeout(anode)

    with caplog.at_level(logging.WARNING):
        result = anode.get_canned_message()

    assert result is None
    assert re.search(
        r"Timed out waiting for canned message response", caplog.text, re.MULTILINE
    )


@pytest.mark.unit
def test_getChannelByChannelIndex(
    autospec_local_node_iface: Callable[[type[Any]], MagicMock],
) -> None:
    """Test getChannelByChannelIndex()."""
    anode = Node(autospec_local_node_iface(MeshInterface), "!12345678", noProto=True)

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
def test_writeConfig_with_no_radioConfig(
    autospec_local_node_iface: Callable[[type[Any]], MagicMock],
) -> None:
    """Test writeConfig raises MeshInterfaceError for invalid config name."""
    anode = Node(autospec_local_node_iface(MeshInterface), "!12345678", noProto=True)

    with pytest.raises(
        MeshInterface.MeshInterfaceError,
        match="Error: No valid config with name foo",
    ):
        anode.writeConfig("foo")


@pytest.mark.unit
def test_writeChannel_with_no_channels_raises_mesh_error(
    autospec_local_node_iface: Callable[[type[Any]], MagicMock],
) -> None:
    """Test writeChannel raises when channels have not been loaded."""
    anode = Node(autospec_local_node_iface(MeshInterface), "!12345678", noProto=True)
    anode.channels = None

    with pytest.raises(
        MeshInterface.MeshInterfaceError, match="Error: No channels have been read"
    ):
        anode.writeChannel(0)

@pytest.mark.unit
def test_writeConfig_traffic_management(
    autospec_local_node_iface: Callable[[type[Any]], MagicMock],
) -> None:
    """Test writeConfig writes traffic_management module config through set_module_config."""
    iface = autospec_local_node_iface(MeshInterface)
    anode = Node(iface, "!12345678", noProto=True)
    anode.moduleConfig.traffic_management.enabled = True
    anode.moduleConfig.traffic_management.rate_limit_enabled = True

    sent_messages: list[admin_pb2.AdminMessage] = []
    anode._send_admin = _make_fake_send_admin(  # type: ignore[method-assign,assignment]
        sent_messages=sent_messages
    )

    anode.writeConfig("traffic_management")

    assert len(sent_messages) == 1
    sent_message = sent_messages[0]
    assert sent_message.HasField("set_module_config")
    assert sent_message.set_module_config.HasField("traffic_management")
    assert sent_message.set_module_config.traffic_management.enabled is True
    assert (
        sent_message.set_module_config.traffic_management.rate_limit_enabled is True
    )


@pytest.mark.unit
def test_requestChannel_not_localNode(
    caplog: LogCaptureFixture, mock_serial_interface: MagicMock
) -> None:
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
def test_requestChannel_localNode(
    caplog: LogCaptureFixture, mock_serial_interface: MagicMock
) -> None:
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
def test_requestChannels_non_localNode(
    caplog: LogCaptureFixture, mock_serial_interface: MagicMock
) -> None:
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
def test_requestChannels_non_localNode_starting_index(
    caplog: LogCaptureFixture, mock_serial_interface: MagicMock
) -> None:
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
def test_set_favorite(
    favorite: str | int,
    autospec_local_node_iface: Callable[[type[Any]], MagicMock],
) -> None:
    """Verify setFavorite sends an admin message marking the given node as a favorite and transmits it.

    Parameters
    ----------
    favorite : str | int
        Node ID to mark as favorite.
    """
    iface = autospec_local_node_iface(SerialInterface)
    node = Node(iface, 12345678)
    amesg = admin_pb2.AdminMessage()
    with patch("meshtastic.node.admin_pb2.AdminMessage", return_value=amesg):
        node.setFavorite(favorite)
    assert amesg.set_favorite_node == 502009325
    iface.sendData.assert_called_once()


@pytest.mark.unit
@pytest.mark.parametrize("favorite", ["!1dec0ded", 502009325])
def test_remove_favorite(
    favorite: str | int,
    autospec_local_node_iface: Callable[[type[Any]], MagicMock],
) -> None:
    """Verify that removing a favorite node creates an AdminMessage with the expected node ID and sends it via the interface.

    Parameters
    ----------
    favorite : str | int
        Identifier of the favorite node to remove; used to populate the admin message sent to the interface.
    """
    iface = autospec_local_node_iface(SerialInterface)
    node = Node(iface, 12345678)
    amesg = admin_pb2.AdminMessage()
    with patch("meshtastic.node.admin_pb2.AdminMessage", return_value=amesg):
        node.removeFavorite(favorite)

    assert amesg.remove_favorite_node == 502009325
    iface.sendData.assert_called_once()


@pytest.mark.unit
@pytest.mark.parametrize("ignored", ["!1dec0ded", 502009325])
def test_set_ignored(
    ignored: str | int,
    autospec_local_node_iface: Callable[[type[Any]], MagicMock],
) -> None:
    """Verify that Node.setIgnored constructs an AdminMessage marking the given node ID as ignored and sends it.

    Parameters
    ----------
    ignored : str | int
        Node identifier passed to setIgnored.
    """
    iface = autospec_local_node_iface(SerialInterface)
    node = Node(iface, 12345678)
    amesg = admin_pb2.AdminMessage()
    with patch("meshtastic.node.admin_pb2.AdminMessage", return_value=amesg):
        node.setIgnored(ignored)
    assert amesg.set_ignored_node == 502009325
    iface.sendData.assert_called_once()


@pytest.mark.unit
@pytest.mark.parametrize("ignored", ["!1dec0ded", 502009325])
def test_remove_ignored(
    ignored: str | int,
    autospec_local_node_iface: Callable[[type[Any]], MagicMock],
) -> None:
    """Verify that calling removeIgnored sends an admin message to remove a node from the ignored list and transmits it.

    Parameters
    ----------
    ignored : str | int
        Node identifier (e.g., node ID or address) that will be encoded into `remove_ignored_node` on the AdminMessage.
    """
    iface = autospec_local_node_iface(SerialInterface)
    node = Node(iface, 12345678)
    amesg = admin_pb2.AdminMessage()
    with patch("meshtastic.node.admin_pb2.AdminMessage", return_value=amesg):
        node.removeIgnored(ignored)

    assert amesg.remove_ignored_node == 502009325
    iface.sendData.assert_called_once()


@pytest.mark.unit
@pytest.mark.parametrize(
    ("param_name", "value", "expected_error"),
    [
        (
            "long_name",
            "   ",
            "Long Name cannot be empty or contain only whitespace characters",
        ),
        (
            "long_name",
            "",
            "Long Name cannot be empty or contain only whitespace characters",
        ),
        (
            "short_name",
            "   ",
            "Short Name cannot be empty or contain only whitespace characters",
        ),
        (
            "short_name",
            "",
            "Short Name cannot be empty or contain only whitespace characters",
        ),
    ],
)
def test_setOwner_rejects_empty_or_whitespace_names(
    autospec_local_node_iface: Callable[[type[Any]], MagicMock],
    param_name: str,
    value: str,
    expected_error: str,
) -> None:
    """Test setOwner rejects empty or whitespace-only names."""
    iface = autospec_local_node_iface(MeshInterface)
    anode = Node(iface, 123, noProto=True)

    with pytest.raises(MeshInterface.MeshInterfaceError, match=expected_error):
        anode.setOwner(**{param_name: value})  # type: ignore[arg-type]


@pytest.mark.unit
@pytest.mark.parametrize(
    ("owner_kwargs", "expected_patterns"),
    [
        (
            {"long_name": "ValidName", "short_name": "VN"},
            (
                r"p\.set_owner\.long_name:ValidName:",
                r"p\.set_owner\.short_name:VN:",
            ),
        ),
        (
            {"short_name": "TST"},
            (r"p\.set_owner\.short_name:TST:",),
        ),
        (
            {"long_name": "TestUser", "short_name": "TOOLONG"},
            (r"p\.set_owner\.short_name:TOOL:",),
        ),
        (
            {"long_name": "LicensedUser", "is_licensed": True},
            (r"p\.set_owner\.is_licensed:True:",),
        ),
        (
            {"long_name": "TestUser", "is_unmessagable": True},
            (r"p\.set_owner\.is_unmessagable:True:",),
        ),
    ],
)
def test_setOwner_logs_expected_fields_for_variants(
    caplog: LogCaptureFixture,
    autospec_local_node_iface: Callable[[type[Any]], MagicMock],
    owner_kwargs: dict[str, Any],
    expected_patterns: tuple[str, ...],
) -> None:
    """Test setOwner variants log the expected fields."""
    iface = autospec_local_node_iface(MeshInterface)
    anode = Node(iface, 123, noProto=True)

    with caplog.at_level(logging.DEBUG):
        anode.setOwner(**owner_kwargs)

    for pattern in expected_patterns:
        assert re.search(pattern, caplog.text, re.MULTILINE)


@pytest.mark.unit
def test_waitForConfig_timeout(
    autospec_local_node_iface: Callable[[type[Any]], MagicMock],
) -> None:
    """Test waitForConfig returns False on timeout."""
    iface = autospec_local_node_iface(MeshInterface)
    anode = Node(iface, 123, noProto=True)
    # Mock timeout to simulate immediate timeout (waitForSet returns False)
    anode._timeout = MagicMock()
    anode._timeout.waitForSet.return_value = False

    result = anode.waitForConfig()
    assert result is False


@pytest.mark.unit
def test_waitForConfig_success(
    autospec_local_node_iface: Callable[[type[Any]], MagicMock],
) -> None:
    """Test waitForConfig returns True when config is available."""
    iface = autospec_local_node_iface(MeshInterface)
    anode = Node(iface, 123, noProto=True)

    # Set up the config to be "available"
    anode.localConfig = localonly_pb2.LocalConfig()

    # Mock the timeout to return True
    anode._timeout = MagicMock()
    anode._timeout.waitForSet.return_value = True

    result = anode.waitForConfig()
    assert result is True

@pytest.mark.unit
def test_start_ota_local_node() -> None:
    """Test startOTA on local node."""
    iface = MagicMock(autospec=MeshInterface)
    anode = Node(iface, 1234567890, noProto=True)
    iface.localNode = anode

    captured: dict[str, object] = {}
    anode._send_admin = _make_fake_send_admin(  # type: ignore[method-assign,assignment]
        captured=captured
    )

    test_hash = b"\x01\x02\x03" * 8  # 24-byte hash
    anode.startOTA(mode=admin_pb2.OTAMode.OTA_WIFI, hash=test_hash)

    sent_msg = cast(admin_pb2.AdminMessage, captured["msg"])
    assert sent_msg.ota_request.reboot_ota_mode == admin_pb2.OTAMode.OTA_WIFI
    assert sent_msg.ota_request.ota_hash == test_hash


@pytest.mark.unit
def test_start_ota_remote_node_raises_error() -> None:
    """Test startOTA on remote node raises MeshInterfaceError."""
    iface = MagicMock(autospec=MeshInterface)
    local_node = Node(iface, 1234567890, noProto=True)
    remote_node = Node(iface, 9876543210, noProto=True)
    iface.localNode = local_node

    test_hash = b"\x01\x02\x03" * 8
    with pytest.raises(
        MeshInterface.MeshInterfaceError, match="startOTA only possible on local node"
    ):
        remote_node.startOTA(mode=admin_pb2.OTAMode.OTA_WIFI, hash=test_hash)
