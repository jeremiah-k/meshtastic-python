"""Tests for USB-only firmware lockdown client helpers."""

import os
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from pubsub import pub

from meshtastic.lockdown import (
    LOCKDOWN_STATUS_TOPIC,
    build_lockdown_auth,
    read_lockdown_passphrase_file,
    send_lockdown_auth,
    validate_lockdown_passphrase,
)
from meshtastic.protobuf import mesh_pb2, portnums_pb2
from meshtastic.serial_interface import SerialInterface
from meshtastic.tcp_interface import TCPInterface


@pytest.mark.unit
def test_validate_lockdown_passphrase_enforces_wire_bounds() -> None:
    assert validate_lockdown_passphrase(b"secret") == b"secret"
    with pytest.raises(ValueError, match="1..32"):
        validate_lockdown_passphrase(b"")
    with pytest.raises(ValueError, match="1..32"):
        validate_lockdown_passphrase(b"x" * 33)


@pytest.mark.unit
def test_read_lockdown_passphrase_file_requires_operator_only_permissions(
    tmp_path: Path,
) -> None:
    path = tmp_path / "passphrase"
    path.write_bytes(b"secret\n")
    path.chmod(0o600)
    assert read_lockdown_passphrase_file(path) == b"secret"
    if os.name == "nt":
        return
    path.chmod(0o640)
    with pytest.raises(PermissionError, match="operator-only"):
        read_lockdown_passphrase_file(path)


@pytest.mark.unit
def test_read_lockdown_passphrase_file_strips_only_one_line_ending(
    tmp_path: Path,
) -> None:
    path = tmp_path / "passphrase-multiline"
    path.write_bytes(b"secret\n\n")
    path.chmod(0o600)
    assert read_lockdown_passphrase_file(path) == b"secret\n"


@pytest.mark.unit
def test_build_lockdown_auth_rejects_out_of_range_limits() -> None:
    auth = build_lockdown_auth(
        b"secret", boots_remaining=3, max_session_seconds=90, disable=True
    )
    assert auth.passphrase == b"secret"
    assert auth.boots_remaining == 3
    assert auth.max_session_seconds == 90
    assert auth.disable is True
    with pytest.raises(ValueError, match="0 and 255"):
        build_lockdown_auth(b"secret", boots_remaining=256)


@pytest.mark.unit
def test_send_lockdown_auth_is_serial_only_and_uses_plain_local_admin() -> None:
    tcp = MagicMock(spec=TCPInterface)
    with pytest.raises(ValueError, match="USB-serial only"):
        send_lockdown_auth(tcp, build_lockdown_auth(b"secret"), timeout=0.1)

    serial = MagicMock(spec=SerialInterface)
    serial.myInfo = mesh_pb2.MyNodeInfo(my_node_num=123)

    def _send(*_args: object, **kwargs: object) -> mesh_pb2.MeshPacket:
        assert kwargs["portNum"] == portnums_pb2.PortNum.ADMIN_APP
        assert kwargs["wantAck"] is True
        assert kwargs["pkiEncrypted"] is False
        pub.sendMessage(
            LOCKDOWN_STATUS_TOPIC,
            interface=serial,
            status=mesh_pb2.LockdownStatus(state=mesh_pb2.LockdownStatus.UNLOCKED),
        )
        return mesh_pb2.MeshPacket(id=7)

    serial.sendData.side_effect = _send
    status = send_lockdown_auth(serial, build_lockdown_auth(b"secret"), timeout=0.5)
    assert status is not None
    assert status.state == mesh_pb2.LockdownStatus.UNLOCKED


@pytest.mark.unit
def test_send_lockdown_auth_status_listener_tolerates_extra_keywords(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    serial = MagicMock(spec=SerialInterface)
    serial.myInfo = mesh_pb2.MyNodeInfo(my_node_num=123)
    listeners: list[object] = []

    def _subscribe(listener: object, topic: str) -> None:
        assert topic == LOCKDOWN_STATUS_TOPIC
        listeners.append(listener)

    def _unsubscribe(listener: object, topic: str) -> None:
        assert topic == LOCKDOWN_STATUS_TOPIC
        assert listener in listeners

    monkeypatch.setattr(pub, "subscribe", _subscribe)
    monkeypatch.setattr(pub, "unsubscribe", _unsubscribe)

    def _send(*_args: object, **_kwargs: object) -> mesh_pb2.MeshPacket:
        listener = listeners[-1]
        assert callable(listener)
        listener(
            interface=serial,
            status=mesh_pb2.LockdownStatus(state=mesh_pb2.LockdownStatus.UNLOCKED),
            source="future-publisher",
        )
        return mesh_pb2.MeshPacket(id=8)

    serial.sendData.side_effect = _send
    status = send_lockdown_auth(serial, build_lockdown_auth(b"secret"), timeout=0.5)
    assert status is not None
    assert status.state == mesh_pb2.LockdownStatus.UNLOCKED


@pytest.mark.unit
def test_send_lockdown_auth_times_out_without_status() -> None:
    serial = MagicMock(spec=SerialInterface)
    serial.myInfo = mesh_pb2.MyNodeInfo(my_node_num=123)
    serial.sendData.return_value = mesh_pb2.MeshPacket(id=7)
    with pytest.raises(TimeoutError, match="LockdownStatus"):
        send_lockdown_auth(serial, build_lockdown_auth(b"secret"), timeout=0.01)
