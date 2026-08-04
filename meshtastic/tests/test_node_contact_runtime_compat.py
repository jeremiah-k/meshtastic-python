"""Compatibility seams for the extracted Node contact runtime."""

import base64
from unittest.mock import create_autospec, patch

import pytest

import meshtastic.node as node_module
from meshtastic.mesh_interface import MeshInterface
from meshtastic.node import Node
from meshtastic.node_runtime.contact_runtime import decode_node_bytes_field
from meshtastic.protobuf import admin_pb2


def _node_with_contact(node_num: int, *, macaddr: str = "AQIDBAUG") -> Node:
    """Build a Node with one contact-shaped NodeDB entry."""
    iface = create_autospec(MeshInterface, instance=True)
    iface.nodesByNum = {
        node_num: {
            "num": node_num,
            "user": {
                "id": f"!{node_num:08x}",
                "longName": "Contact",
                "shortName": "CT",
                "macaddr": macaddr,
            },
        }
    }
    iface.localNode = None
    return Node(iface, node_num, noProto=True)


@pytest.mark.unit
def test_get_contact_url_preserves_node_module_decoder_monkeypatch() -> None:
    """The public Node facade must keep the historical private decoder seam."""
    target = _node_with_contact(0x12345678, macaddr="not-valid-base64")

    with patch.object(node_module, "_decode_node_bytes_field", return_value=b"123456"):
        url = target.getContactURL(0x12345678)

    assert url.startswith("https://meshtastic.org/v/#")


@pytest.mark.unit
def test_add_contact_url_preserves_node_module_payload_limit_monkeypatch() -> None:
    """The public Node facade must keep the historical payload-limit seam."""
    source = _node_with_contact(0x12345678)
    url = source.getContactURL(0x12345678)
    target = _node_with_contact(0x87654321)

    with patch.object(node_module, "_MAX_CONTACT_URL_PAYLOAD", 1):
        with pytest.raises(MeshInterface.MeshInterfaceError, match="Contact URL fragment too large"):
            target.addContactURL(url)


@pytest.mark.unit
def test_decode_node_bytes_field_preserves_raw_bytes() -> None:
    """Raw NodeDB bytes should pass through without base64 decoding."""
    raw = b"\x00\x01contact"

    assert decode_node_bytes_field(raw) is raw


@pytest.mark.unit
@pytest.mark.parametrize("field_name", ["macaddr", "publicKey"])
def test_get_contact_url_rejects_malformed_node_db_byte_fields(field_name: str) -> None:
    """Malformed NodeDB byte fields should fail with field-specific diagnostics."""
    target = _node_with_contact(0x12345678)
    nodes_by_num = target.iface.nodesByNum
    assert nodes_by_num is not None
    nodes_by_num[0x12345678]["user"][field_name] = "not-base64!"

    with pytest.raises(MeshInterface.MeshInterfaceError, match=f"Invalid {field_name}"):
        target.getContactURL(0x12345678)


@pytest.mark.unit
def test_get_contact_url_accepts_numeric_enum_values() -> None:
    """Numeric hardware-model and role values should survive contact export."""
    target = _node_with_contact(0x12345678)
    nodes_by_num = target.iface.nodesByNum
    assert nodes_by_num is not None
    user = nodes_by_num[0x12345678]["user"]
    user["hwModel"] = 1
    user["role"] = 1

    url = target.getContactURL(0x12345678)
    fragment = url.split("#", maxsplit=1)[1]
    fragment += "=" * (-len(fragment) % 4)
    contact = admin_pb2.SharedContact()
    contact.ParseFromString(base64.urlsafe_b64decode(fragment))

    assert contact.user.hw_model == 1
    assert contact.user.role == 1


@pytest.mark.unit
def test_decode_contact_rejects_payload_over_decoded_limit() -> None:
    """Decoded payload size should be checked even when the encoded guard permits it."""
    target = _node_with_contact(0x12345678)
    encoded = base64.urlsafe_b64encode(b"1234").decode("ascii").rstrip("=")

    with pytest.raises(MeshInterface.MeshInterfaceError, match="payload too large"):
        target._contact_runtime._decode_contact(  # noqa: SLF001
            f"https://meshtastic.org/v/#{encoded}",
            max_payload=3,
        )


@pytest.mark.unit
def test_decode_contact_reports_malformed_protobuf_payload() -> None:
    """Valid base64 containing invalid protobuf wire data should report parse failure."""
    target = _node_with_contact(0x12345678)
    encoded = base64.urlsafe_b64encode(b"\xff").decode("ascii").rstrip("=")

    with pytest.raises(MeshInterface.MeshInterfaceError, match="Failed to parse"):
        target._contact_runtime._decode_contact(  # noqa: SLF001
            f"https://meshtastic.org/v/#{encoded}"
        )
