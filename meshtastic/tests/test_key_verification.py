"""Tests for the firmware 2.8 key-verification handshake helpers."""

import threading
from unittest.mock import MagicMock

import pytest
from pubsub import pub

from meshtastic.key_verification import (
    build_key_verification_admin,
    send_key_verification,
)
from meshtastic.mesh_interface import MeshInterface
from meshtastic.protobuf import admin_pb2, mesh_pb2, portnums_pb2


def _interface_with_node_num(node_num: int) -> MagicMock:
    interface = MagicMock(spec=MeshInterface)
    interface.myInfo = MagicMock()
    interface.myInfo.my_node_num = node_num
    return interface


@pytest.mark.unit
def test_build_rejects_unknown_stage() -> None:
    """Unknown stage names must be rejected before any message is built."""
    with pytest.raises(ValueError, match="unknown key-verification stage"):
        build_key_verification_admin("revoke")


@pytest.mark.unit
def test_build_initiate_requires_remote_nodenum() -> None:
    """Initiation without a peer node number must fail."""
    with pytest.raises(ValueError, match="initiate requires"):
        build_key_verification_admin("initiate")


@pytest.mark.unit
def test_build_initiate_sets_type_and_peer() -> None:
    """Initiation records the message type and the peer being verified."""
    message = build_key_verification_admin("initiate", remote_nodenum=0xABCD1234)
    assert message.message_type == admin_pb2.KeyVerificationAdmin.INITIATE_VERIFICATION
    assert message.remote_nodenum == 0xABCD1234


@pytest.mark.unit
def test_build_later_stages_require_nonce() -> None:
    """Every post-initiate stage must echo the handshake nonce."""
    for stage in ("provide", "verify", "no-verify"):
        with pytest.raises(ValueError, match="requires the nonce"):
            build_key_verification_admin(stage)


@pytest.mark.unit
def test_build_provide_requires_four_digit_security_number() -> None:
    """The provide stage validates presence and range of the security number."""
    with pytest.raises(ValueError, match="provide requires"):
        build_key_verification_admin("provide", nonce=5)
    with pytest.raises(ValueError, match="four digits"):
        build_key_verification_admin("provide", nonce=5, security_number=10000)
    message = build_key_verification_admin("provide", nonce=5, security_number=4242)
    assert message.security_number == 4242
    assert (
        message.message_type == admin_pb2.KeyVerificationAdmin.PROVIDE_SECURITY_NUMBER
    )


@pytest.mark.unit
def test_build_rejects_out_of_range_inputs() -> None:
    """Node numbers and nonce values outside wire ranges must fail."""
    with pytest.raises(ValueError, match="32-bit node number"):
        build_key_verification_admin("initiate", remote_nodenum=0x100000000)
    with pytest.raises(ValueError, match="non-negative"):
        build_key_verification_admin("verify", nonce=-1)


@pytest.mark.unit
def test_send_validates_timeout_and_my_info() -> None:
    """Non-positive timeouts and missing my_info fail before any send."""
    interface = _interface_with_node_num(42)
    request = build_key_verification_admin("initiate", remote_nodenum=7)
    with pytest.raises(ValueError, match="timeout must be positive"):
        send_key_verification(interface, request, timeout=0)
    missing = MagicMock(spec=MeshInterface)
    missing.myInfo = None
    with pytest.raises(RuntimeError, match="my_info"):
        send_key_verification(missing, request, timeout=1.0)


@pytest.mark.unit
def test_send_targets_local_node_over_admin_app() -> None:
    """The handshake request rides ADMIN_APP to the local node, with ACK."""
    interface = _interface_with_node_num(2478223698)
    interface.sendData.side_effect = lambda *args, **kwargs: None
    request = build_key_verification_admin("initiate", remote_nodenum=0xABCD1234)
    with pytest.raises(TimeoutError):
        send_key_verification(interface, request, timeout=0.05)
    payload, target = interface.sendData.call_args[0]
    assert target == 2478223698
    assert payload.key_verification.message_type == (
        admin_pb2.KeyVerificationAdmin.INITIATE_VERIFICATION
    )
    kwargs = interface.sendData.call_args[1]
    assert kwargs["portNum"] == portnums_pb2.PortNum.ADMIN_APP
    assert kwargs["wantAck"] is True


