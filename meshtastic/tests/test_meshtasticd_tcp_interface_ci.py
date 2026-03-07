"""TCPInterface integration checks against a live meshtasticd daemon."""

import ipaddress
import os
import socket
import time
from typing import cast
from urllib.parse import ParseResult, urlparse

import pytest

from meshtastic.tcp_interface import DEFAULT_TCP_PORT, TCPInterface

MESHTASTICD_HOST_ENV_VAR = "MESHTASTICD_HOST"
CONNECT_TIMEOUT_SECONDS = 5.0
WAIT_CONNECTED_TIMEOUT_SECONDS = 10.0
RECONNECT_RECOVERY_TIMEOUT_SECONDS = 25.0
RECONNECT_RETRY_INTERVAL_SECONDS = 0.5

INVALID_HOST_EMPTY = "Invalid {env_var}={host!r}: host component is empty."
EXPECTED_HOST_PORT_ONLY = "Invalid {env_var}={host!r}: expected HOST[:PORT] only."
INVALID_IPV6_BRACKETED_PORT = (
    "Invalid {env_var}={host!r}: raw IPv6 literals "
    "with explicit ports must use bracket form like [::1]:4401."
)
INVALID_PORT_EMPTY = (
    "Invalid {env_var}={host!r}: invalid TCP port ''. Expected HOST[:PORT] "
    "with numeric PORT."
)
INVALID_PORT_NONNUMERIC = (
    "Invalid {env_var}={host!r}: invalid TCP port {raw_port!r}. Expected "
    "HOST[:PORT] with numeric PORT."
)
INVALID_PORT_RANGE = "Invalid {env_var}={host!r}: port must be in range 1..65535."


def _require_meshtasticd_host() -> str:
    """Return the configured meshtasticd host or skip when unset."""
    host = os.environ.get(MESHTASTICD_HOST_ENV_VAR)
    if not host:
        pytest.skip(
            f"Set {MESHTASTICD_HOST_ENV_VAR} to run meshtasticd integration tests."
        )
    return host


def _extract_port_component(host: str) -> str | None:
    """Extract the textual port component from a HOST[:PORT] string when present."""
    if host.startswith("[") and "]:" in host:
        return host.rsplit("]:", 1)[1]
    if host.count(":") == 1:
        return host.rsplit(":", 1)[1]
    return None


def _has_extra_url_components(parsed: ParseResult) -> bool:
    """Return True when a parsed HOST value includes unsupported URL components."""
    return any(
        (
            parsed.path,
            parsed.params,
            parsed.query,
            parsed.fragment,
            parsed.username is not None,
            parsed.password is not None,
        )
    )


def _parse_host_and_port(host: str) -> tuple[str, int]:
    """Parse ``HOST[:PORT]`` into a hostname and TCP port."""
    if not host:
        raise ValueError(
            INVALID_HOST_EMPTY.format(env_var=MESHTASTICD_HOST_ENV_VAR, host=host)
        )
    if any(separator in host for separator in ("@", "/", "?", "#", ";")):
        raise ValueError(
            EXPECTED_HOST_PORT_ONLY.format(
                env_var=MESHTASTICD_HOST_ENV_VAR,
                host=host,
            )
        )
    if host.count(":") >= 2 and not host.startswith("["):
        host_part, separator, possible_port = host.rpartition(":")
        if separator and possible_port.isdigit():
            try:
                ipaddress.IPv6Address(host_part)
            except ipaddress.AddressValueError:
                pass
            else:
                raise ValueError(
                    INVALID_IPV6_BRACKETED_PORT.format(
                        env_var=MESHTASTICD_HOST_ENV_VAR,
                        host=host,
                    )
                )
        try:
            ipaddress.IPv6Address(host)
        except ipaddress.AddressValueError as exc:
            raise ValueError(
                INVALID_IPV6_BRACKETED_PORT.format(
                    env_var=MESHTASTICD_HOST_ENV_VAR,
                    host=host,
                )
            ) from exc
        return host, DEFAULT_TCP_PORT

    raw_port = _extract_port_component(host)
    if raw_port == "":
        raise ValueError(
            INVALID_PORT_EMPTY.format(env_var=MESHTASTICD_HOST_ENV_VAR, host=host)
        )

    parsed = urlparse(f"//{host}")
    if _has_extra_url_components(parsed):
        raise ValueError(
            EXPECTED_HOST_PORT_ONLY.format(
                env_var=MESHTASTICD_HOST_ENV_VAR,
                host=host,
            )
        )
    try:
        host_name = parsed.hostname
        port = parsed.port
    except ValueError as exc:
        if raw_port is not None and not raw_port.isdigit():
            raise ValueError(
                INVALID_PORT_NONNUMERIC.format(
                    env_var=MESHTASTICD_HOST_ENV_VAR,
                    host=host,
                    raw_port=raw_port,
                )
            ) from exc
        raise ValueError(
            INVALID_PORT_RANGE.format(env_var=MESHTASTICD_HOST_ENV_VAR, host=host)
        ) from exc
    if not host_name:
        raise ValueError(
            INVALID_HOST_EMPTY.format(env_var=MESHTASTICD_HOST_ENV_VAR, host=host)
        )
    if raw_port == "0" or port == 0:
        raise ValueError(
            INVALID_PORT_RANGE.format(env_var=MESHTASTICD_HOST_ENV_VAR, host=host)
        )
    if raw_port is None:
        return host_name, DEFAULT_TCP_PORT
    assert port is not None
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
def test_parse_host_and_port_accepts_raw_ipv6_without_port() -> None:
    """_parse_host_and_port should treat raw IPv6 literals as host-only values."""
    assert _parse_host_and_port("::1") == ("::1", DEFAULT_TCP_PORT)


@pytest.mark.unit
def test_parse_host_and_port_rejects_ambiguous_unbracketed_ipv6_port() -> None:
    """_parse_host_and_port should reject IPv6 HOST:PORT values without brackets."""
    with pytest.raises(
        ValueError,
        match=r"raw IPv6 literals with explicit ports must use bracket form",
    ):
        _parse_host_and_port("::1:4401")


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
@pytest.mark.smokevirt
def test_tcp_interface_meshtasticd_connect_and_sendtext() -> None:
    """TCPInterface should connect to meshtasticd and send a text packet."""
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
@pytest.mark.smokevirt
def test_tcp_interface_meshtasticd_recovers_after_socket_drop() -> None:
    """TCPInterface should recover after a forced local socket close."""
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
            f"host={host}\n"
            f"last_error={last_error!r}\n"
            f"socket_present={iface.socket is not None}\n"
            f"fatal_disconnect={getattr(iface, '_fatal_disconnect', None)}"
        )
