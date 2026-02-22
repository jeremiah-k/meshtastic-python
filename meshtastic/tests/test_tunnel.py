"""Meshtastic unit tests for tunnel.py."""

import logging
import re
import sys
from collections.abc import Generator
from unittest.mock import MagicMock, patch

import pytest

from meshtastic import mt_config

from ..tcp_interface import TCPInterface

try:
    # Depends upon pytap2, not installed by default
    from ..tunnel import Tunnel, onTunnelReceive
except ImportError:
    pytest.skip("Can't import Tunnel or onTunnelReceive", allow_module_level=True)

pytestmark = pytest.mark.usefixtures("platform_socket_mocks")


@pytest.fixture(autouse=True)
def reset_tunnel_mt_config_state() -> Generator[None, None, None]:
    """Reset mt_config module state before and after each tunnel test."""
    mt_config.reset()
    yield
    mt_config.reset()


@pytest.fixture(name="platform_socket_mocks")
def _platform_socket_mocks() -> Generator[tuple[MagicMock, MagicMock], None, None]:
    """Patch platform.system and socket.socket for tunnel tests."""
    with patch("platform.system", return_value="Linux") as platform_mock:
        with patch("socket.socket") as socket_mock:
            yield platform_mock, socket_mock


@pytest.mark.unit
def test_Tunnel_on_non_linux_system(platform_socket_mocks):
    """Test that we cannot instantiate a Tunnel on a non Linux system."""
    mock_platform_system, _ = platform_socket_mocks
    mock_platform_system.return_value = "notLinux"
    with pytest.raises(Tunnel.TunnelError) as pytest_wrapped_e:
        iface = TCPInterface(hostname="localhost", noProto=True)
        Tunnel(iface)
    assert pytest_wrapped_e.type == Tunnel.TunnelError


@pytest.mark.unit
def test_Tunnel_without_interface():
    """Test that we can not instantiate a Tunnel without a valid interface."""
    with pytest.raises(Tunnel.TunnelError) as pytest_wrapped_e:
        Tunnel(None)
    assert pytest_wrapped_e.type == Tunnel.TunnelError


@pytest.mark.unitslow
def test_Tunnel_with_interface(caplog, iface_with_nodes):
    """Test that Tunnel initializes with a valid interface and registers itself."""
    iface = iface_with_nodes
    iface.myInfo.my_node_num = 2475227164
    with caplog.at_level(logging.WARNING):
        tun = Tunnel(iface)
        try:
            assert tun == mt_config.tunnel_instance
        finally:
            tun.close()
    assert re.search(r"Not creating a TapDevice()", caplog.text, re.MULTILINE)
    assert re.search(r"Not starting TUN reader", caplog.text, re.MULTILINE)


@pytest.mark.unitslow
def test_onTunnelReceive_from_ourselves(caplog, iface_with_nodes):
    """Test onTunnelReceive."""
    iface = iface_with_nodes
    iface.myInfo.my_node_num = 2475227164
    sys.argv = [""]
    mt_config.args = sys.argv
    packet = {"decoded": {"payload": "foo"}, "from": 2475227164}
    with caplog.at_level(logging.DEBUG):
        Tunnel(iface)
        onTunnelReceive(packet, iface)
    assert re.search(r"in onTunnelReceive", caplog.text, re.MULTILINE)
    assert re.search(r"Ignoring message we sent", caplog.text, re.MULTILINE)


@pytest.mark.unit
def test_onTunnelReceive_from_someone_else(caplog, iface_with_nodes):
    """Test onTunnelReceive."""
    iface = iface_with_nodes
    iface.myInfo.my_node_num = 2475227164
    sys.argv = [""]
    mt_config.args = sys.argv
    packet = {"decoded": {"payload": "foo"}, "from": 123}
    with caplog.at_level(logging.DEBUG):
        Tunnel(iface)
        onTunnelReceive(packet, iface)
    assert re.search(r"in onTunnelReceive", caplog.text, re.MULTILINE)


@pytest.mark.unitslow
def test_shouldFilterPacket_random(caplog, iface_with_nodes):
    """Test _shouldFilterPacket()."""
    iface = iface_with_nodes
    iface.noProto = True
    # random packet
    packet = b"1234567890123456789012345678901234567890"
    with caplog.at_level(logging.DEBUG):
        tun = Tunnel(iface)
        ignore = tun._shouldFilterPacket(packet)
        assert not ignore


