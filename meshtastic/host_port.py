"""Shared host/port parsing helpers used by runtime and integration tooling."""

from __future__ import annotations

import ipaddress
from urllib.parse import ParseResult, urlparse

_EXPECTED_HOST_PORT_ONLY = "Invalid {env_var}={host!r}: expected HOST[:PORT] only."
_INVALID_HOST_EMPTY = "Invalid {env_var}={host!r}: host component is empty."
_INVALID_IPV6_BRACKETED_PORT = (
    "Invalid {env_var}={host!r}: raw IPv6 literals "
    "with explicit ports must use bracket form like [::1]:4401."
)
_INVALID_PORT_EMPTY = (
    "Invalid {env_var}={host!r}: invalid TCP port ''. Expected HOST[:PORT] "
    "with numeric PORT."
)
_INVALID_PORT_NONNUMERIC = (
    "Invalid {env_var}={host!r}: invalid TCP port {raw_port!r}. Expected "
    "HOST[:PORT] with numeric PORT."
)
_INVALID_PORT_RANGE = "Invalid {env_var}={host!r}: port must be in range 1..65535."
_FORBIDDEN_HOST_SEPARATORS: tuple[str, ...] = ("@", "/", "?", "#", ";")


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


def _validate_port(
    port: int,
    *,
    host: str,
    env_var: str,
) -> int:
    """Validate a TCP port value and return it unchanged when valid."""
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        raise ValueError(_INVALID_PORT_RANGE.format(env_var=env_var, host=host))
    return port


def parseHostAndPort(
    host: str,
    *,
    default_port: int,
    env_var: str,
) -> tuple[str, int]:
    """Parse ``HOST[:PORT]`` into a hostname and TCP port.

    Parameters
    ----------
    host : str
        Raw host input in HOST[:PORT] form.
    default_port : int
        Port to use when `host` omits an explicit port.
    env_var : str
        Environment-variable name used in validation error messages.

    Returns
    -------
    tuple[str, int]
        Parsed `(hostname, port)`.

    Raises
    ------
    ValueError
        If `host` is malformed or the port is invalid.
    """
    host = host.strip()
    if not host:
        raise ValueError(_INVALID_HOST_EMPTY.format(env_var=env_var, host=host))

    if any(char.isspace() for char in host):
        raise ValueError(_EXPECTED_HOST_PORT_ONLY.format(env_var=env_var, host=host))

    if any(separator in host for separator in _FORBIDDEN_HOST_SEPARATORS):
        raise ValueError(_EXPECTED_HOST_PORT_ONLY.format(env_var=env_var, host=host))

    default_port = _validate_port(default_port, host=host, env_var=env_var)

    if host.count(":") >= 2 and not host.startswith("["):
        try:
            ipaddress.IPv6Address(host)
        except ipaddress.AddressValueError as exc:
            host_part, separator, possible_port = host.rpartition(":")
            if separator and possible_port.isdigit():
                try:
                    ipaddress.IPv6Address(host_part)
                except ipaddress.AddressValueError:
                    pass
                else:
                    raise ValueError(
                        _INVALID_IPV6_BRACKETED_PORT.format(env_var=env_var, host=host)
                    ) from exc
            raise ValueError(
                _INVALID_IPV6_BRACKETED_PORT.format(env_var=env_var, host=host)
            ) from exc
        else:
            return host, default_port

    raw_port = _extract_port_component(host)
    if raw_port == "":
        raise ValueError(_INVALID_PORT_EMPTY.format(env_var=env_var, host=host))

    try:
        parsed = urlparse(f"//{host}")
    except ValueError as exc:
        if host.startswith("["):
            raise ValueError(
                _INVALID_IPV6_BRACKETED_PORT.format(env_var=env_var, host=host)
            ) from exc
        if raw_port is not None and not raw_port.isdigit():
            raise ValueError(
                _INVALID_PORT_NONNUMERIC.format(
                    env_var=env_var,
                    host=host,
                    raw_port=raw_port,
                )
            ) from exc
        raise ValueError(
            _INVALID_PORT_RANGE.format(env_var=env_var, host=host)
        ) from exc
    if _has_extra_url_components(parsed):
        raise ValueError(_EXPECTED_HOST_PORT_ONLY.format(env_var=env_var, host=host))

    try:
        host_name = parsed.hostname
        port = parsed.port
    except ValueError as exc:
        if raw_port is not None and not raw_port.isdigit():
            raise ValueError(
                _INVALID_PORT_NONNUMERIC.format(
                    env_var=env_var,
                    host=host,
                    raw_port=raw_port,
                )
            ) from exc
        raise ValueError(
            _INVALID_PORT_RANGE.format(env_var=env_var, host=host)
        ) from exc

    if not host_name:
        raise ValueError(_INVALID_HOST_EMPTY.format(env_var=env_var, host=host))

    if raw_port == "0" or port == 0:
        raise ValueError(_INVALID_PORT_RANGE.format(env_var=env_var, host=host))

    if raw_port is None:
        return host_name, default_port

    assert port is not None
    return host_name, _validate_port(port, host=host, env_var=env_var)


# COMPAT_STABLE_SHIM: naming-only snake_case alias for the shared host/port parser.
def parse_host_and_port(
    host: str,
    *,
    default_port: int,
    env_var: str,
) -> tuple[str, int]:
    """Compatibility alias for parseHostAndPort()."""
    return parseHostAndPort(host, default_port=default_port, env_var=env_var)


__all__ = ["parseHostAndPort", "parse_host_and_port"]
