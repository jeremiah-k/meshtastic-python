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
    assert meshtastic.serial.SerialInterface is serial_interface.SerialInterface


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
