"""Meshtastic unit tests for tunnel.py."""

import argparse
import logging
import re
import sys
from contextlib import contextmanager
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


@contextmanager
def _managed_tunnel(iface: MeshInterface) -> Generator[Tunnel, None, None]:
    """Yield a Tunnel and ensure close() is always called."""
    tun = Tunnel(iface)
    try:
        yield tun
    finally:
        tun.close()


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
    assert re.search(r"Not creating a TapDevice\(\)", caplog.text, re.MULTILINE)
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
    monkeypatch.setattr(mt_config, "args", argparse.Namespace())
    packet = {"decoded": {"payload": "foo"}, "from": 2475227164}
    with caplog.at_level(logging.DEBUG):
        tun = Tunnel(iface)
        try:
            onTunnelReceive(packet, iface)
        finally:
            tun.close()
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
    monkeypatch.setattr(mt_config, "args", argparse.Namespace())
    packet = {"decoded": {"payload": "foo"}, "from": 123}
    with caplog.at_level(logging.DEBUG):
        tun = Tunnel(iface)
        try:
            onTunnelReceive(packet, iface)
        finally:
            tun.close()
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
        try:
            ignore = tun._should_filter_packet(packet)
            assert not ignore
        finally:
            tun.close()


@pytest.mark.unit
def test_should_filter_packet_short_header(
    caplog: pytest.LogCaptureFixture,
    iface_with_nodes: MeshInterface,
) -> None:
    """Packets shorter than an IPv4 header should be dropped safely."""
    iface = iface_with_nodes
    iface.noProto = True
    packet = b"\x00" * 10
    with caplog.at_level(logging.DEBUG):
        tun = Tunnel(iface)
        try:
            ignore = tun._should_filter_packet(packet)
            assert ignore
        finally:
            tun.close()
    assert re.search(r"Ignoring short IP packet", caplog.text, re.MULTILINE)


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
        try:
            ignore = tun._should_filter_packet(packet)
            assert ignore
        finally:
            tun.close()


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
        try:
            ignore = tun._should_filter_packet(packet)
            assert re.search(r"forwarding ICMP message", caplog.text, re.MULTILINE)
            assert not ignore
        finally:
            tun.close()


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
        try:
            ignore = tun._should_filter_packet(packet)
            assert re.search(r"forwarding udp", caplog.text, re.MULTILINE)
            assert not ignore
        finally:
            tun.close()


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
        try:
            ignore = tun._should_filter_packet(packet)
            assert re.search(r"ignoring blacklisted UDP", caplog.text, re.MULTILINE)
            assert ignore
        finally:
            tun.close()


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
        try:
            ignore = tun._should_filter_packet(packet)
            assert re.search(r"forwarding tcp", caplog.text, re.MULTILINE)
            assert not ignore
        finally:
            tun.close()


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
        try:
            ignore = tun._should_filter_packet(packet)
            assert re.search(r"ignoring blacklisted TCP", caplog.text, re.MULTILINE)
            assert ignore
        finally:
            tun.close()


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
        try:
            nodeid = tun._ip_to_node_id(b"\x00\x00\x00\x00")
            assert nodeid is None
        finally:
            tun.close()


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
        try:
            nodeid = tun._ip_to_node_id(b"\x00\x00\xff\xff")
            assert nodeid == "^all"
        finally:
            tun.close()


@pytest.mark.unit
def test_tun_reader_forwards_unfiltered_packets(
    iface_with_nodes: MeshInterface,
) -> None:
    """_tun_reader should forward packets when filtering says they are allowed."""
    iface = iface_with_nodes
    iface.noProto = True
    tun = Tunnel(iface)
    try:
        packet = b"\x45\x00\x00\x28\x00\x00\x00\x00\x40\x11\x00\x00\x0a\x73\x01\x01\x0a\x73\x02\x02"
        tap = MagicMock()
        tap.read.return_value = packet
        tun.tun = tap
        tun._should_filter_packet = MagicMock(return_value=False)  # type: ignore[method-assign]

        def _capture_and_stop(dest_addr: bytes, payload: bytes) -> None:
            assert payload == packet
            assert dest_addr == packet[16:20]
            tun._stop_event.set()

        tun._send_packet = MagicMock(side_effect=_capture_and_stop)  # type: ignore[method-assign]
        tun._tun_reader()
        tun._send_packet.assert_called_once()
    finally:
        tun.close()


@pytest.mark.unit
def test_tun_reader_stops_on_read_failure(
    caplog: pytest.LogCaptureFixture,
    iface_with_nodes: MeshInterface,
) -> None:
    """_tun_reader should log and stop when tap.read raises unexpectedly."""
    iface = iface_with_nodes
    iface.noProto = True
    with _managed_tunnel(iface) as tun:
        tap = MagicMock()
        tap.read.side_effect = OSError("read failed")
        tun.tun = tap
        with caplog.at_level(logging.ERROR):
            tun._tun_reader()
        assert "TUN reader terminating due to read failure" in caplog.text


@pytest.mark.unit
def test_ip_to_node_id_returns_none_when_nodes_unavailable(
    iface_with_nodes: MeshInterface,
) -> None:
    """_ip_to_node_id should return None when iface.nodes is missing/empty."""
    iface = iface_with_nodes
    iface.noProto = True
    iface.nodes = None
    with _managed_tunnel(iface) as tun:
        assert tun._ip_to_node_id(b"\x00\x00\x12\x34") is None


@pytest.mark.unit
def test_ip_to_node_id_short_destination_is_ignored(
    caplog: pytest.LogCaptureFixture,
    iface_with_nodes: MeshInterface,
) -> None:
    """_ip_to_node_id should ignore short destination addresses."""
    iface = iface_with_nodes
    iface.noProto = True
    with _managed_tunnel(iface) as tun:
        with caplog.at_level(logging.DEBUG):
            assert tun._ip_to_node_id(b"\x00\x01\x02") is None
        assert "Ignoring short destination address" in caplog.text


@pytest.mark.unit
def test_ip_to_node_id_returns_matching_user_id(
    iface_with_nodes: MeshInterface,
) -> None:
    """_ip_to_node_id should return user.id for matching node-number low bits."""
    iface = iface_with_nodes
    iface.noProto = True
    iface.nodes = {
        "!candidate": {"num": 0xABCD1234, "user": {"id": "!candidate"}},
        "!ignored": {"num": 0xABCD1111, "user": {"id": "!ignored"}},
    }
    with _managed_tunnel(iface) as tun:
        assert tun._ip_to_node_id(b"\x00\x00\x12\x34") == "!candidate"


@pytest.mark.unit
def test_ip_to_node_id_skips_nodes_without_integer_num(
    iface_with_nodes: MeshInterface,
) -> None:
    """_ip_to_node_id should ignore node entries that do not expose integer num values."""
    iface = iface_with_nodes
    iface.noProto = True
    with _managed_tunnel(iface) as tun:
        iface.nodes = {"!bad": {"num": "not-an-int", "user": {"id": "!bad"}}}
        assert tun._ip_to_node_id(b"\x00\x00\x12\x34") is None


@pytest.mark.unit
def test_should_filter_packet_alias_delegates(
    iface_with_nodes: MeshInterface,
) -> None:
    """_shouldFilterPacket should delegate to _should_filter_packet."""
    iface = iface_with_nodes
    iface.noProto = True
    with _managed_tunnel(iface) as tun:
        tun._should_filter_packet = MagicMock(return_value=True)  # type: ignore[method-assign]
        assert tun._shouldFilterPacket(b"\x00" * 20) is True
        tun._should_filter_packet.assert_called_once()
