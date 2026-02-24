"""Meshtastic unit tests for tunnel.py."""

import argparse
import logging
import re
import sys
from collections.abc import Generator
from unittest.mock import MagicMock

import pytest

from meshtastic import mt_config

from ..mesh_interface import MeshInterface
from ..tcp_interface import TCPInterface

try:
    # Depends upon pytap2, not installed by default
    from ..tunnel import Tunnel, onTunnelReceive
except ImportError:
    pytest.skip("Can't import Tunnel or onTunnelReceive", allow_module_level=True)

pytestmark = pytest.mark.usefixtures("platform_socket_mocks")

# Custom logging level for trace-level logs
LOG_TRACE = 5


@pytest.fixture(autouse=True)
def reset_tunnel_mt_config_state() -> Generator[None, None, None]:
    """Reset mt_config module state before and after each tunnel test."""

    def _close_active_tunnel() -> None:
        tunnel = mt_config.tunnel_instance
        if tunnel is not None:
            tunnel.close()

    _close_active_tunnel()
    mt_config.reset()
    yield
    _close_active_tunnel()
    mt_config.reset()


@pytest.mark.unit
def test_Tunnel_on_non_linux_system(
    platform_socket_mocks: tuple[MagicMock, MagicMock],
) -> None:
    """Test that we cannot instantiate a Tunnel on a non Linux system."""
    mock_platform_system, _ = platform_socket_mocks
    mock_platform_system.return_value = "notLinux"
    iface = TCPInterface(hostname="localhost", noProto=True)
    try:
        with pytest.raises(Tunnel.TunnelError) as pytest_wrapped_e:
            Tunnel(iface)
    finally:
        iface.close()
    assert issubclass(pytest_wrapped_e.type, Tunnel.TunnelError)


@pytest.mark.unit
def test_Tunnel_without_interface() -> None:
    """Test that we can not instantiate a Tunnel without a valid interface."""
    with pytest.raises(Tunnel.TunnelError) as pytest_wrapped_e:
        Tunnel(None)
    assert pytest_wrapped_e.type == Tunnel.TunnelError


