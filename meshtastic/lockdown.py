"""USB-only client helpers for firmware lockdown provisioning and control."""

from __future__ import annotations

import os
import stat
import threading
from pathlib import Path

from pubsub import pub

from meshtastic._topics import LOCKDOWN_STATUS_TOPIC
from meshtastic.protobuf import admin_pb2, mesh_pb2, portnums_pb2
from meshtastic.serial_interface import SerialInterface

DEFAULT_LOCKDOWN_TIMEOUT_SECONDS = 20.0


def validate_lockdown_passphrase(passphrase: bytes) -> bytes:
    """Validate and defensively copy a lockdown passphrase."""
    value = bytes(passphrase)
    if not 1 <= len(value) <= 32:
        raise ValueError("lockdown passphrase must be 1..32 bytes")
    return value


def read_lockdown_passphrase_file(path: str | os.PathLike[str]) -> bytes:
    """Read an operator-only passphrase file, refusing group/world access."""
    target = Path(path)
    mode = stat.S_IMODE(target.stat().st_mode)
    if os.name != "nt" and mode & 0o077:
        raise PermissionError(
            f"{target} mode is {oct(mode)}; lockdown passphrase files must be operator-only (0600)"
        )
    value = target.read_bytes()
    if value.endswith(b"\r\n"):
        value = value[:-2]
    elif value.endswith(b"\n"):
        value = value[:-1]
    return validate_lockdown_passphrase(value)


def _require_usb_serial(interface: object) -> SerialInterface:
    """Reject BLE/TCP transports because the passphrase is cleartext on wire."""
    if not isinstance(interface, SerialInterface):
        raise ValueError(
            "lockdown authentication is USB-serial only; refusing BLE/TCP transport"
        )
    return interface


def build_lockdown_auth(
    passphrase: bytes = b"",
    *,
    boots_remaining: int = 0,
    valid_until_epoch: int = 0,
    max_session_seconds: int = 0,
    lock_now: bool = False,
    disable: bool = False,
) -> admin_pb2.LockdownAuth:
    """Build a bounded LockdownAuth request without silently truncating input."""
    if passphrase:
        passphrase = validate_lockdown_passphrase(passphrase)
    if not 0 <= boots_remaining <= 255:
        raise ValueError("boots_remaining must be between 0 and 255")
    if valid_until_epoch < 0 or max_session_seconds < 0:
        raise ValueError("lockdown time limits must not be negative")
    auth = admin_pb2.LockdownAuth(
        passphrase=passphrase,
        boots_remaining=boots_remaining,
        valid_until_epoch=valid_until_epoch,
        max_session_seconds=max_session_seconds,
        lock_now=lock_now,
        disable=disable,
    )
    return auth


def send_lockdown_auth(
    interface: object,
    auth: admin_pb2.LockdownAuth,
    *,
    timeout: float = DEFAULT_LOCKDOWN_TIMEOUT_SECONDS,
    allow_reboot_without_status: bool = False,
) -> mesh_pb2.LockdownStatus | None:
    """Send one local USB lockdown command and await its structured status."""
    serial_interface = _require_usb_serial(interface)
    if timeout <= 0:
        raise ValueError("timeout must be positive")
    my_info = serial_interface.myInfo
    if my_info is None:
        raise RuntimeError("device did not provide my_info")

    event = threading.Event()
    result: list[mesh_pb2.LockdownStatus] = []

    def _on_status(
        *, interface: object, status: mesh_pb2.LockdownStatus, **_kwargs: object
    ) -> None:
        if interface is not target_interface:
            return
        copied = mesh_pb2.LockdownStatus()
        copied.CopyFrom(status)
        result.append(copied)
        event.set()

    target_interface = serial_interface
    pub.subscribe(_on_status, LOCKDOWN_STATUS_TOPIC)
    try:
        admin = admin_pb2.AdminMessage()
        admin.lockdown_auth.CopyFrom(auth)
        serial_interface.sendData(
            admin,
            my_info.my_node_num,
            portNum=portnums_pb2.PortNum.ADMIN_APP,
            wantAck=True,
            channelIndex=0,
            pkiEncrypted=False,
            priority=mesh_pb2.MeshPacket.Priority.RELIABLE,
        )
        if event.wait(timeout):
            return result[-1]
        if allow_reboot_without_status:
            return None
        raise TimeoutError("no LockdownStatus received before timeout")
    finally:
        pub.unsubscribe(_on_status, LOCKDOWN_STATUS_TOPIC)