def _inform_notification(
    nonce: int, security_number: int
) -> mesh_pb2.ClientNotification:
    notification = mesh_pb2.ClientNotification()
    notification.key_verification_number_inform.nonce = nonce
    notification.key_verification_number_inform.remote_longname = "Repeater"
    notification.key_verification_number_inform.security_number = security_number
    return notification


@pytest.mark.unit
def test_send_returns_matching_notification_from_pubsub() -> None:
    """A device notification published on the topic resolves the wait."""

    interface = _interface_with_node_num(2478223698)

    def _send(payload: object, target: int, **_kwargs: object) -> None:
        def _reply() -> None:
            pub.sendMessage(
                "meshtastic.clientNotification",
                interface=interface,
                notification=_inform_notification(9, 4242),
            )

        threading.Thread(target=_reply, daemon=True).start()

    interface.sendData.side_effect = _send
    request = build_key_verification_admin("initiate", remote_nodenum=0xABCD1234)
    result = send_key_verification(interface, request, timeout=3.0)
    assert result is not None
    assert result.key_verification_number_inform.security_number == 4242


@pytest.mark.unit
def test_send_ignores_foreign_notifications() -> None:
    """Notifications from other interfaces and other payloads are skipped."""

    interface = _interface_with_node_num(2478223698)
    other = _interface_with_node_num(1)
    published = threading.Event()

    def _send(payload: object, target: int, **_kwargs: object) -> None:
        def _reply() -> None:
            pub.sendMessage(
                "meshtastic.clientNotification",
                interface=other,
                notification=_inform_notification(9, 4242),
            )
            plain = mesh_pb2.ClientNotification()
            plain.message = "unrelated"
            pub.sendMessage(
                "meshtastic.clientNotification",
                interface=interface,
                notification=plain,
            )
            published.set()

        threading.Thread(target=_reply, daemon=True).start()

    def _valid_reply() -> None:
        assert published.wait(timeout=2.0)
        pub.sendMessage(
            "meshtastic.clientNotification",
            interface=interface,
            notification=_inform_notification(3, 1111),
        )

    threading.Thread(target=_valid_reply, daemon=True).start()

    interface.sendData.side_effect = _send
    request = build_key_verification_admin("initiate", remote_nodenum=0xABCD1234)
    result = send_key_verification(interface, request, timeout=3.0)
    assert result is not None
    assert result.key_verification_number_inform.nonce == 3


@pytest.mark.unit
def test_send_times_out_without_notification() -> None:
    """Missing device replies raise TimeoutError after the wait expires."""
    interface = _interface_with_node_num(2478223698)
    request = build_key_verification_admin("verify", nonce=5)
    with pytest.raises(TimeoutError):
        send_key_verification(interface, request, timeout=0.05)


@pytest.mark.unit
def test_send_filters_stale_nonce_for_later_stages() -> None:
    """Later stages only accept notifications for their own handshake."""

    interface = _interface_with_node_num(2478223698)

    def _send(payload: object, target: int, **_kwargs: object) -> None:
        def _reply() -> None:
            pub.sendMessage(
                "meshtastic.clientNotification",
                interface=interface,
                notification=_inform_notification(999, 4242),  # stale nonce
            )

        threading.Thread(target=_reply, daemon=True).start()

    interface.sendData.side_effect = _send
    request = build_key_verification_admin("verify", nonce=5)
    with pytest.raises(TimeoutError):
        send_key_verification(interface, request, timeout=0.2)
