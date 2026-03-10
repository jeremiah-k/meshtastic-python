"""TCPInterface integration checks against a live meshtasticd daemon."""

import os
import socket
import time
from typing import cast

import pytest

MESHTASTICD_HOST_ENV_VAR: str = "MESHTASTICD_HOST"
CONNECT_TIMEOUT_SECONDS: float = 5.0
WAIT_CONNECTED_TIMEOUT_SECONDS: float = 10.0
RECONNECT_RECOVERY_TIMEOUT_SECONDS: float = 25.0
RECONNECT_RETRY_INTERVAL_SECONDS: float = 0.5


def _require_meshtasticd_host() -> str:
    """Return the configured meshtasticd host or skip when unset."""
    host = os.environ.get(MESHTASTICD_HOST_ENV_VAR)
    if not host:
        pytest.skip(
            f"Set {MESHTASTICD_HOST_ENV_VAR} to run meshtasticd integration tests."
        )
    return host


def _parse_host_and_port(host: str) -> tuple[str, int]:
    """Parse ``HOST[:PORT]`` into a hostname and TCP port via shared runtime helper."""
    from meshtastic.host_port import (  # pylint: disable=import-outside-toplevel
        parseHostAndPort,
    )
    from meshtastic.tcp_interface import (  # pylint: disable=import-outside-toplevel
        DEFAULT_TCP_PORT,
    )

    return parseHostAndPort(
        host,
        default_port=DEFAULT_TCP_PORT,
        env_var=MESHTASTICD_HOST_ENV_VAR,
    )


def _default_tcp_port() -> int:
    """Return the runtime default TCP port used by TCPInterface."""
    from meshtastic.tcp_interface import (  # pylint: disable=import-outside-toplevel
        DEFAULT_TCP_PORT,
    )

    return DEFAULT_TCP_PORT


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


@pytest.mark.unit
def test_parse_host_and_port_rejects_empty_port() -> None:
    """_parse_host_and_port should reject explicitly empty port values."""
    with pytest.raises(
        ValueError,
        match=rf"Invalid {MESHTASTICD_HOST_ENV_VAR}=.*numeric PORT",
    ):
        _parse_host_and_port("localhost:")


@pytest.mark.unit
def test_parse_host_and_port_rejects_zero_port() -> None:
    """_parse_host_and_port should reject port zero."""
    with pytest.raises(
        ValueError,
        match=rf"Invalid {MESHTASTICD_HOST_ENV_VAR}=.*1\.\.65535",
    ):
        _parse_host_and_port("localhost:0")


@pytest.mark.unit
def test_parse_host_and_port_accepts_bracketed_ipv6_with_port() -> None:
    """_parse_host_and_port should accept bracketed IPv6 addresses with ports."""
    assert _parse_host_and_port("[::1]:4401") == ("::1", 4401)


@pytest.mark.unit
def test_parse_host_and_port_snake_case_alias_matches_canonical_name() -> None:
    """The snake_case host parser alias should delegate to the canonical camelCase name."""
    from meshtastic.host_port import (  # pylint: disable=import-outside-toplevel
        parseHostAndPort,
        parse_host_and_port,
    )

    assert parse_host_and_port(
        "localhost:4401",
        default_port=_default_tcp_port(),
        env_var=MESHTASTICD_HOST_ENV_VAR,
    ) == parseHostAndPort(
        "localhost:4401",
        default_port=_default_tcp_port(),
        env_var=MESHTASTICD_HOST_ENV_VAR,
    )


@pytest.mark.unit
def test_parse_host_and_port_rejects_malformed_bracketed_ipv6() -> None:
    """Malformed bracketed IPv6 literals should raise a clear parse error."""
    with pytest.raises(ValueError, match=r"raw IPv6 literals"):
        _parse_host_and_port("[]")


@pytest.mark.unit
def test_parse_host_and_port_accepts_raw_ipv6_without_port() -> None:
    """_parse_host_and_port should treat raw IPv6 literals as host-only values."""
    assert _parse_host_and_port("::1") == ("::1", _default_tcp_port())


@pytest.mark.unit
def test_parse_host_and_port_accepts_compressed_ipv6_with_numeric_tail() -> None:
    """_parse_host_and_port should accept compressed IPv6 literals whose tail is numeric."""
    assert _parse_host_and_port("2001:db8::1:2:3") == (
        "2001:db8::1:2:3",
        _default_tcp_port(),
    )


