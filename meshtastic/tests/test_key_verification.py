"""Tests for the firmware 2.8 key-verification handshake helpers."""

import threading
from unittest.mock import MagicMock

import pytest
from pubsub import pub

from meshtastic._topics import CLIENT_NOTIFICATION_TOPIC
from meshtastic.key_verification import (
    build_key_verification_admin,
    send_key_verification,
)
from meshtastic.mesh_interface import MeshInterface
from meshtastic.protobuf import admin_pb2, mesh_pb2, portnums_pb2


def _interface_with_node_num(node_num: int) -> MagicMock:
    """Build a MeshInterface double that reports one local node number."""
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
def test_build_provide_requires_six_digit_security_number() -> None:
    """The provide stage validates presence and range of the security number."""
    with pytest.raises(ValueError, match="provide requires"):
        build_key_verification_admin("provide", nonce=5)
    with pytest.raises(ValueError, match="six digits"):
        build_key_verification_admin("provide", nonce=5, security_number=0)
    with pytest.raises(ValueError, match="six digits"):
        build_key_verification_admin("provide", nonce=5, security_number=1000000)
    message = build_key_verification_admin("provide", nonce=5, security_number=424242)
    assert message.security_number == 424242
    assert (
        message.message_type == admin_pb2.KeyVerificationAdmin.PROVIDE_SECURITY_NUMBER
    )


@pytest.mark.unit
def test_build_rejects_out_of_range_inputs() -> None:
    """Node numbers and nonce values outside wire ranges must fail."""
    with pytest.raises(ValueError, match="32-bit node number"):
        build_key_verification_admin("initiate", remote_nodenum=0x100000000)
    with pytest.raises(ValueError, match="unsigned 64-bit"):
        build_key_verification_admin("verify", nonce=-1)
    with pytest.raises(ValueError, match="unsigned 64-bit"):
        build_key_verification_admin("verify", nonce=0x10000000000000000)


@pytest.mark.unit
def test_send_validates_timeout_and_my_info() -> None:
    """Non-positive timeouts and missing my_info fail before any send."""
    interface = _interface_with_node_num(42)
    request = build_key_verification_admin("initiate", remote_nodenum=7)
    with pytest.raises(ValueError, match="finite and positive"):
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


def _number_request_notification(nonce: int) -> mesh_pb2.ClientNotification:
    """Build a security-number-request notification for one handshake nonce."""
    notification = mesh_pb2.ClientNotification()
    notification.key_verification_number_request.nonce = nonce
    notification.key_verification_number_request.remote_longname = "Repeater"
    return notification


def _final_notification(nonce: int) -> mesh_pb2.ClientNotification:
    """Build a final key-verification notification for one handshake nonce."""
    notification = mesh_pb2.ClientNotification()
    notification.key_verification_final.nonce = nonce
    notification.key_verification_final.remote_longname = "Repeater"
    return notification


@pytest.mark.unit
def test_send_returns_matching_notification_from_pubsub() -> None:
    """A device notification published on the topic resolves the wait."""

    interface = _interface_with_node_num(2478223698)

    def _send(payload: object, target: int, **_kwargs: object) -> None:
        def _reply() -> None:
            pub.sendMessage(
                CLIENT_NOTIFICATION_TOPIC,
                interface=interface,
                notification=_number_request_notification(9),
            )

        threading.Thread(target=_reply, daemon=True).start()

    interface.sendData.side_effect = _send
    request = build_key_verification_admin("initiate", remote_nodenum=0xABCD1234)
    result = send_key_verification(interface, request, timeout=3.0)
    assert result is not None
    assert result.key_verification_number_request.nonce == 9


