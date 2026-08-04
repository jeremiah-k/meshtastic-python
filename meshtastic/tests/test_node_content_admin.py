"""Meshtastic unit tests for node.py."""

import logging
import re
from collections.abc import Callable
from typing import Any, cast
from unittest.mock import MagicMock, patch

import pytest
from pytest import CaptureFixture, LogCaptureFixture

from .. import node as node_module
from ..mesh_interface import MeshInterface
from ..node import MAX_CHANNELS, Node
from ..protobuf import (
    admin_pb2,
    config_pb2,
    localonly_pb2,
    mesh_pb2,
)
from ..protobuf.channel_pb2 import Channel  # pylint: disable=E0611
from ..util import Acknowledgment

from ._node_legacy_support import (
    _TrackingLock,
    _get_mock_call_arg,
    _make_fake_send_admin,
)

CHANNEL_LIMIT = MAX_CHANNELS


@pytest.mark.unit
def test_tracking_lock_rejects_nested_acquisition() -> None:
    """_TrackingLock should fail on nested acquisition attempts."""
    lock = _TrackingLock()
    with lock:
        with pytest.raises(
            AssertionError, match="_TrackingLock does not allow nested acquisition"
        ):
            with lock:
                pass


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
    captured: dict[str, object] = {}
    fake_send_admin = _make_fake_send_admin(
        sent_messages=sent_messages,
        captured=captured,
        response_payload=response_payload,
        return_packet=request_packet,
    )
    anode._send_admin = fake_send_admin  # type: ignore[method-assign,assignment]

    assert anode.get_canned_message() == "hello world"
    assert anode.cannedPluginMessage == "hello world"
    assert len(sent_messages) == 1
    assert sent_messages[0].get_canned_message_module_messages_request is True
    assert captured["wantResponse"] is True

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
    assert captured["adminIndex"] is None
    assert anode.cannedPluginMessage is None
    assert anode.cannedPluginMessageMessages is None


@pytest.mark.unit
def test_set_canned_message_over_limit_raises(mock_serial_interface: MagicMock) -> None:
    """set_canned_message should reject messages above the configured limit."""
    anode = Node(mock_serial_interface, "!12345678", noProto=True)
    limit = node_module.MAX_CANNED_MESSAGE_LENGTH
    with pytest.raises(
        MeshInterface.MeshInterfaceError,
        match=f"The canned message must be {limit} characters or fewer",
    ):
        anode.set_canned_message("a" * (limit + 1))


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
    assert "Received settings block: lora" in caplog.text


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
def test_factoryReset_config_reset_uses_int_field_and_local_ack_callback(
    autospec_local_node_iface: Callable[[type[Any]], MagicMock],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Local config reset should register ACK handling before the packet is sent."""
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
    response_handler = cast(Callable[[dict[str, Any]], Any], captured["onResponse"])
    assert getattr(response_handler, "__self__", None) is anode
    assert getattr(response_handler, "__func__", None) is Node.onAckNak


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
@pytest.mark.parametrize(
    ("packet", "expected_flags"),
    [
        pytest.param(
            {"decoded": {}},
            (False, True, False),
            id="missing-routing",
        ),
        pytest.param(
            {"decoded": {"routing": {"errorReason": "NO_REPLY"}}},
            (False, True, False),
            id="routing-no-reply",
        ),
        pytest.param(
            {"decoded": {"routing": {"errorReason": "NONE"}}},
            (False, True, False),
            id="missing-source",
        ),
        pytest.param(
            {"decoded": {"routing": {"errorReason": "NONE"}}, "from": "abc"},
            (False, True, False),
            id="invalid-source",
        ),
        pytest.param(
            {"decoded": {"routing": {"errorReason": "NONE"}}, "from": 123},
            (False, False, True),
            id="implicit-ack",
        ),
        pytest.param(
            {"decoded": {"routing": {"errorReason": "NONE"}}, "from": 124},
            (True, False, False),
            id="remote-ack",
        ),
    ],
)
def test_onAckNak_classifies_ack_nak_variants(
    packet: dict[str, Any],
    expected_flags: tuple[bool, bool, bool],
    autospec_local_node_iface: Callable[[type[Any]], MagicMock],
) -> None:
    """ACK/NAK variants should set exactly the expected acknowledgment flag."""
    iface = autospec_local_node_iface(MeshInterface)
    iface.localNode.nodeNum = 123
    anode = Node(iface, "!12345678", noProto=True)
    iface._acknowledgment = Acknowledgment()

    anode.onAckNak(packet)

    acknowledgment = iface._acknowledgment
    assert (
        acknowledgment.receivedAck,
        acknowledgment.receivedNak,
        acknowledgment.receivedImplAck,
    ) == expected_flags

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
    """_send_admin should attach passkey to outbound message and send over the selected admin channel."""
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
    iface.sendData.assert_called_once()
    outbound_msg = iface.sendData.call_args[0][0]
    assert outbound_msg.session_passkey == b"secret"
    assert msg.session_passkey == b""
    assert iface.sendData.call_args.kwargs["channelIndex"] == 3
    assert iface.sendData.call_args.kwargs["pkiEncrypted"] is True

@pytest.mark.unit
def test_send_admin_respects_explicit_channel_zero(
    autospec_local_node_iface: Callable[[type[Any]], MagicMock],
) -> None:
    """_send_admin should treat channel 0 as explicit, not as auto-detect."""
    iface = autospec_local_node_iface(MeshInterface)
    iface.localNode._get_admin_channel_index.return_value = 3
    iface._get_or_create_by_num.return_value = {"adminSessionPassKey": b"secret"}
    packet = mesh_pb2.MeshPacket()
    iface.sendData.return_value = packet
    anode = Node(iface, 321, noProto=False)
    msg = admin_pb2.AdminMessage()

    result = anode._send_admin(msg, adminIndex=0)

    assert result is packet
    assert iface.sendData.call_args.kwargs["channelIndex"] == 0

@pytest.mark.unit
def test_ensureSessionKey_requests_only_when_missing(
    autospec_local_node_iface: Callable[[type[Any]], MagicMock],
) -> None:
    """EnsureSessionKey should request only when missing and forward the selected admin index."""
    iface = autospec_local_node_iface(MeshInterface)
    anode = Node(iface, 555, noProto=False)
    anode.requestConfig = MagicMock()  # type: ignore[method-assign]
    anode._timeout = MagicMock()
    anode._timeout.waitForSet.return_value = True

    iface._get_or_create_by_num.return_value = {}
    anode.ensureSessionKey(adminIndex=6)
    assert anode.requestConfig.call_count == 1
    request_config_call = anode.requestConfig.call_args
    assert request_config_call.args[0] == admin_pb2.AdminMessage.SESSIONKEY_CONFIG
    assert (
        _get_mock_call_arg(
            request_config_call,
            name="adminIndex",
            positional_index=1,
        )
        == 6
    )

    anode.requestConfig.reset_mock()
    iface._get_or_create_by_num.return_value = {"adminSessionPassKey": b"x"}
    anode.ensureSessionKey()
    anode.requestConfig.assert_not_called()
