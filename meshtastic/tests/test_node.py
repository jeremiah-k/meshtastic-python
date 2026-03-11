"""Meshtastic unit tests for node.py."""

# pylint: disable=C0302

import base64
import logging
import re
import threading
from collections.abc import Callable
from typing import Any, Literal, Protocol, cast
from unittest.mock import MagicMock, patch

import pytest
from pytest import CaptureFixture, LogCaptureFixture

from .. import node as node_module
from ..mesh_interface import MeshInterface
from ..node import MAX_CHANNELS, Node
from ..protobuf import (
    admin_pb2,
    apponly_pb2,
    config_pb2,
    localonly_pb2,
    mesh_pb2,
)
from ..protobuf.channel_pb2 import Channel  # pylint: disable=E0611
from ..serial_interface import SerialInterface
from ..util import Acknowledgment, fromPSK

CHANNEL_LIMIT = MAX_CHANNELS


class _FakeSendAdminProtocol(Protocol):
    """Callable protocol for fake _send_admin helpers with optional parameters."""

    def __call__(
        self,
        msg: admin_pb2.AdminMessage,
        wantResponse: bool = False,
        onResponse: Callable[[dict[str, Any]], Any] | None = None,
        adminIndex: int = 0,
    ) -> mesh_pb2.MeshPacket | None: ...


class _DropChannelsOnEnterCountLock:
    """Lock stub that clears ``node.channels`` on a specific acquisition count."""

    def __init__(self, node: Node, trigger_enter: int) -> None:
        self.node = node
        self.trigger_enter = trigger_enter
        self.enters = 0

    def __enter__(self) -> "_DropChannelsOnEnterCountLock":
        self.enters += 1
        if self.enters == self.trigger_enter:
            self.node.channels = None
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> Literal[False]:
        _ = (exc_type, exc, tb)
        return False


class _TrackingLock:
    """Lock stub that records how many times it was acquired."""

    def __init__(self) -> None:
        self.enter_count = 0

    def __enter__(self) -> "_TrackingLock":
        self.enter_count += 1
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> Literal[False]:
        _ = (exc_type, exc, tb)
        return False


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
def test_on_response_request_settings_copies_local_config_from_raw_response(
    mock_serial_interface: MagicMock,
    caplog: LogCaptureFixture,
) -> None:
    """OnResponseRequestSettings should copy recognized LocalConfig payloads from admin.raw."""
    anode = Node(mock_serial_interface, "!12345678", noProto=True)
    anode.iface._acknowledgment = Acknowledgment()
    raw_admin = admin_pb2.AdminMessage()
    raw_admin.get_config_response.lora.hop_limit = 7

    payload = {
        "decoded": {
            "admin": {
                "getConfigResponse": {"lora": {}},
                "raw": raw_admin,
            }
        }
    }

    with caplog.at_level(logging.INFO):
        anode.onResponseRequestSettings(payload)

    assert anode.localConfig.lora.hop_limit == 7
    assert "lora:" in caplog.text


@pytest.mark.unit
def test_set_ringtone_returns_none_when_module_unavailable(
    mock_serial_interface: MagicMock,
    caplog: LogCaptureFixture,
) -> None:
    """_set_ringtone should return None when the ext notification module is unavailable."""
    anode = Node(mock_serial_interface, "!12345678", noProto=True)
    anode.module_available = MagicMock(return_value=False)  # type: ignore[method-assign]
    anode.ensureSessionKey = MagicMock()  # type: ignore[method-assign]

    with caplog.at_level(logging.WARNING):
        result = anode._set_ringtone("tone")

    assert result is None
    anode.ensureSessionKey.assert_not_called()
    assert "External Notification module not present" in caplog.text


@pytest.mark.unit
def test_set_ringtone_rejects_payloads_longer_than_max(
    mock_serial_interface: MagicMock,
) -> None:
    """_set_ringtone should reject values exceeding MAX_RINGTONE_LENGTH."""
    anode = Node(mock_serial_interface, "!12345678", noProto=True)
    anode.module_available = MagicMock(return_value=True)  # type: ignore[method-assign]
    anode.ensureSessionKey = MagicMock()  # type: ignore[method-assign]
    anode._send_admin = MagicMock()  # type: ignore[method-assign]

    with pytest.raises(
        MeshInterface.MeshInterfaceError,
        match=(
            f"The ringtone must be {node_module.MAX_RINGTONE_LENGTH} characters or fewer"
        ),
    ):
        anode._set_ringtone("x" * (node_module.MAX_RINGTONE_LENGTH + 1))

    anode.ensureSessionKey.assert_not_called()
    anode._send_admin.assert_not_called()


@pytest.mark.unit
def test_set_canned_message_returns_none_when_module_unavailable(
    mock_serial_interface: MagicMock,
    caplog: LogCaptureFixture,
) -> None:
    """_set_canned_message should return None when the canned message module is unavailable."""
    anode = Node(mock_serial_interface, "!12345678", noProto=True)
    anode.module_available = MagicMock(return_value=False)  # type: ignore[method-assign]
    anode.ensureSessionKey = MagicMock()  # type: ignore[method-assign]

    with caplog.at_level(logging.WARNING):
        result = anode._set_canned_message("hello")

    assert result is None
    anode.ensureSessionKey.assert_not_called()
    assert "Canned Message module not present" in caplog.text


