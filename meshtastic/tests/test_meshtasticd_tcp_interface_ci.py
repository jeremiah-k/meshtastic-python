"""TCPInterface integration checks against a live meshtasticd daemon."""

import os
import time
from typing import cast

import pytest

from meshtastic.tcp_interface import DEFAULT_TCP_PORT, TCPInterface

pytestmark = [pytest.mark.int, pytest.mark.smokevirt]

HOST = os.environ.get("MESHTASTICD_HOST", "localhost:4401")
CONNECT_TIMEOUT_SECONDS = 5.0
RECONNECT_RECOVERY_TIMEOUT_SECONDS = 25.0
RECONNECT_RETRY_INTERVAL_SECONDS = 0.5


def _parse_host_and_port(host: str) -> tuple[str, int]:
    """Parse ``HOST[:PORT]`` into a hostname and TCP port."""
    if ":" not in host:
        return host, DEFAULT_TCP_PORT
    host_name, raw_port = host.rsplit(":", 1)
    return host_name, int(raw_port)


def test_tcp_interface_meshtasticd_connect_and_sendtext() -> None:
    """TCPInterface should connect to meshtasticd and send a text packet."""
    host_name, port = _parse_host_and_port(HOST)

    with TCPInterface(
        hostname=host_name,
        portNumber=port,
        connectTimeout=CONNECT_TIMEOUT_SECONDS,
    ) as raw_iface:
        iface = cast(TCPInterface, raw_iface)
        assert iface.isConnected.wait(timeout=10.0)
        assert iface.myInfo is not None
        assert iface.myInfo.my_node_num > 0
        packet = iface.sendText("meshtasticd tcp integration hello")
        assert packet is not None


def test_tcp_interface_meshtasticd_recovers_after_socket_drop() -> None:
    """TCPInterface should recover after a forced local socket close."""
    host_name, port = _parse_host_and_port(HOST)

    with TCPInterface(
        hostname=host_name,
        portNumber=port,
        connectTimeout=CONNECT_TIMEOUT_SECONDS,
    ) as raw_iface:
        iface = cast(TCPInterface, raw_iface)
        assert iface.isConnected.wait(timeout=10.0)
        original_socket = iface.socket
        assert original_socket is not None

        # Force a local transport break to exercise the reader-thread reconnect path.
        original_socket.close()

        recovered = False
        last_error: Exception | None = None
        deadline = time.monotonic() + RECONNECT_RECOVERY_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            try:
                packet = iface.sendText("meshtasticd tcp reconnect probe")
                if packet is not None and iface.socket is not None:
                    if iface.socket is not original_socket:
                        recovered = True
                        break
            except (ConnectionError, OSError, TimeoutError) as ex:
                last_error = ex
            time.sleep(RECONNECT_RETRY_INTERVAL_SECONDS)

        assert recovered, (
            "TCPInterface did not recover after forced socket close.\n"
            f"host={HOST}\n"
            f"last_error={last_error!r}\n"
            f"socket_present={iface.socket is not None}\n"
            f"fatal_disconnect={getattr(iface, '_fatal_disconnect', None)}"
        )