@pytest.mark.unitslow
def test_Tunnel_with_interface(
    caplog: pytest.LogCaptureFixture,
    iface_with_nodes: MeshInterface,
) -> None:
    """Test that Tunnel initializes with a valid interface and registers itself."""
    iface = iface_with_nodes
    assert iface.myInfo is not None
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
def test_onTunnelReceive_from_ourselves(
    caplog: pytest.LogCaptureFixture,
    iface_with_nodes: MeshInterface,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Test onTunnelReceive."""
    iface = iface_with_nodes
    assert iface.myInfo is not None
    iface.myInfo.my_node_num = 2475227164
    monkeypatch.setattr(sys, "argv", [""])
    mt_config.args = argparse.Namespace()
    packet = {"decoded": {"payload": "foo"}, "from": 2475227164}
    with caplog.at_level(logging.DEBUG):
        Tunnel(iface)
        onTunnelReceive(packet, iface)
    assert re.search(r"in onTunnelReceive", caplog.text, re.MULTILINE)
    assert re.search(r"Ignoring message we sent", caplog.text, re.MULTILINE)


@pytest.mark.unit
def test_onTunnelReceive_from_someone_else(
    caplog: pytest.LogCaptureFixture,
    iface_with_nodes: MeshInterface,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Test onTunnelReceive."""
    iface = iface_with_nodes
    assert iface.myInfo is not None
    iface.myInfo.my_node_num = 2475227164
    monkeypatch.setattr(sys, "argv", [""])
    mt_config.args = argparse.Namespace()
    packet = {"decoded": {"payload": "foo"}, "from": 123}
    with caplog.at_level(logging.DEBUG):
        Tunnel(iface)
        onTunnelReceive(packet, iface)
    assert re.search(r"in onTunnelReceive", caplog.text, re.MULTILINE)


@pytest.mark.unitslow
def test_should_filter_packet_random(
    caplog: pytest.LogCaptureFixture,
    iface_with_nodes: MeshInterface,
) -> None:
    """Test _should_filter_packet()."""
    iface = iface_with_nodes
    iface.noProto = True
    # random packet
    packet = b"1234567890123456789012345678901234567890"
    with caplog.at_level(logging.DEBUG):
        tun = Tunnel(iface)
        ignore = tun._should_filter_packet(packet)
        assert not ignore


@pytest.mark.unitslow
def test_should_filter_packet_in_blacklist(
    caplog: pytest.LogCaptureFixture,
    iface_with_nodes: MeshInterface,
) -> None:
    """Test _should_filter_packet()."""
    iface = iface_with_nodes
    iface.noProto = True
    # faked IGMP
    packet = b"\x00\x00\x00\x00\x00\x00\x00\x00\x00\x02\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00"
    with caplog.at_level(logging.DEBUG):
        tun = Tunnel(iface)
        ignore = tun._should_filter_packet(packet)
        assert ignore


@pytest.mark.unitslow
def test_should_filter_packet_icmp(
    caplog: pytest.LogCaptureFixture,
    iface_with_nodes: MeshInterface,
) -> None:
    """Test _should_filter_packet()."""
    iface = iface_with_nodes
    iface.noProto = True
    # faked ICMP
    packet = b"\x00\x00\x00\x00\x00\x00\x00\x00\x00\x01\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00"
    with caplog.at_level(logging.DEBUG):
        tun = Tunnel(iface)
        ignore = tun._should_filter_packet(packet)
        assert re.search(r"forwarding ICMP message", caplog.text, re.MULTILINE)
        assert not ignore


@pytest.mark.unit
def test_should_filter_packet_udp(
    caplog: pytest.LogCaptureFixture,
    iface_with_nodes: MeshInterface,
) -> None:
    """Test _should_filter_packet()."""
    iface = iface_with_nodes
    iface.noProto = True
    # faked UDP
    packet = b"\x00\x00\x00\x00\x00\x00\x00\x00\x00\x11\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00"
    with caplog.at_level(logging.DEBUG):
        tun = Tunnel(iface)
        ignore = tun._should_filter_packet(packet)
        assert re.search(r"forwarding udp", caplog.text, re.MULTILINE)
        assert not ignore


@pytest.mark.unitslow
def test_should_filter_packet_udp_blacklisted(
    caplog: pytest.LogCaptureFixture,
    iface_with_nodes: MeshInterface,
) -> None:
    """Test _should_filter_packet()."""
    iface = iface_with_nodes
    iface.noProto = True
    # faked UDP
    packet = b"\x00\x00\x00\x00\x00\x00\x00\x00\x00\x11\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x07\x6c\x07\x6c\x00\x00\x00"
    with caplog.at_level(LOG_TRACE):
        tun = Tunnel(iface)
        ignore = tun._should_filter_packet(packet)
        assert re.search(r"ignoring blacklisted UDP", caplog.text, re.MULTILINE)
        assert ignore


@pytest.mark.unit
def test_should_filter_packet_tcp(
    caplog: pytest.LogCaptureFixture,
    iface_with_nodes: MeshInterface,
) -> None:
    """Test _should_filter_packet()."""
    iface = iface_with_nodes
    iface.noProto = True
    # faked TCP
    packet = b"\x00\x00\x00\x00\x00\x00\x00\x00\x00\x06\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00"
    with caplog.at_level(logging.DEBUG):
        tun = Tunnel(iface)
        ignore = tun._should_filter_packet(packet)
        assert re.search(r"forwarding tcp", caplog.text, re.MULTILINE)
        assert not ignore


@pytest.mark.unitslow
def test_should_filter_packet_tcp_blacklisted(
    caplog: pytest.LogCaptureFixture,
    iface_with_nodes: MeshInterface,
) -> None:
    """Test _should_filter_packet()."""
    iface = iface_with_nodes
    iface.noProto = True
    # faked TCP
    packet = b"\x00\x00\x00\x00\x00\x00\x00\x00\x00\x06\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x17\x0c\x17\x0c\x00\x00\x00"
    with caplog.at_level(LOG_TRACE):
        tun = Tunnel(iface)
        ignore = tun._should_filter_packet(packet)
        assert re.search(r"ignoring blacklisted TCP", caplog.text, re.MULTILINE)
        assert ignore


@pytest.mark.unitslow
def test_ip_to_node_id_none(
    caplog: pytest.LogCaptureFixture,
    iface_with_nodes: MeshInterface,
) -> None:
    """Test _ip_to_node_id()."""
    iface = iface_with_nodes
    iface.noProto = True
    with caplog.at_level(logging.DEBUG):
        tun = Tunnel(iface)
        nodeid = tun._ip_to_node_id(b"\x00\x00\x00\x00")
        assert nodeid is None


@pytest.mark.unitslow
def test_ip_to_node_id_all(
    caplog: pytest.LogCaptureFixture,
    iface_with_nodes: MeshInterface,
) -> None:
    """Test _ip_to_node_id()."""
    iface = iface_with_nodes
    iface.noProto = True
    with caplog.at_level(logging.DEBUG):
        tun = Tunnel(iface)
        nodeid = tun._ip_to_node_id(b"\x00\x00\xff\xff")
        assert nodeid == "^all"