@pytest.mark.unit
def test_send_ignores_foreign_notifications() -> None:
    """Notifications from other interfaces and other payloads are skipped."""

    interface = _interface_with_node_num(2478223698)
    other = _interface_with_node_num(1)
    published = threading.Event()

    def _send(payload: object, target: int, **_kwargs: object) -> None:
        def _reply() -> None:
            pub.sendMessage(
                CLIENT_NOTIFICATION_TOPIC,
                interface=other,
                notification=_number_request_notification(9),
            )
            plain = mesh_pb2.ClientNotification()
            plain.message = "unrelated"
            pub.sendMessage(
                CLIENT_NOTIFICATION_TOPIC,
                interface=interface,
                notification=plain,
            )
            published.set()

        threading.Thread(target=_reply, daemon=True).start()

    def _valid_reply() -> None:
        assert published.wait(timeout=2.0)
        pub.sendMessage(
            CLIENT_NOTIFICATION_TOPIC,
            interface=interface,
            notification=_number_request_notification(3),
        )

    threading.Thread(target=_valid_reply, daemon=True).start()

    interface.sendData.side_effect = _send
    request = build_key_verification_admin("initiate", remote_nodenum=0xABCD1234)
    result = send_key_verification(interface, request, timeout=3.0)
    assert result is not None
    assert result.key_verification_number_request.nonce == 3


@pytest.mark.unit
@pytest.mark.parametrize("stage", ["verify", "no-verify"])
def test_send_decision_stage_returns_without_notification(stage: str) -> None:
    """Firmware decision stages return after sending because they emit no reply."""
    interface = _interface_with_node_num(2478223698)
    request = build_key_verification_admin(stage, nonce=5)

    assert send_key_verification(interface, request, timeout=0.05) is None
    interface.sendData.assert_called_once()


@pytest.mark.unit
def test_send_filters_stale_nonce_for_later_stages() -> None:
    """Later stages only accept notifications for their own handshake."""

    interface = _interface_with_node_num(2478223698)

    def _send(payload: object, target: int, **_kwargs: object) -> None:
        def _reply() -> None:
            pub.sendMessage(
                CLIENT_NOTIFICATION_TOPIC,
                interface=interface,
                notification=_final_notification(999),  # stale nonce
            )

        threading.Thread(target=_reply, daemon=True).start()

    interface.sendData.side_effect = _send
    request = build_key_verification_admin("provide", nonce=5, security_number=424242)
    with pytest.raises(TimeoutError):
        send_key_verification(interface, request, timeout=0.2)


@pytest.mark.unit
def test_send_ignores_wrong_key_verification_variant() -> None:
    """A same-nonce notification from another stage cannot resolve the wait."""
    interface = _interface_with_node_num(2478223698)

    def _send(payload: object, target: int, **_kwargs: object) -> None:
        _ = (payload, target)

        def _reply() -> None:
            pub.sendMessage(
                CLIENT_NOTIFICATION_TOPIC,
                interface=interface,
                notification=_number_request_notification(5),
            )

        threading.Thread(target=_reply, daemon=True).start()

    interface.sendData.side_effect = _send
    request = build_key_verification_admin("provide", nonce=5, security_number=424242)
    with pytest.raises(TimeoutError):
        send_key_verification(interface, request, timeout=0.2)


@pytest.mark.unit
@pytest.mark.parametrize("timeout", [float("inf"), float("-inf"), float("nan")])
def test_send_rejects_non_finite_timeout(timeout: float) -> None:
    """Non-finite waits cannot become an accidental infinite/blocking CLI session."""
    interface = _interface_with_node_num(42)
    request = build_key_verification_admin("initiate", remote_nodenum=7)

    with pytest.raises(ValueError, match="finite and positive"):
        send_key_verification(interface, request, timeout=timeout)

    interface.sendData.assert_not_called()


@pytest.mark.unit
def test_notification_nonce_returns_none_for_unrelated_notification() -> None:
    """The nonce helper explicitly identifies notifications outside the handshake."""
    from meshtastic.key_verification import _notification_nonce

    assert _notification_nonce(mesh_pb2.ClientNotification(message="other")) is None


@pytest.mark.unit
def test_send_ignores_expected_variant_when_nonce_extraction_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A malformed expected notification cannot complete the handshake wait."""
    interface = _interface_with_node_num(2478223698)

    def _send(payload: object, target: int, **_kwargs: object) -> None:
        _ = (payload, target)
        pub.sendMessage(
            CLIENT_NOTIFICATION_TOPIC,
            interface=interface,
            notification=_number_request_notification(9),
        )

    interface.sendData.side_effect = _send
    monkeypatch.setattr(
        "meshtastic.key_verification._notification_nonce", lambda _n: None
    )
    request = build_key_verification_admin("initiate", remote_nodenum=0xABCD1234)

    with pytest.raises(TimeoutError):
        send_key_verification(interface, request, timeout=0.02)
