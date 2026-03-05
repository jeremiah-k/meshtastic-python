"""TCPInterface integration checks against a live meshtasticd daemon."""

import os
import time
from typing import cast

import pytest

from meshtastic.tcp_interface import DEFAULT_TCP_PORT, TCPInterface

MESHTASTICD_HOST_ENV_VAR = "MESHTASTICD_HOST"
HOST = os.environ.get(MESHTASTICD_HOST_ENV_VAR, "localhost:4401")
CONNECT_TIMEOUT_SECONDS = 5.0
WAIT_CONNECTED_TIMEOUT_SECONDS = 10.0
RECONNECT_RECOVERY_TIMEOUT_SECONDS = 25.0
RECONNECT_RETRY_INTERVAL_SECONDS = 0.5


def _parse_host_and_port(host: str) -> tuple[str, int]:
    """Parse ``HOST[:PORT]`` into a hostname and TCP port."""
    if ":" not in host:
        return host, DEFAULT_TCP_PORT

    host_name, raw_port = host.rsplit(":", 1)
    if not host_name:
        raise ValueError(
            f"Invalid {MESHTASTICD_HOST_ENV_VAR}={host!r}: host component is empty."
        )
    try:
        port = int(raw_port)
    except ValueError as exc:
        raise ValueError(
            f"Invalid {MESHTASTICD_HOST_ENV_VAR}={host!r}: invalid TCP port "
            f"{raw_port!r}. Expected HOST[:PORT] with numeric PORT."
        ) from exc
    if not 1 <= port <= 65535:
        raise ValueError(
            f"Invalid {MESHTASTICD_HOST_ENV_VAR}={host!r}: port must be in range "
            "1..65535."
        )
    return host_name, port


@pytest.mark.unit
def test_parse_host_and_port_rejects_non_numeric_port() -> None:
    """_parse_host_and_port should reject non-numeric port values."""
    with pytest.raises(
        ValueError,
        match=rf"Invalid {MESHTASTICD_HOST_ENV_VAR}=.*numeric PORT",
    ):
        _parse_host_and_port("localhost:not-a-port")


@pytest.mark.unit
def test_parse_host_and_port_rejects_out_of_range_port() -> None:
    """_parse_host_and_port should reject out-of-range port values."""
    with pytest.raises(
        ValueError,
        match=rf"Invalid {MESHTASTICD_HOST_ENV_VAR}=.*1\.\.65535",
    ):
        _parse_host_and_port("localhost:70000")


@pytest.mark.int
@pytest.mark.smokevirt
def test_tcp_interface_meshtasticd_connect_and_sendtext() -> None:
    """TCPInterface should connect to meshtasticd and send a text packet."""
    host_name, port = _parse_host_and_port(HOST)

    with TCPInterface(
        hostname=host_name,
        portNumber=port,
        connectTimeout=CONNECT_TIMEOUT_SECONDS,
    ) as raw_iface:
        iface = cast(TCPInterface, raw_iface)
        assert iface.isConnected.wait(timeout=WAIT_CONNECTED_TIMEOUT_SECONDS)
        assert iface.myInfo is not None
        assert iface.myInfo.my_node_num > 0
        packet = iface.sendText("meshtasticd tcp integration hello")
        assert packet is not None


@pytest.mark.int
@pytest.mark.smokevirt
def test_tcp_interface_meshtasticd_recovers_after_socket_drop() -> None:
    """TCPInterface should recover after a forced local socket close."""
    host_name, port = _parse_host_and_port(HOST)

    with TCPInterface(
        hostname=host_name,
        portNumber=port,
        connectTimeout=CONNECT_TIMEOUT_SECONDS,
    ) as raw_iface:
        iface = cast(TCPInterface, raw_iface)
        assert iface.isConnected.wait(timeout=WAIT_CONNECTED_TIMEOUT_SECONDS)
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