@pytest.mark.unit
@pytest.mark.parametrize("host", ["::1:4401", "::2:4401", "::a:1234"])
def test_parse_host_and_port_accepts_unbracketed_ipv6_numeric_tail(host: str) -> None:
    """Valid raw IPv6 literals with numeric tails should parse as host-only values."""
    assert _parse_host_and_port(host) == (host, _default_tcp_port())


@pytest.mark.unit
def test_parse_host_and_port_rejects_invalid_unbracketed_ipv6_port_shape() -> None:
    """Invalid unbracketed IPv6:PORT-like strings should require bracket form."""
    with pytest.raises(
        ValueError,
        match=r"raw IPv6 literals with explicit ports must use bracket form",
    ):
        _parse_host_and_port("2001:db8:0:1:2:3:4:5:4401")


@pytest.mark.unit
@pytest.mark.parametrize("host", ["localhost", "::1"])
@pytest.mark.parametrize("default_port", [0, 70000])
def test_parse_host_and_port_rejects_invalid_default_port(
    host: str,
    default_port: int,
) -> None:
    """Host-only parse paths should reject invalid default ports before returning."""
    from meshtastic.host_port import (  # pylint: disable=import-outside-toplevel
        parseHostAndPort,
    )

    with pytest.raises(
        ValueError,
        match=rf"Invalid {MESHTASTICD_HOST_ENV_VAR}=.*1\.\.65535",
    ):
        parseHostAndPort(
            host,
            default_port=default_port,
            env_var=MESHTASTICD_HOST_ENV_VAR,
        )


@pytest.mark.unit
@pytest.mark.parametrize("default_port", [0, 70000])
def test_parse_host_and_port_skips_default_port_validation_with_explicit_port(
    default_port: int,
) -> None:
    """Explicit host:port inputs should not validate the unused default port."""
    from meshtastic.host_port import (  # pylint: disable=import-outside-toplevel
        parseHostAndPort,
    )

    assert parseHostAndPort(
        "localhost:4401",
        default_port=default_port,
        env_var=MESHTASTICD_HOST_ENV_VAR,
    ) == ("localhost", 4401)


@pytest.mark.unit
@pytest.mark.parametrize(
    "host",
    [
        "user@localhost:4401",
        "user:pass@localhost:4401",
        "localhost/path",
        "localhost?x=1",
        "localhost#fragment",
        "localhost;params",
    ],
)
def test_parse_host_and_port_rejects_extra_url_components(host: str) -> None:
    """_parse_host_and_port should reject usernames, paths, and query strings."""
    with pytest.raises(
        ValueError,
        match=r"expected HOST\[:PORT\] only",
    ):
        _parse_host_and_port(host)


@pytest.mark.int
def test_tcp_interface_meshtasticd_connect_and_sendtext() -> None:
    """TCPInterface should connect to meshtasticd and send a text packet."""
    from meshtastic.tcp_interface import (  # pylint: disable=import-outside-toplevel
        TCPInterface,
    )

    host = _require_meshtasticd_host()
    host_name, port = _parse_host_and_port(host)

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
def test_tcp_interface_meshtasticd_recovers_after_socket_drop() -> None:
    """TCPInterface should recover after a forced local socket close."""
    from meshtastic.tcp_interface import (  # pylint: disable=import-outside-toplevel
        TCPInterface,
    )

    host = _require_meshtasticd_host()
    host_name, port = _parse_host_and_port(host)

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
        try:
            original_socket.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        original_socket.close()
        time.sleep(0.1)

        recovered = False
        last_error: Exception | None = None
        deadline = time.monotonic() + RECONNECT_RECOVERY_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            try:
                if not iface.isConnected.wait(timeout=RECONNECT_RETRY_INTERVAL_SECONDS):
                    continue
                packet = iface.sendText("meshtasticd tcp reconnect probe")
                if packet is not None and iface.socket is not None:
                    if iface.socket is not original_socket:
                        recovered = True
                        break
            except (
                ConnectionError,
                OSError,
                TimeoutError,
                TCPInterface.MeshInterfaceError,
            ) as ex:
                last_error = ex
            time.sleep(RECONNECT_RETRY_INTERVAL_SECONDS)

        assert recovered, (
            "TCPInterface did not recover after forced socket close.\n"
            f"host={host}\n"
            f"last_error={last_error!r}\n"
            f"socket_present={iface.socket is not None}\n"
            f"fatal_disconnect={getattr(iface, '_fatal_disconnect', None)}"
        )