@pytest.mark.unitslow
def test_shouldFilterPacket_in_blacklist(caplog, iface_with_nodes):
    """Test _shouldFilterPacket()."""
    iface = iface_with_nodes
    iface.noProto = True
    # faked IGMP
    packet = b"\x00\x00\x00\x00\x00\x00\x00\x00\x00\x02\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00"
    with caplog.at_level(logging.DEBUG):
        tun = Tunnel(iface)
        ignore = tun._shouldFilterPacket(packet)
        assert ignore


@pytest.mark.unitslow
def test_shouldFilterPacket_icmp(caplog, iface_with_nodes):
    """Test _shouldFilterPacket()."""
    iface = iface_with_nodes
    iface.noProto = True
    # faked ICMP
    packet = b"\x00\x00\x00\x00\x00\x00\x00\x00\x00\x01\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00"
    with caplog.at_level(logging.DEBUG):
        tun = Tunnel(iface)
        ignore = tun._shouldFilterPacket(packet)
        assert re.search(r"forwarding ICMP message", caplog.text, re.MULTILINE)
        assert not ignore


@pytest.mark.unit
def test_shouldFilterPacket_udp(caplog, iface_with_nodes):
    """Test _shouldFilterPacket()."""
    iface = iface_with_nodes
    iface.noProto = True
    # faked UDP
    packet = b"\x00\x00\x00\x00\x00\x00\x00\x00\x00\x11\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00"
    with caplog.at_level(logging.DEBUG):
        tun = Tunnel(iface)
        ignore = tun._shouldFilterPacket(packet)
        assert re.search(r"forwarding udp", caplog.text, re.MULTILINE)
        assert not ignore


@pytest.mark.unitslow
def test_shouldFilterPacket_udp_blacklisted(caplog, iface_with_nodes):
    """Test _shouldFilterPacket()."""
    iface = iface_with_nodes
    iface.noProto = True
    # faked UDP
    packet = b"\x00\x00\x00\x00\x00\x00\x00\x00\x00\x11\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x07\x6c\x07\x6c\x00\x00\x00"
    # Note: custom logging level
    LOG_TRACE = 5
    with caplog.at_level(LOG_TRACE):
        tun = Tunnel(iface)
        ignore = tun._shouldFilterPacket(packet)
        assert re.search(r"ignoring blacklisted UDP", caplog.text, re.MULTILINE)
        assert ignore


@pytest.mark.unit
def test_shouldFilterPacket_tcp(caplog, iface_with_nodes):
    """Test _shouldFilterPacket()."""
    iface = iface_with_nodes
    iface.noProto = True
    # faked TCP
    packet = b"\x00\x00\x00\x00\x00\x00\x00\x00\x00\x06\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00"
    with caplog.at_level(logging.DEBUG):
        tun = Tunnel(iface)
        ignore = tun._shouldFilterPacket(packet)
        assert re.search(r"forwarding tcp", caplog.text, re.MULTILINE)
        assert not ignore


@pytest.mark.unitslow
def test_shouldFilterPacket_tcp_blacklisted(caplog, iface_with_nodes):
    """Test _shouldFilterPacket()."""
    iface = iface_with_nodes
    iface.noProto = True
    # faked TCP
    packet = b"\x00\x00\x00\x00\x00\x00\x00\x00\x00\x06\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x17\x0c\x17\x0c\x00\x00\x00"
    # Note: custom logging level
    LOG_TRACE = 5
    with caplog.at_level(LOG_TRACE):
        tun = Tunnel(iface)
        ignore = tun._shouldFilterPacket(packet)
        assert re.search(r"ignoring blacklisted TCP", caplog.text, re.MULTILINE)
        assert ignore


@pytest.mark.unitslow
def test_ipToNodeId_none(caplog, iface_with_nodes):
    """Test _ipToNodeId()."""
    iface = iface_with_nodes
    iface.noProto = True
    with caplog.at_level(logging.DEBUG):
        tun = Tunnel(iface)
        nodeid = tun._ipToNodeId("something not useful")
        assert nodeid is None


@pytest.mark.unitslow
def test_ipToNodeId_all(caplog, iface_with_nodes):
    """Test _ipToNodeId()."""
    iface = iface_with_nodes
    iface.noProto = True
    with caplog.at_level(logging.DEBUG):
        tun = Tunnel(iface)
        nodeid = tun._ipToNodeId(b"\x00\x00\xff\xff")
        assert nodeid == "^all"