@pytest.mark.unit
def test_get_channels_with_hash_alias_delegates_to_canonical(
    autospec_local_node_iface: Callable[[type[Any]], MagicMock],
) -> None:
    """get_channels_with_hash should delegate to getChannelsWithHash()."""
    anode = Node(autospec_local_node_iface(MeshInterface), "!12345678", noProto=True)
    expected = [{"index": 0, "role": "PRIMARY", "name": "x", "hash": 1}]

    with patch.object(anode, "getChannelsWithHash", return_value=expected) as wrapped:
        assert anode.get_channels_with_hash() == expected

    wrapped.assert_called_once_with()


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
def test_factoryReset_config_reset_uses_int_field_and_local_no_callback(
    autospec_local_node_iface: Callable[[type[Any]], MagicMock],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """factoryReset(full=False) should set config reset flag as int and skip callback for local node."""
    monkeypatch.setattr(node_module, "FACTORY_RESET_REQUEST_VALUE", 7)
    iface = autospec_local_node_iface(MeshInterface)
    anode = Node(iface, "!12345678", noProto=True)
    iface.localNode = anode
    anode.ensureSessionKey = MagicMock()  # type: ignore[method-assign]
    captured: dict[str, object] = {}
    sent_packet = mesh_pb2.MeshPacket()
    anode._send_admin = _make_fake_send_admin(  # type: ignore[method-assign,assignment]
        captured=captured,
        return_packet=sent_packet,
    )

    result = anode.factoryReset(full=False)

    assert result is sent_packet
    anode.ensureSessionKey.assert_called_once_with()
    sent_msg = cast(admin_pb2.AdminMessage, captured["msg"])
    assert sent_msg.factory_reset_config == node_module.FACTORY_RESET_REQUEST_VALUE
    assert sent_msg.factory_reset_device == 0
    assert captured["wantResponse"] is False
    assert captured["onResponse"] is None


@pytest.mark.unit
def test_factoryReset_full_device_uses_int_field_and_remote_ack_callback(
    autospec_local_node_iface: Callable[[type[Any]], MagicMock],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """factoryReset(full=True) should set device reset flag as int and use onAckNak for remote nodes."""
    monkeypatch.setattr(node_module, "FACTORY_RESET_REQUEST_VALUE", 7)
    iface = autospec_local_node_iface(MeshInterface)
    anode = Node(iface, "!12345678", noProto=True)
    anode.ensureSessionKey = MagicMock()  # type: ignore[method-assign]
    captured: dict[str, object] = {}
    sent_packet = mesh_pb2.MeshPacket()
    anode._send_admin = _make_fake_send_admin(  # type: ignore[method-assign,assignment]
        captured=captured,
        return_packet=sent_packet,
    )

    result = anode.factoryReset(full=True)

    assert result is sent_packet
    anode.ensureSessionKey.assert_called_once_with()
    sent_msg = cast(admin_pb2.AdminMessage, captured["msg"])
    assert sent_msg.factory_reset_device == node_module.FACTORY_RESET_REQUEST_VALUE
    assert sent_msg.factory_reset_config == 0
    assert captured["wantResponse"] is False
    response_handler = cast(Callable[[dict[str, Any]], Any], captured["onResponse"])
    assert getattr(response_handler, "__self__", None) is anode
    assert getattr(response_handler, "__func__", None) is Node.onAckNak


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
    anode.channels = [
        Channel(index=i, role=Channel.Role.DISABLED) for i in range(CHANNEL_LIMIT)
    ]

    channel_set = apponly_pb2.ChannelSet()
    for i in range(CHANNEL_LIMIT + 1):
        settings = channel_set.settings.add()
        settings.name = f"ch{i}"
        settings.psk = b"\x01"

    encoded = base64.urlsafe_b64encode(channel_set.SerializeToString()).decode("ascii")
    encoded = encoded.replace("=", "")
    url = f"https://meshtastic.org/e/#{encoded}"

    with caplog.at_level(logging.WARNING):
        anode.setURL(url)

    assert re.search(
        rf"URL contains more than {CHANNEL_LIMIT} channels",
        caplog.text,
        re.MULTILINE,
    )
    assert len(anode.channels) == CHANNEL_LIMIT
    assert anode.channels[0].settings.name == "ch0"
    assert anode.channels[CHANNEL_LIMIT - 1].settings.name == f"ch{CHANNEL_LIMIT - 1}"


@pytest.mark.unit
def test_setChannels_copies_input_channel_objects(
    autospec_local_node_iface: Callable[[type[Any]], MagicMock],
) -> None:
    """SetChannels should snapshot caller-provided channels instead of storing shared references."""
    anode = Node(autospec_local_node_iface(MeshInterface), "!12345678", noProto=True)
    source_channel = Channel(index=0, role=Channel.Role.PRIMARY)
    source_channel.settings.name = "source"
    source_channel.settings.psk = b"\x01"

    anode.setChannels([source_channel])
    source_channel.settings.name = "mutated-after-set"

    assert anode.channels is not None
    assert len(anode.channels) == CHANNEL_LIMIT
    assert anode.channels[0] is not source_channel
    assert anode.channels[0].settings.name == "source"


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
    selected_primary = anode.getChannelByChannelIndex(0)
    assert selected_primary is not None
    assert selected_primary is channel1
    # test secondary
    assert anode.getChannelByChannelIndex(1) is not None
    # test disabled
    assert anode.getChannelByChannelIndex(2) is not None
    # test invalid values
    assert anode.getChannelByChannelIndex(-1) is None
    assert anode.getChannelByChannelIndex(9) is None

    copied_primary = anode.getChannelCopyByChannelIndex(0)
    assert copied_primary is not None
    assert copied_primary is not channel1
    copied_primary.role = Channel.Role.DISABLED
    assert channel1.role == Channel.Role.PRIMARY


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
    assert sent_message.set_module_config.traffic_management.rate_limit_enabled is True


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


@pytest.mark.unit
def test_requestConfig_with_module_config_descriptor(
    mock_serial_interface: MagicMock,
) -> None:
    """Verify requestConfig sets get_module_config_request for LocalModuleConfig fields.

    Tests line 370: when configType is a field descriptor with containing_type.name
    != 'LocalConfig', it should set get_module_config_request to the field index.
    """
    anode = Node(mock_serial_interface, "!12345678", noProto=True)
    mock_serial_interface.localNode = anode

    # Get a field descriptor from LocalModuleConfig (not LocalConfig)
    module_config = localonly_pb2.LocalModuleConfig()
    mqtt_field = module_config.DESCRIPTOR.fields_by_name["mqtt"]

    sent_messages: list[admin_pb2.AdminMessage] = []
    anode._send_admin = _make_fake_send_admin(  # type: ignore[method-assign,assignment]
        sent_messages=sent_messages
    )

    anode.requestConfig(mqtt_field)

    assert len(sent_messages) == 1
    sent_msg = sent_messages[0]
    # mqtt field has index 0, should be set as get_module_config_request
    assert sent_msg.get_module_config_request == 0


@pytest.mark.unit
def test_showChannels_logs_snapshot_and_skips_disabled_entries(
    caplog: LogCaptureFixture,
    capsys: CaptureFixture[str],
    autospec_local_node_iface: Callable[[type[Any]], MagicMock],
) -> None:
    """ShowChannels should log channel snapshot and print only non-disabled channels."""
    anode = Node(autospec_local_node_iface(MeshInterface), "!12345678", noProto=True)
    primary = Channel(index=0, role=Channel.Role.PRIMARY)
    primary.settings.name = "primary"
    primary.settings.psk = b"\x01"
    disabled = Channel(index=1, role=Channel.Role.DISABLED)
    disabled.settings.name = "disabled"
    anode.channels = [primary, disabled]
    anode.getURL = MagicMock(side_effect=["primary-url", "complete-url"])  # type: ignore[method-assign]

    with caplog.at_level(logging.DEBUG):
        anode.showChannels()

    out, _ = capsys.readouterr()
    assert "self.channels" in caplog.text
    assert "Index 0: PRIMARY" in out
    assert "Index 1:" not in out


@pytest.mark.unit
def test_turnOffEncryptionOnPrimaryChannel_requires_loaded_channels(
    autospec_local_node_iface: Callable[[type[Any]], MagicMock],
) -> None:
    """TurnOffEncryptionOnPrimaryChannel should fail when channels are missing."""
    anode = Node(autospec_local_node_iface(MeshInterface), "!12345678", noProto=True)
    anode.channels = []

    with pytest.raises(
        MeshInterface.MeshInterfaceError,
        match="Error: No channels have been read",
    ):
        anode.turnOffEncryptionOnPrimaryChannel()


@pytest.mark.unit
def test_turnOffEncryptionOnPrimaryChannel_updates_primary_and_writes(
    autospec_local_node_iface: Callable[[type[Any]], MagicMock],
) -> None:
    """TurnOffEncryptionOnPrimaryChannel should disable PSK and write channel 0."""
    anode = Node(autospec_local_node_iface(MeshInterface), "!12345678", noProto=True)
    primary = Channel(index=0, role=Channel.Role.PRIMARY)
    primary.settings.psk = b"\x01"
    anode.channels = [primary]
    anode.writeChannel = MagicMock()  # type: ignore[method-assign]

    anode.turnOffEncryptionOnPrimaryChannel()

    assert anode.channels[0].settings.psk == fromPSK("none")
    anode.writeChannel.assert_called_once_with(0)


@pytest.mark.unit
def test_writeChannel_out_of_range_raises_mesh_error(
    autospec_local_node_iface: Callable[[type[Any]], MagicMock],
) -> None:
    """WriteChannel should reject invalid channel indices."""
    anode = Node(autospec_local_node_iface(MeshInterface), "!12345678", noProto=True)
    anode.channels = [Channel(index=0, role=Channel.Role.PRIMARY)]

    with pytest.raises(
        MeshInterface.MeshInterfaceError,
        match=r"Channel index 1 out of range \(0-0\)",
    ):
        anode.writeChannel(1)


@pytest.mark.unit
def test_deleteChannel_rejects_non_secondary_or_disabled(
    autospec_local_node_iface: Callable[[type[Any]], MagicMock],
) -> None:
    """DeleteChannel should only allow SECONDARY or DISABLED channels."""
    anode = Node(autospec_local_node_iface(MeshInterface), "!12345678", noProto=True)
    anode.channels = [Channel(index=0, role=Channel.Role.PRIMARY)]

    with pytest.raises(
        MeshInterface.MeshInterfaceError,
        match="Only SECONDARY or DISABLED channels can be deleted",
    ):
        anode.deleteChannel(0)


@pytest.mark.unit
def test_deleteChannel_rewrites_following_channels_and_updates_admin_index(
    autospec_local_node_iface: Callable[[type[Any]], MagicMock],
) -> None:
    """DeleteChannel should rewrite channels and use fresh admin-index resolution per write."""
    iface = autospec_local_node_iface(MeshInterface)
    anode = Node(iface, "!12345678", noProto=True)
    iface.localNode = anode

    primary = Channel(index=0, role=Channel.Role.PRIMARY)
    primary.settings.name = "primary"
    admin_secondary = Channel(index=1, role=Channel.Role.SECONDARY)
    admin_secondary.settings.name = "admin"
    disabled = Channel(index=2, role=Channel.Role.DISABLED)
    anode.channels = [primary, admin_secondary, disabled]
    anode.ensureSessionKey = MagicMock()  # type: ignore[method-assign]
    anode._send_admin = MagicMock()  # type: ignore[method-assign]

    anode.deleteChannel(1)

    assert anode.channels is not None
    assert len(anode.channels) == CHANNEL_LIMIT
    assert anode._send_admin.call_count == CHANNEL_LIMIT - 1
    assert all(
        call.kwargs["adminIndex"] == 0 for call in anode._send_admin.call_args_list
    )


@pytest.mark.unit
def test_channel_lookup_helpers_cover_name_disabled_and_admin_index(
    autospec_local_node_iface: Callable[[type[Any]], MagicMock],
) -> None:
    """Channel helper lookups should find expected channels under lock."""
    anode = Node(autospec_local_node_iface(MeshInterface), "!12345678", noProto=True)
    primary = Channel(index=0, role=Channel.Role.PRIMARY)
    primary.settings.name = "main"
    admin_channel = Channel(index=1, role=Channel.Role.SECONDARY)
    admin_channel.settings.name = "AdMiN"
    disabled = Channel(index=2, role=Channel.Role.DISABLED)
    anode.channels = [primary, admin_channel, disabled]

    named_channel = anode.getChannelByName("main")
    assert named_channel is not None
    assert named_channel is primary
    assert named_channel.index == primary.index

    disabled_channel = anode.getDisabledChannel()
    assert disabled_channel is not None
    assert disabled_channel is disabled
    assert disabled_channel.index == disabled.index

    named_channel_copy = anode.getChannelCopyByName("main")
    assert named_channel_copy is not None
    assert named_channel_copy is not primary
    assert named_channel_copy.index == primary.index

    disabled_channel_copy = anode.getDisabledChannelCopy()
    assert disabled_channel_copy is not None
    assert disabled_channel_copy is not disabled
    assert disabled_channel_copy.index == disabled.index

    named_channel.role = Channel.Role.DISABLED
    assert primary.role == Channel.Role.DISABLED
    disabled_channel.role = Channel.Role.PRIMARY
    assert disabled.role == Channel.Role.PRIMARY
    named_channel_copy.role = Channel.Role.PRIMARY
    assert primary.role == Channel.Role.DISABLED
    disabled_channel_copy.role = Channel.Role.DISABLED
    assert disabled.role == Channel.Role.PRIMARY
    assert anode._get_admin_channel_index() == 1
    assert anode.getAdminChannelIndex() == 1

    anode.channels = None
    assert anode.getDisabledChannel() is None


def _decode_channel_set_from_url(url: str) -> apponly_pb2.ChannelSet:
    """Decode and parse a ChannelSet from a meshtastic URL."""
    b64 = url.split("#")[-1]
    missing_padding = len(b64) % 4
    if missing_padding:
        b64 += "=" * (4 - missing_padding)
    raw = base64.urlsafe_b64decode(b64)
    channel_set = apponly_pb2.ChannelSet()
    channel_set.ParseFromString(raw)
    return channel_set


@pytest.mark.unit
def test_getURL_requests_lora_when_local_config_empty(
    autospec_local_node_iface: Callable[[type[Any]], MagicMock],
) -> None:
    """GetURL should request lora config when localConfig has no populated fields."""
    anode = Node(autospec_local_node_iface(MeshInterface), "!12345678", noProto=True)
    primary = Channel(index=0, role=Channel.Role.PRIMARY)
    primary.settings.name = "primary"
    primary.settings.psk = b"\x01"
    secondary = Channel(index=1, role=Channel.Role.SECONDARY)
    secondary.settings.name = "secondary"
    secondary.settings.psk = b"\x02"
    disabled = Channel(index=2, role=Channel.Role.DISABLED)
    anode.channels = [primary, secondary, disabled]
    anode.requestConfig = MagicMock()  # type: ignore[method-assign]

    url = anode.getURL(includeAll=False)

    anode.requestConfig.assert_called_once_with(
        anode.localConfig.DESCRIPTOR.fields_by_name["lora"]
    )
    channel_set = _decode_channel_set_from_url(url)
    assert len(channel_set.settings) == 1
    assert channel_set.settings[0].name == "primary"


@pytest.mark.unit
def test_setURL_rejects_missing_fragment_and_empty_fragment_data(
    autospec_local_node_iface: Callable[[type[Any]], MagicMock],
) -> None:
    """SetURL should fail fast for malformed fragment inputs once channels are loaded."""
    anode = Node(autospec_local_node_iface(MeshInterface), "!12345678", noProto=True)
    anode.channels = [Channel(index=0, role=Channel.Role.DISABLED)]

    with pytest.raises(MeshInterface.MeshInterfaceError, match="Invalid URL"):
        anode.setURL("https://meshtastic.org/e/not-a-fragment")

    with pytest.raises(MeshInterface.MeshInterfaceError, match="no channel data found"):
        anode.setURL("https://meshtastic.org/e/#")


@pytest.mark.unit
def test_setURL_add_only_adds_unique_named_channels(
    autospec_local_node_iface: Callable[[type[Any]], MagicMock],
) -> None:
    """setURL(addOnly=True) should ignore existing/empty names and add only new ones."""
    anode = Node(autospec_local_node_iface(MeshInterface), "!12345678", noProto=True)
    primary = Channel(index=0, role=Channel.Role.PRIMARY)
    primary.settings.name = "existing"
    disabled = Channel(index=1, role=Channel.Role.DISABLED)
    anode.channels = [primary, disabled]
    anode.writeChannel = MagicMock()  # type: ignore[method-assign]

    channel_set = apponly_pb2.ChannelSet()
    existing = channel_set.settings.add()
    existing.name = "existing"
    existing.psk = b"\x01"
    empty = channel_set.settings.add()
    empty.name = ""
    empty.psk = b"\x02"
    new_channel = channel_set.settings.add()
    new_channel.name = "new-ch"
    new_channel.psk = b"\x03"
    encoded = base64.urlsafe_b64encode(channel_set.SerializeToString()).decode("ascii")
    url = f"https://meshtastic.org/e/#{encoded.rstrip('=')}"

    anode.setURL(url, addOnly=True)

    assert anode.channels is not None
    assert anode.channels[1].settings.name == "new-ch"
    assert anode.channels[1].role == Channel.Role.SECONDARY
    anode.writeChannel.assert_called_once_with(1)


@pytest.mark.unit
def test_setURL_add_only_raises_when_no_disabled_slot_available(
    autospec_local_node_iface: Callable[[type[Any]], MagicMock],
) -> None:
    """setURL(addOnly=True) should fail if no DISABLED channels remain."""
    anode = Node(autospec_local_node_iface(MeshInterface), "!12345678", noProto=True)
    anode.channels = [Channel(index=i, role=Channel.Role.SECONDARY) for i in range(2)]

    channel_set = apponly_pb2.ChannelSet()
    new_channel = channel_set.settings.add()
    new_channel.name = "new-ch"
    new_channel.psk = b"\x01"
    encoded = base64.urlsafe_b64encode(channel_set.SerializeToString()).decode("ascii")
    url = f"https://meshtastic.org/e/#{encoded.rstrip('=')}"

    with pytest.raises(
        MeshInterface.MeshInterfaceError, match="No free channels were found"
    ):
        anode.setURL(url, addOnly=True)


@pytest.mark.unit
def test_setURL_replace_raises_if_channels_disappear_during_assignment(
    autospec_local_node_iface: Callable[[type[Any]], MagicMock],
) -> None:
    """SetURL replace-path should recheck channels before assignment in each loop iteration."""
    anode = Node(autospec_local_node_iface(MeshInterface), "!12345678", noProto=True)
    anode.channels = [Channel(index=0, role=Channel.Role.DISABLED)]
    anode._channels_lock = _DropChannelsOnEnterCountLock(  # type: ignore[assignment]
        anode, trigger_enter=3
    )

    channel_set = apponly_pb2.ChannelSet()
    setting = channel_set.settings.add()
    setting.name = "primary"
    setting.psk = b"\x01"
    encoded = base64.urlsafe_b64encode(channel_set.SerializeToString()).decode("ascii")
    url = f"https://meshtastic.org/e/#{encoded.rstrip('=')}"

    with pytest.raises(
        MeshInterface.MeshInterfaceError, match="Config or channels not loaded"
    ):
        anode.setURL(url, addOnly=False)


@pytest.mark.unit
def test_fixup_channels_truncates_and_reindexes_to_limit(
    autospec_local_node_iface: Callable[[type[Any]], MagicMock],
) -> None:
    """_fixup_channels should truncate over-limit input and maintain contiguous indices."""
    anode = Node(autospec_local_node_iface(MeshInterface), "!12345678", noProto=True)
    anode.channels = [
        Channel(index=i, role=Channel.Role.SECONDARY) for i in range(CHANNEL_LIMIT + 2)
    ]

    anode._fixup_channels()

    assert anode.channels is not None
    assert len(anode.channels) == CHANNEL_LIMIT
    assert [ch.index for ch in anode.channels] == list(range(CHANNEL_LIMIT))


@pytest.mark.unit
def test_fill_channels_handles_none_and_pads_to_limit(
    autospec_local_node_iface: Callable[[type[Any]], MagicMock],
) -> None:
    """_fill_channels should no-op for None and pad existing channel lists to max size."""
    anode = Node(autospec_local_node_iface(MeshInterface), "!12345678", noProto=True)
    anode.channels = None
    anode._fill_channels()
    assert anode.channels is None

    anode.channels = [Channel(index=0, role=Channel.Role.PRIMARY)]
    anode._fill_channels()
    assert anode.channels is not None
    assert len(anode.channels) == CHANNEL_LIMIT
    assert anode.channels[-1].role == Channel.Role.DISABLED


@pytest.mark.unit
def test_onResponseRequestChannel_routing_paths(
    autospec_local_node_iface: Callable[[type[Any]], MagicMock],
) -> None:
    """OnResponseRequestChannel should expire on routing failure and retry on routing success."""
    anode = Node(autospec_local_node_iface(MeshInterface), "!12345678", noProto=True)
    anode._request_channel = MagicMock()  # type: ignore[method-assign]

    anode.onResponseRequestChannel(
        {
            "decoded": {
                "portnum": "ROUTING_APP",
                "routing": {"errorReason": "NO_ROUTE"},
            }
        }
    )
    assert anode._request_channel.call_count == 0

    ch = Channel(index=3, role=Channel.Role.SECONDARY)
    anode.partialChannels = [ch]
    anode.onResponseRequestChannel(
        {"decoded": {"portnum": "ROUTING_APP", "routing": {"errorReason": "NONE"}}}
    )
    anode._request_channel.assert_called_once_with(3)


@pytest.mark.unit
def test_onResponseRequestChannel_handles_partial_and_final_channel(
    autospec_local_node_iface: Callable[[type[Any]], MagicMock],
) -> None:
    """OnResponseRequestChannel should request next channel until the final channel arrives."""
    anode = Node(autospec_local_node_iface(MeshInterface), "!12345678", noProto=True)
    anode._request_channel = MagicMock()  # type: ignore[method-assign]

    partial = Channel(index=2, role=Channel.Role.SECONDARY)
    anode.onResponseRequestChannel(
        {
            "decoded": {
                "portnum": "ADMIN_APP",
                "admin": {"raw": MagicMock(get_channel_response=partial)},
            }
        }
    )
    anode._request_channel.assert_called_once_with(3)

    final = Channel(index=CHANNEL_LIMIT - 1, role=Channel.Role.SECONDARY)
    anode._request_channel.reset_mock()
    anode.onResponseRequestChannel(
        {
            "decoded": {
                "portnum": "ADMIN_APP",
                "admin": {"raw": MagicMock(get_channel_response=final)},
            }
        }
    )
    anode._request_channel.assert_not_called()
    assert anode.channels is not None
    assert len(anode.channels) == CHANNEL_LIMIT


@pytest.mark.unit
def test_onAckNak_handles_missing_invalid_and_ack_variants(
    autospec_local_node_iface: Callable[[type[Any]], MagicMock],
) -> None:
    """OnAckNak should handle malformed payloads and update ACK state for valid variants."""
    iface = autospec_local_node_iface(MeshInterface)
    iface._acknowledgment = Acknowledgment()
    iface.localNode.nodeNum = 123
    anode = Node(iface, "!12345678", noProto=True)

    anode.onAckNak({"decoded": {}})
    assert iface._acknowledgment.receivedAck is False
    assert iface._acknowledgment.receivedNak is False
    assert iface._acknowledgment.receivedImplAck is False

    anode.onAckNak({"decoded": {"routing": {"errorReason": "NO_REPLY"}}})
    assert iface._acknowledgment.receivedNak is True

    iface._acknowledgment = Acknowledgment()
    anode.onAckNak({"decoded": {"routing": {"errorReason": "NONE"}}})
    assert iface._acknowledgment.receivedAck is False

    anode.onAckNak({"decoded": {"routing": {"errorReason": "NONE"}}, "from": "abc"})
    assert iface._acknowledgment.receivedAck is False

    anode.onAckNak({"decoded": {"routing": {"errorReason": "NONE"}}, "from": 123})
    assert iface._acknowledgment.receivedImplAck is True

    iface._acknowledgment = Acknowledgment()
    anode.onAckNak({"decoded": {"routing": {"errorReason": "NONE"}}, "from": 124})
    assert iface._acknowledgment.receivedAck is True


@pytest.mark.unit
def test_send_admin_no_proto_returns_none(
    autospec_local_node_iface: Callable[[type[Any]], MagicMock],
) -> None:
    """_send_admin should no-op when protocol usage is disabled."""
    anode = Node(autospec_local_node_iface(MeshInterface), "!12345678", noProto=True)
    msg = admin_pb2.AdminMessage()

    assert anode._send_admin(msg) is None


@pytest.mark.unit
def test_send_admin_uses_session_passkey_and_selected_admin_index(
    autospec_local_node_iface: Callable[[type[Any]], MagicMock],
) -> None:
    """_send_admin should attach passkey and send over the selected admin channel."""
    iface = autospec_local_node_iface(MeshInterface)
    iface.localNode._get_admin_channel_index.return_value = 3
    iface._get_or_create_by_num.return_value = {"adminSessionPassKey": b"secret"}
    packet = mesh_pb2.MeshPacket()
    iface.sendData.return_value = packet
    anode = Node(iface, 321, noProto=False)
    msg = admin_pb2.AdminMessage()

    response_handler = MagicMock()
    result = anode._send_admin(msg, wantResponse=True, onResponse=response_handler)

    assert result is packet
    assert msg.session_passkey == b"secret"
    iface.sendData.assert_called_once()
    assert iface.sendData.call_args.kwargs["channelIndex"] == 3
    assert iface.sendData.call_args.kwargs["pkiEncrypted"] is True


@pytest.mark.unit
def test_ensureSessionKey_requests_only_when_missing(
    autospec_local_node_iface: Callable[[type[Any]], MagicMock],
) -> None:
    """EnsureSessionKey should request a key only if one is not already cached."""
    iface = autospec_local_node_iface(MeshInterface)
    anode = Node(iface, 555, noProto=False)
    anode.requestConfig = MagicMock()  # type: ignore[method-assign]

    iface._get_or_create_by_num.return_value = {}
    anode.ensureSessionKey()
    anode.requestConfig.assert_called_once_with(
        admin_pb2.AdminMessage.SESSIONKEY_CONFIG
    )

    anode.requestConfig.reset_mock()
    iface._get_or_create_by_num.return_value = {"adminSessionPassKey": b"x"}
    anode.ensureSessionKey()
    anode.requestConfig.assert_not_called()


@pytest.mark.unit
def test_get_channels_with_hash_handles_missing_fields(
    autospec_local_node_iface: Callable[[type[Any]], MagicMock],
) -> None:
    """_get_channels_with_hash should emit hashes only when both name and PSK are present."""
    anode = Node(autospec_local_node_iface(MeshInterface), "!12345678", noProto=True)
    with_hash = Channel(index=0, role=Channel.Role.PRIMARY)
    with_hash.settings.name = "hash-me"
    with_hash.settings.psk = b"\x01\x02"
    without_hash = Channel(index=1, role=Channel.Role.SECONDARY)
    anode.channels = [with_hash, without_hash]

    entries = anode._get_channels_with_hash()

    assert len(entries) == 2
    assert entries[0]["hash"] is not None
    assert entries[1]["hash"] is None


@pytest.mark.unit
def test_deleteChannel_missing_or_out_of_range_validations(
    autospec_local_node_iface: Callable[[type[Any]], MagicMock],
) -> None:
    """DeleteChannel should validate missing channels and invalid indices."""
    anode = Node(autospec_local_node_iface(MeshInterface), "!12345678", noProto=True)
    anode.channels = None
    with pytest.raises(
        MeshInterface.MeshInterfaceError, match="Error: No channels have been read"
    ):
        anode.deleteChannel(0)

    anode.channels = [Channel(index=0, role=Channel.Role.SECONDARY)]
    with pytest.raises(
        MeshInterface.MeshInterfaceError,
        match=r"Channel index 5 out of range \(0-0\)",
    ):
        anode.deleteChannel(5)


@pytest.mark.unit
def test_deleteChannel_rewrite_uses_snapshot_when_channels_change_after_lock_release(
    autospec_local_node_iface: Callable[[type[Any]], MagicMock],
) -> None:
    """DeleteChannel should complete rewrites from a captured snapshot even if channels change later."""
    iface = autospec_local_node_iface(MeshInterface)
    anode = Node(iface, "!12345678", noProto=True)
    iface.localNode = anode
    anode.channels = [Channel(index=0, role=Channel.Role.SECONDARY)]
    anode._channels_lock = _DropChannelsOnEnterCountLock(  # type: ignore[assignment]
        anode, trigger_enter=2
    )
    anode.ensureSessionKey = MagicMock()  # type: ignore[method-assign]
    anode._send_admin = MagicMock()  # type: ignore[method-assign]

    anode.deleteChannel(0)

    # The lock stub drops channels on the second lock acquisition (during admin
    # index lookup), but rewrite sends still complete from the captured snapshot.
    assert anode.channels is None
    assert anode._send_admin.call_count == CHANNEL_LIMIT


@pytest.mark.unit
def test_channel_lookup_helpers_return_none_when_no_match(
    autospec_local_node_iface: Callable[[type[Any]], MagicMock],
) -> None:
    """Lookup helpers should return no result when entries are absent."""
    anode = Node(autospec_local_node_iface(MeshInterface), "!12345678", noProto=True)
    anode.channels = [Channel(index=0, role=Channel.Role.SECONDARY)]

    assert anode.getChannelByName("missing") is None
    assert anode.getDisabledChannel() is None
    assert anode._get_admin_channel_index() == 0


@pytest.mark.unit
def test_setURL_reports_decode_and_parse_errors(
    autospec_local_node_iface: Callable[[type[Any]], MagicMock],
) -> None:
    """SetURL should surface base64 decode and protobuf parse failures."""
    anode = Node(autospec_local_node_iface(MeshInterface), "!12345678", noProto=True)
    anode.channels = [Channel(index=0, role=Channel.Role.DISABLED)]

    with pytest.raises(MeshInterface.MeshInterfaceError, match="Invalid URL"):
        anode.setURL("https://meshtastic.org/e/#_")

    bad_proto = base64.urlsafe_b64encode(b"\x00\x01").decode("ascii").rstrip("=")
    with pytest.raises(
        MeshInterface.MeshInterfaceError,
        match="Unable to parse channel settings from URL",
    ):
        anode.setURL(f"https://meshtastic.org/e/#{bad_proto}")


@pytest.mark.unit
def test_setURL_reports_empty_settings_when_channels_loaded(
    autospec_local_node_iface: Callable[[type[Any]], MagicMock],
) -> None:
    """SetURL should reject URLs that decode to an empty ChannelSet."""
    anode = Node(autospec_local_node_iface(MeshInterface), "!12345678", noProto=True)
    anode.channels = [Channel(index=0, role=Channel.Role.DISABLED)]
    channel_set = apponly_pb2.ChannelSet()
    channel_set.lora_config.tx_enabled = True
    encoded = base64.urlsafe_b64encode(channel_set.SerializeToString()).decode("ascii")

    with pytest.raises(
        MeshInterface.MeshInterfaceError, match="There were no settings"
    ):
        anode.setURL(f"https://meshtastic.org/e/#{encoded.rstrip('=')}")


@pytest.mark.unit
def test_setURL_add_only_rechecks_channels_before_addition(
    autospec_local_node_iface: Callable[[type[Any]], MagicMock],
) -> None:
    """SetURL(addOnly=True) should fail if channels disappear before add loop mutation."""
    anode = Node(autospec_local_node_iface(MeshInterface), "!12345678", noProto=True)
    anode.channels = [Channel(index=0, role=Channel.Role.DISABLED)]
    anode._channels_lock = _DropChannelsOnEnterCountLock(  # type: ignore[assignment]
        anode, trigger_enter=2
    )

    channel_set = apponly_pb2.ChannelSet()
    setting = channel_set.settings.add()
    setting.name = "new-channel"
    setting.psk = b"\x01"
    encoded = base64.urlsafe_b64encode(channel_set.SerializeToString()).decode("ascii")

    with pytest.raises(
        MeshInterface.MeshInterfaceError, match="Config or channels not loaded"
    ):
        anode.setURL(f"https://meshtastic.org/e/#{encoded.rstrip('=')}", addOnly=True)


@pytest.mark.unit
def test_setURL_replace_rechecks_channels_before_length_calculation(
    autospec_local_node_iface: Callable[[type[Any]], MagicMock],
) -> None:
    """SetURL replace path should fail if channels disappear before max-channel snapshot."""
    anode = Node(autospec_local_node_iface(MeshInterface), "!12345678", noProto=True)
    anode.channels = [Channel(index=0, role=Channel.Role.DISABLED)]
    anode._channels_lock = _DropChannelsOnEnterCountLock(  # type: ignore[assignment]
        anode, trigger_enter=2
    )

    channel_set = apponly_pb2.ChannelSet()
    setting = channel_set.settings.add()
    setting.name = "primary"
    setting.psk = b"\x01"
    encoded = base64.urlsafe_b64encode(channel_set.SerializeToString()).decode("ascii")

    with pytest.raises(
        MeshInterface.MeshInterfaceError, match="Config or channels not loaded"
    ):
        anode.setURL(f"https://meshtastic.org/e/#{encoded.rstrip('=')}")


@pytest.mark.unit
def test_fixup_channels_locked_returns_immediately_when_channels_none(
    autospec_local_node_iface: Callable[[type[Any]], MagicMock],
) -> None:
    """_fixup_channels_locked should no-op when channels are unset."""
    anode = Node(autospec_local_node_iface(MeshInterface), "!12345678", noProto=True)
    anode.channels = None

    anode._fixup_channels()

    assert anode.channels is None


@pytest.mark.unit
def test_onRequestGetMetadata_handles_routing_error_and_ack_only(
    autospec_local_node_iface: Callable[[type[Any]], MagicMock],
) -> None:
    """OnRequestGetMetadata should NAK on routing error and avoid recursive retries."""
    iface = autospec_local_node_iface(MeshInterface)
    iface._acknowledgment = Acknowledgment()
    anode = Node(iface, "!12345678", noProto=True)
    anode.getMetadata = MagicMock()  # type: ignore[method-assign]

    anode.onRequestGetMetadata(
        {"decoded": {"portnum": "ROUTING_APP", "routing": {"errorReason": "NO_PATH"}}}
    )
    assert iface._acknowledgment.receivedNak is True

    iface._acknowledgment = Acknowledgment()
    anode.onRequestGetMetadata(
        {"decoded": {"portnum": "ROUTING_APP", "routing": {"errorReason": "NONE"}}}
    )
    anode.getMetadata.assert_not_called()


@pytest.mark.unit
def test_onRequestGetMetadata_handles_non_routing_error_reason(
    autospec_local_node_iface: Callable[[type[Any]], MagicMock],
) -> None:
    """OnRequestGetMetadata should mark NAK for decoded routing errors outside ROUTING_APP."""
    iface = autospec_local_node_iface(MeshInterface)
    iface._acknowledgment = Acknowledgment()
    anode = Node(iface, "!12345678", noProto=True)

    anode.onRequestGetMetadata(
        {
            "decoded": {
                "portnum": "ADMIN_APP",
                "routing": {"errorReason": "TIMEOUT"},
            }
        }
    )

    assert iface._acknowledgment.receivedNak is True


@pytest.mark.unit
def test_onRequestGetMetadata_logs_valid_and_fallback_enum_values(
    autospec_local_node_iface: Callable[[type[Any]], MagicMock],
    caplog: LogCaptureFixture,
) -> None:
    """OnRequestGetMetadata should handle both valid and unknown enum values."""
    iface = autospec_local_node_iface(MeshInterface)
    iface._acknowledgment = Acknowledgment()
    anode = Node(iface, "!12345678", noProto=True)
    anode._timeout = MagicMock()

    valid_raw = admin_pb2.AdminMessage()
    valid_resp = valid_raw.get_device_metadata_response
    valid_resp.firmware_version = "fw"
    valid_resp.device_state_version = 1
    valid_resp.role = config_pb2.Config.DeviceConfig.Role.CLIENT
    valid_resp.position_flags = 0
    valid_resp.hw_model = mesh_pb2.HardwareModel.TBEAM
    valid_resp.hasPKC = True
    with caplog.at_level(logging.INFO):
        anode.onRequestGetMetadata(
            {"decoded": {"portnum": "ADMIN_APP", "admin": {"raw": valid_raw}}}
        )
    assert iface._acknowledgment.receivedAck is True
    assert iface.metadata.firmware_version == "fw"
    anode._timeout.reset.assert_called()

    iface._acknowledgment = Acknowledgment()
    unknown_raw = admin_pb2.AdminMessage()
    unknown_resp = unknown_raw.get_device_metadata_response
    unknown_resp.firmware_version = "fw2"
    unknown_resp.device_state_version = 2
    unknown_resp.role = cast(config_pb2.Config.DeviceConfig.Role.ValueType, 999)
    unknown_resp.position_flags = 0
    unknown_resp.hw_model = cast(mesh_pb2.HardwareModel.ValueType, 999)
    unknown_resp.hasPKC = False
    unknown_resp.excluded_modules = 1
    anode.onRequestGetMetadata(
        {"decoded": {"portnum": "ADMIN_APP", "admin": {"raw": unknown_raw}}}
    )
    assert iface._acknowledgment.receivedAck is True
    assert iface.metadata.firmware_version == "fw2"


@pytest.mark.unit
def test_onRequestGetMetadata_updates_metadata_under_node_db_lock(
    autospec_local_node_iface: Callable[[type[Any]], MagicMock],
) -> None:
    """onRequestGetMetadata should update iface.metadata while holding iface._node_db_lock."""
    iface = autospec_local_node_iface(MeshInterface)
    iface._acknowledgment = Acknowledgment()
    iface._node_db_lock = _TrackingLock()
    anode = Node(iface, "!12345678", noProto=True)
    anode._timeout = MagicMock()

    raw = admin_pb2.AdminMessage()
    response = raw.get_device_metadata_response
    response.firmware_version = "2.7.19"
    response.device_state_version = 25
    response.role = config_pb2.Config.DeviceConfig.Role.CLIENT
    response.position_flags = 0
    response.hw_model = mesh_pb2.HardwareModel.PORTDUINO
    response.hasPKC = True

    anode.onRequestGetMetadata(
        {"decoded": {"portnum": "ADMIN_APP", "admin": {"raw": raw}}}
    )

    assert iface._node_db_lock.enter_count == 1
    assert iface.metadata.firmware_version == "2.7.19"


@pytest.mark.unit
def test_onRequestGetMetadata_emits_stdout_when_redirected(
    autospec_local_node_iface: Callable[[type[Any]], MagicMock],
    capsys: CaptureFixture[str],
) -> None:
    """Metadata response should still emit stdout lines for legacy redirect parsers."""
    iface = autospec_local_node_iface(MeshInterface)
    iface._acknowledgment = Acknowledgment()
    anode = Node(iface, "!12345678", noProto=True)
    anode._timeout = MagicMock()

    raw = admin_pb2.AdminMessage()
    resp = raw.get_device_metadata_response
    resp.firmware_version = "2.7.18"
    resp.device_state_version = 24
    resp.role = config_pb2.Config.DeviceConfig.Role.CLIENT
    resp.position_flags = 0
    resp.hw_model = mesh_pb2.HardwareModel.PORTDUINO
    resp.hasPKC = True

    anode.onRequestGetMetadata(
        {"decoded": {"portnum": "ADMIN_APP", "admin": {"raw": raw}}}
    )

    out, _err = capsys.readouterr()
    assert "firmware_version: 2.7.18" in out


@pytest.mark.unit
def test_emit_cached_metadata_returns_false_without_firmware_version(
    autospec_local_node_iface: Callable[[type[Any]], MagicMock],
) -> None:
    """_emit_cached_metadata_for_stdout should return False when firmware_version is missing."""
    iface = autospec_local_node_iface(MeshInterface)
    iface.metadata = mesh_pb2.DeviceMetadata()
    anode = Node(iface, "!12345678", noProto=True)

    assert anode._emit_cached_metadata_for_stdout() is False


@pytest.mark.unit
def test_emit_cached_metadata_uses_fallback_values_for_unknown_enums(
    autospec_local_node_iface: Callable[[type[Any]], MagicMock],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """_emit_cached_metadata_for_stdout should emit numeric fallback values for unknown enum members."""
    iface = autospec_local_node_iface(MeshInterface)
    iface.metadata = mesh_pb2.DeviceMetadata(
        firmware_version="2.7.18",
        device_state_version=24,
        role=cast(config_pb2.Config.DeviceConfig.Role.ValueType, 999),
        position_flags=0,
        hw_model=cast(mesh_pb2.HardwareModel.ValueType, 999),
        hasPKC=False,
        excluded_modules=1,
    )
    anode = Node(iface, "!12345678", noProto=True)
    emitted: list[str] = []
    monkeypatch.setattr(anode, "_emit_metadata_line", emitted.append)

    assert anode._emit_cached_metadata_for_stdout() is True
    assert "role: 999" in emitted
    assert "hw_model: 999" in emitted
    assert any(line.startswith("excluded_modules:") for line in emitted)


@pytest.mark.unit
def test_emit_cached_metadata_reads_metadata_under_node_db_lock(
    autospec_local_node_iface: Callable[[type[Any]], MagicMock],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """_emit_cached_metadata_for_stdout should snapshot iface.metadata under iface._node_db_lock."""
    iface = autospec_local_node_iface(MeshInterface)
    iface._node_db_lock = _TrackingLock()
    iface.metadata = mesh_pb2.DeviceMetadata(
        firmware_version="2.7.18",
        device_state_version=24,
        role=config_pb2.Config.DeviceConfig.Role.CLIENT,
        position_flags=0,
        hw_model=mesh_pb2.HardwareModel.PORTDUINO,
        hasPKC=True,
    )
    anode = Node(iface, "!12345678", noProto=True)
    emitted: list[str] = []
    monkeypatch.setattr(anode, "_emit_metadata_line", emitted.append)

    assert anode._emit_cached_metadata_for_stdout() is True
    assert iface._node_db_lock.enter_count == 1
    assert any("firmware_version: 2.7.18" in line for line in emitted)


@pytest.mark.unit
def test_getMetadata_waits_for_redirected_stdout_callback_output(
    autospec_local_node_iface: Callable[[type[Any]], MagicMock],
    capsys: CaptureFixture[str],
) -> None:
    """GetMetadata should keep redirected stdout active until metadata callback emits."""
    iface = autospec_local_node_iface(MeshInterface)
    iface._acknowledgment = Acknowledgment()
    iface.waitForAckNak = MagicMock()
    anode = Node(iface, "!12345678", noProto=True)
    anode._emit_cached_metadata_for_stdout = MagicMock(return_value=True)  # type: ignore[method-assign]

    def _fake_send_admin(
        _msg: admin_pb2.AdminMessage,
        wantResponse: bool = False,
        onResponse: Callable[[dict[str, Any]], Any] | None = None,
        adminIndex: int = 0,
    ) -> None:
        _ = (wantResponse, adminIndex)
        assert onResponse is not None
        raw = admin_pb2.AdminMessage()
        response = raw.get_device_metadata_response
        response.firmware_version = "2.7.18"
        response.device_state_version = 24
        response.role = config_pb2.Config.DeviceConfig.Role.CLIENT
        response.position_flags = 0
        response.hw_model = mesh_pb2.HardwareModel.PORTDUINO
        response.hasPKC = True

        timer = threading.Timer(
            0.05,
            lambda: onResponse(
                {
                    "decoded": {
                        "portnum": "ADMIN_APP",
                        "admin": {"raw": raw},
                    }
                }
            ),
        )
        timer.daemon = True
        timer.start()

    anode._send_admin = _fake_send_admin  # type: ignore[assignment]
    anode.getMetadata()

    out, _err = capsys.readouterr()
    assert "firmware_version: 2.7.18" in out
    anode._emit_cached_metadata_for_stdout.assert_not_called()


@pytest.mark.unit
def test_getMetadata_emits_cached_metadata_when_callback_never_arrives(
    autospec_local_node_iface: Callable[[type[Any]], MagicMock],
    capsys: CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """GetMetadata should emit cached interface metadata for redirected stdout parsers."""
    iface = autospec_local_node_iface(MeshInterface)
    iface._acknowledgment = Acknowledgment()
    iface.waitForAckNak = MagicMock()
    iface.metadata = mesh_pb2.DeviceMetadata(
        firmware_version="2.7.18",
        device_state_version=24,
        role=config_pb2.Config.DeviceConfig.Role.CLIENT_MUTE,
        position_flags=0,
        hw_model=mesh_pb2.HardwareModel.PORTDUINO,
        hasPKC=True,
    )
    anode = Node(iface, "!12345678", noProto=True)

    monkeypatch.setattr(node_module, "METADATA_STDOUT_COMPAT_WAIT_SECONDS", 0.01)

    def _fake_send_admin(
        _msg: admin_pb2.AdminMessage,
        wantResponse: bool = False,
        onResponse: Callable[[dict[str, Any]], Any] | None = None,
        adminIndex: int = 0,
    ) -> None:
        _ = (wantResponse, onResponse, adminIndex)

    anode._send_admin = _fake_send_admin  # type: ignore[assignment]
    anode.getMetadata()

    out, _err = capsys.readouterr()
    assert "firmware_version: 2.7.18" in out


@pytest.mark.unit
def test_on_response_request_settings_warns_for_unrecognized_payload_shape(
    mock_serial_interface: MagicMock,
    caplog: LogCaptureFixture,
) -> None:
    """OnResponseRequestSettings should warn and return for unsupported response payloads."""
    anode = Node(mock_serial_interface, "!12345678", noProto=True)
    anode.iface._acknowledgment = Acknowledgment()

    with caplog.at_level(logging.WARNING):
        anode.onResponseRequestSettings(
            {"decoded": {"admin": {"raw": admin_pb2.AdminMessage()}}}
        )

    assert "Did not receive a valid response" in caplog.text
