"""Meshtastic unit tests for __init__.py."""

import logging
import re
from unittest.mock import MagicMock

import pytest

import meshtastic
from meshtastic import (
    _on_node_info_receive,
    _on_position_receive,
    _on_text_receive,
    mt_config,
    serial_interface,
)

from ..serial_interface import SerialInterface


@pytest.mark.unit
def test_init_serial_alias_points_to_internal_module():
    """Verify meshtastic.serial resolves to the internal serial_interface module."""
    assert meshtastic.serial is serial_interface


@pytest.mark.unit
def test_init_on_text_receive_with_exception(caplog):
    """Test _on_text_receive."""
    args = MagicMock()
    mt_config.args = args
    iface = MagicMock(autospec=SerialInterface)
    packet = {}
    with caplog.at_level(logging.DEBUG):
        _on_text_receive(iface, packet)
    assert re.search(r"in _on_text_receive", caplog.text, re.MULTILINE)
    assert re.search(r"Malformatted", caplog.text, re.MULTILINE)


@pytest.mark.unit
def test_init_on_position_receive(caplog):
    """Test _on_position_receive."""
    args = MagicMock()
    mt_config.args = args
    iface = MagicMock(autospec=SerialInterface)
    packet = {"from": "foo", "decoded": {"position": {}}}
    with caplog.at_level(logging.DEBUG):
        _on_position_receive(iface, packet)
    assert re.search(r"in _on_position_receive", caplog.text, re.MULTILINE)


@pytest.mark.unit
def test_init_on_node_info_receive(caplog, iface_with_nodes):
    """Test _on_node_info_receive."""
    args = MagicMock()
    mt_config.args = args
    iface = iface_with_nodes
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


@pytest.mark.unit
def test_init_getattr_raises_for_unknown_attribute():
    """Verify __getattr__ raises AttributeError for unknown attributes."""
    with pytest.raises(
        AttributeError, match="module 'meshtastic' has no attribute 'nonexistent'"
    ):
        _ = meshtastic.nonexistent


@pytest.mark.unit
def test_init_getattr_caches_serial_on_first_access():
    """Verify __getattr__ caches serial module on first access."""
    sentinel = object()
    original = getattr(meshtastic, "serial", sentinel)

    if original is not sentinel:
        delattr(meshtastic, "serial")
    try:
        # First access should trigger lazy load
        serial = meshtastic.serial
        assert serial is serial_interface

        # Second access should use cached value
        serial2 = meshtastic.serial
        assert serial2 is serial
        assert serial2 is serial_interface
    finally:
        if original is sentinel:
            if hasattr(meshtastic, "serial"):
                delattr(meshtastic, "serial")
        else:
            setattr(meshtastic, "serial", original)
