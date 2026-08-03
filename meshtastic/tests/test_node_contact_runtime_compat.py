"""Compatibility seams for the extracted Node contact runtime."""

from unittest.mock import MagicMock, patch

import pytest

import meshtastic.node as node_module
from meshtastic.mesh_interface import MeshInterface
from meshtastic.node import Node


def _node_with_contact(node_num: int, *, macaddr: str = "AQIDBAUG") -> Node:
    """Build a Node with one contact-shaped NodeDB entry."""
    iface = MagicMock(autospec=MeshInterface)
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
