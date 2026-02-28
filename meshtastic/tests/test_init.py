"""Meshtastic unit tests for __init__.py."""

import logging
import re
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, create_autospec

import pytest

import meshtastic
from meshtastic import (
    _on_admin_receive,
    _on_node_info_receive,
    _on_position_receive,
    _on_text_receive,
    _receive_info_update,
    mt_config,
    serial_interface,
)

from ..mesh_interface import MeshInterface
from ..serial_interface import SerialInterface


@pytest.mark.unit
def test_init_serial_alias_points_to_internal_module(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify meshtastic.serial resolves to the internal serial_interface module."""
    monkeypatch.delattr(meshtastic, "serial", raising=False)
    assert meshtastic.serial is serial_interface


@pytest.mark.unit
def test_init_on_text_receive_with_exception(
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Test _on_text_receive."""
    args = MagicMock()
    monkeypatch.setattr(mt_config, "args", args)
    iface = create_autospec(SerialInterface, instance=True)
    packet: dict[str, Any] = {}
    with caplog.at_level(logging.DEBUG):
        _on_text_receive(iface, packet)
    assert re.search(r"in _on_text_receive", caplog.text, re.MULTILINE)
    assert re.search(r"Malformatted", caplog.text, re.MULTILINE)


@pytest.mark.unit
def test_init_on_position_receive(
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Test _on_position_receive."""
    args = MagicMock()
    monkeypatch.setattr(mt_config, "args", args)
    iface = create_autospec(SerialInterface, instance=True)
    packet = {"from": "foo", "decoded": {"position": {}}}
    with caplog.at_level(logging.DEBUG):
        _on_position_receive(iface, packet)
    assert re.search(r"in _on_position_receive", caplog.text, re.MULTILINE)
    iface._get_or_create_by_num.assert_not_called()


@pytest.mark.unit
def test_init_on_node_info_receive(
    caplog: pytest.LogCaptureFixture,
    iface_with_nodes: MeshInterface,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Test _on_node_info_receive."""
    args = MagicMock()
    monkeypatch.setattr(mt_config, "args", args)
    iface = iface_with_nodes
    assert iface.myInfo is not None
    iface.myInfo.my_node_num = 2475227164
    packet = {
        "from": 4808675309,
        "decoded": {
            "user": {
                "id": "bar",
            },
        },
    }
    with caplog.at_level(logging.DEBUG):
        _on_node_info_receive(iface, packet)
    assert re.search(r"in _on_node_info_receive", caplog.text, re.MULTILINE)
    node = iface._get_or_create_by_num(4808675309)
    assert node["user"]["id"] == "bar"
    assert iface.nodes is not None
    assert iface.nodes["bar"] is node
    assert node["lastReceived"]["decoded"]["user"]["id"] == "bar"


@pytest.mark.unit
def test_init_on_admin_receive_redacts_last_received(
    iface_with_nodes: MeshInterface,
) -> None:
    """Admin packets should store redacted lastReceived while keeping passkey state."""
    iface = iface_with_nodes
    packet: dict[str, Any] = {
        "from": 4808675309,
        "decoded": {
            "admin": {
                "raw": SimpleNamespace(session_passkey=b"abc123"),
            }
        },
        "rxTime": 100,
        "rxSnr": 5.0,
        "hopLimit": 3,
    }

    _on_admin_receive(iface, packet)

    node = iface._get_or_create_by_num(4808675309)
    assert node["adminSessionPassKey"] == b"abc123"
    assert node["lastReceived"]["decoded"]["admin"] == "<redacted>"
    # Input packet should remain unchanged for callback consumers.
    assert packet["decoded"]["admin"]["raw"].session_passkey == b"abc123"


@pytest.mark.unit
def test_init_receive_info_update_keeps_non_admin_packet_object(
    iface_with_nodes: MeshInterface,
) -> None:
    """Non-admin lastReceived cache should be a shallow copy of the packet."""
    iface = iface_with_nodes
    packet: dict[str, Any] = {
        "from": 4808675309,
        "decoded": {"text": "hello"},
        "rxTime": 101,
    }

    _receive_info_update(iface, packet)

    node = iface._get_or_create_by_num(4808675309)
    # lastReceived is always a shallow copy (not the same object) to avoid aliasing
    assert node["lastReceived"] == packet
    assert node["lastReceived"] is not packet


@pytest.mark.unit
def test_init_getattr_raises_for_unknown_attribute() -> None:
    """Verify __getattr__ raises AttributeError for unknown attributes."""
    with pytest.raises(
        AttributeError, match="module 'meshtastic' has no attribute 'nonexistent'"
    ):
        _ = meshtastic.nonexistent


@pytest.mark.unit
def test_init_getattr_caches_serial_on_first_access(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify __getattr__ caches serial module on first access."""
    # Isolate module-level cache mutation to this test and auto-restore afterwards.
    # `raising=False` allows this test to run whether meshtastic.serial is
    # already cached or not.
    monkeypatch.delattr(meshtastic, "serial", raising=False)

    # First access should trigger lazy load.
    serial = meshtastic.serial
    assert serial is serial_interface

    # Second access should use cached value.
    serial2 = meshtastic.serial
    assert serial2 is serial
    assert serial2 is serial_interface
