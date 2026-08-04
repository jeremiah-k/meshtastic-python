"""Contact URL serialization, validation, and import runtime for :class:`Node`."""

from __future__ import annotations

import base64
import binascii
from collections.abc import Callable
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

import google.protobuf.message

from meshtastic.protobuf import admin_pb2, config_pb2, mesh_pb2
from meshtastic.util import toNodeNum

if TYPE_CHECKING:
    from meshtastic.node import Node


MAX_CONTACT_URL_PAYLOAD = 4096


def _decode_node_bytes_field(value: str | bytes) -> bytes:
    """Decode a NodeDB byte field stored as base64 text or raw bytes.

    Parameters
    ----------
    value : str | bytes
        NodeDB field value to decode.

    Returns
    -------
    bytes
        Raw field bytes.

    Notes
    -----
    Only base64 syntax is validated here. Firmware remains responsible for
    enforcing nanopb field-size limits when imported contact data is applied.
    """
    if isinstance(value, bytes):
        return value
    return base64.b64decode(value, validate=True)


class _NodeContactRuntime:
    """Own contact URL generation, validation, and admin import orchestration."""

    def __init__(self, node: "Node") -> None:
        self._node = node

    def _read_user_snapshot(self, node_num: int) -> dict[str, Any] | None:
        """Return a detached user mapping from the NodeDB under its lock."""

        def _snapshot() -> dict[str, Any] | None:
            nodes_by_num = self._node.iface.nodesByNum
            node = nodes_by_num.get(node_num) if nodes_by_num else None
            if not isinstance(node, dict):
                return None
            user = node.get("user")
            if not isinstance(user, dict):
                return None
            return dict(user)

        return self._node._execute_with_node_db_lock(_snapshot)  # noqa: SLF001

    def get_contact_url(
        self,
        node_id: int | str,
        *,
        should_ignore: bool = False,
        manually_verified: bool = False,
        decode_bytes_field: Callable[[str | bytes], bytes] = _decode_node_bytes_field,
    ) -> str:
        """Build a shareable contact URL from the current NodeDB snapshot."""
        node_num = toNodeNum(node_id)
        if node_num == 0 or node_num >= 0xFFFFFFFF:
            self._node._raise_interface_error(  # noqa: SLF001
                f"Invalid node number for contact: {node_num}"
            )

        user = self._read_user_snapshot(node_num)
        if not user:
            self._node._raise_interface_error(  # noqa: SLF001
                f"Node {node_id} not found in NodeDB"
            )

        user_id = user.get("id")
        if not isinstance(user_id, str) or not user_id:
            self._node._raise_interface_error(  # noqa: SLF001
                f"Node {node_id} has no usable user ID in NodeDB"
            )

        contact = admin_pb2.SharedContact()
        contact.node_num = node_num
        contact.user.id = user_id

        if user.get("macaddr"):
            try:
                contact.user.macaddr = decode_bytes_field(user["macaddr"])
            except (binascii.Error, ValueError) as exc:
                self._node._raise_interface_error(  # noqa: SLF001
                    f"Invalid macaddr in NodeDB for {node_id}: {exc}"
                )
        if user.get("longName"):
            contact.user.long_name = user["longName"]
        if user.get("shortName"):
            contact.user.short_name = user["shortName"]
        if user.get("hwModel") and user["hwModel"] != "UNSET":
            hw_model = user["hwModel"]
            # Unknown enum names from newer firmware are intentionally omitted so
            # core contact fields remain forward compatible.
            if isinstance(hw_model, str):
                try:
                    contact.user.hw_model = mesh_pb2.HardwareModel.Value(hw_model)
                except ValueError:
                    pass
            elif isinstance(hw_model, int):
                contact.user.hw_model = hw_model  # type: ignore[assignment]
        if user.get("role"):
            role = user["role"]
            if isinstance(role, str):
                try:
                    contact.user.role = config_pb2.Config.DeviceConfig.Role.Value(role)
                except ValueError:
                    pass
            elif isinstance(role, int):
                contact.user.role = role  # type: ignore[assignment]
        if user.get("publicKey"):
            try:
                contact.user.public_key = decode_bytes_field(user["publicKey"])
            except (binascii.Error, ValueError) as exc:
                self._node._raise_interface_error(  # noqa: SLF001
                    f"Invalid publicKey in NodeDB for {node_id}: {exc}"
                )
        if user.get("isLicensed"):
            contact.user.is_licensed = user["isLicensed"]
        if user.get("isUnmessagable") is not None:
            contact.user.is_unmessagable = user["isUnmessagable"]
        if should_ignore:
            contact.should_ignore = True
        if manually_verified:
            contact.manually_verified = True

        encoded = base64.urlsafe_b64encode(contact.SerializeToString()).decode("ascii")
        return f"https://meshtastic.org/v/#{encoded.rstrip('=')}"

    def _decode_contact(
        self, url: str, *, max_payload: int = MAX_CONTACT_URL_PAYLOAD
    ) -> admin_pb2.SharedContact:
        """Decode and validate the SharedContact payload carried by ``url``."""
        fragment = urlparse(url).fragment
        if not fragment:
            self._node._raise_interface_error(f"Invalid URL '{url}'")  # noqa: SLF001

        max_encoded_fragment = (max_payload // 3 + 1) * 4 + 4
        if len(fragment) > max_encoded_fragment:
            self._node._raise_interface_error(  # noqa: SLF001
                f"Contact URL fragment too large ({len(fragment)} chars)"
            )

        encoded = fragment
        missing_padding = len(encoded) % 4
        if missing_padding:
            encoded += "=" * (4 - missing_padding)

        try:
            decoded = base64.b64decode(encoded, altchars=b"-_", validate=True)
        except (binascii.Error, ValueError) as exc:
            self._node._raise_interface_error(  # noqa: SLF001
                f"Failed to decode contact URL: {exc}"
            )

        if len(decoded) > max_payload:
            self._node._raise_interface_error(  # noqa: SLF001
                f"Contact URL payload too large ({len(decoded)} bytes, "
                f"max {max_payload})"
            )

        try:
            contact = admin_pb2.SharedContact()
            contact.ParseFromString(decoded)
        except google.protobuf.message.DecodeError as exc:
            self._node._raise_interface_error(  # noqa: SLF001
                f"Failed to parse contact URL: {exc}"
            )

        if contact.node_num == 0 or contact.node_num >= 0xFFFFFFFF:
            self._node._raise_interface_error(  # noqa: SLF001
                f"Invalid node number in contact: {contact.node_num}"
            )
        if not contact.HasField("user"):
            self._node._raise_interface_error(  # noqa: SLF001
                "Contact URL contains no user data"
            )
        if not contact.user.id:
            self._node._raise_interface_error(  # noqa: SLF001
                "Contact URL contains no user ID"
            )
        return contact

    def add_contact_url(
        self, url: str, *, max_payload: int = MAX_CONTACT_URL_PAYLOAD
    ) -> mesh_pb2.MeshPacket | None:
        """Decode ``url`` and send its contact through the existing admin path."""
        contact = self._decode_contact(url, max_payload=max_payload)
        self._node.ensureSessionKey()

        message = admin_pb2.AdminMessage()
        message.add_contact.CopyFrom(contact)

        on_response = (
            self._node.onAckNak if self._node != self._node.iface.localNode else None
        )
        request = self._node._send_admin(  # noqa: SLF001
            message,
            onResponse=on_response,
        )
        if on_response is not None and request is not None:
            self._node.iface.waitForAckNak()
        return request
