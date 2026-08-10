"""Validation and normalization for direct-write configure document values."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from meshtastic.cli.context import CliExit, _terminate_cli

POSITION_ALTITUDE_MIN = -(1 << 31)
POSITION_ALTITUDE_MAX = (1 << 31) - 1


class _ConfigureValueHooks(Protocol):
    """Minimum reporting contract required by configure value validation."""

    @property
    def cli_exit(self) -> CliExit:
        """Return the non-returning CLI failure reporter."""


@dataclass(frozen=True, slots=True)
class _DirectConfigureValues:
    """Validated non-transactional values ready for device writes."""

    owner: str | None = None
    owner_short: str | None = None
    location: tuple[float, float, int] | None = None
    altitude_specified: bool = False
    canned_messages: str | None = None
    ringtone: str | None = None
    channel_url: str | None = None
    channel_url_key: str | None = None

    @property
    def has_non_url_writes(self) -> bool:
        """Return whether validated values require a non-URL device write."""
        return any(
            value is not None
            for value in (
                self.owner,
                self.owner_short,
                self.location,
                self.canned_messages,
                self.ringtone,
            )
        )


def _normalize_name(
    hooks: _ConfigureValueHooks,
    configuration: dict[str, Any],
    *,
    key: str,
    empty_message: str,
) -> str | None:
    """Normalize one optional owner name while preserving scalar compatibility."""
    if key not in configuration:
        return None
    raw_value = configuration[key]
    value = "" if raw_value is None else str(raw_value).strip()
    if not value:
        _terminate_cli(hooks.cli_exit, empty_message)
    return value


def _validate_coordinate(
    hooks: _ConfigureValueHooks,
    location: dict[str, Any],
    *,
    key: str,
    limit: float,
) -> float:
    """Return one finite coordinate within its geographic range."""
    raw_value = location[key]
    if isinstance(raw_value, bool):
        _terminate_cli(
            hooks.cli_exit,
            f"location.{key} must be a number, got: {raw_value!r}",
        )
    try:
        value = float(raw_value)
    except (TypeError, ValueError):
        _terminate_cli(
            hooks.cli_exit,
            f"location.{key} must be a number, got: {raw_value!r}",
        )
    if not -limit <= value <= limit:
        _terminate_cli(
            hooks.cli_exit,
            f"location.{key} must be between {-limit:g} and {limit:g}, got: {value}",
        )
    return value


def _validate_altitude(hooks: _ConfigureValueHooks, raw_value: Any) -> int:
    """Return an integral altitude representable by the protobuf int32 field."""
    if isinstance(raw_value, bool):
        _terminate_cli(
            hooks.cli_exit,
            f"location.alt must be an integer, got: {raw_value!r}",
        )
    try:
        altitude = int(raw_value)
    except (OverflowError, TypeError, ValueError):
        _terminate_cli(
            hooks.cli_exit,
            f"location.alt must be an integer, got: {raw_value!r}",
        )
    if isinstance(raw_value, float) and raw_value != altitude:
        _terminate_cli(
            hooks.cli_exit,
            f"location.alt must be an integer, got: {raw_value!r}",
        )
    if not POSITION_ALTITUDE_MIN <= altitude <= POSITION_ALTITUDE_MAX:
        _terminate_cli(
            hooks.cli_exit,
            "location.alt must fit the signed 32-bit position field, "
            f"got: {altitude}",
        )
    return altitude


def _validate_location(
    hooks: _ConfigureValueHooks,
    configuration: dict[str, Any],
) -> tuple[tuple[float, float, int] | None, bool]:
    """Normalize an optional location mapping and altitude-presence flag."""
    if "location" not in configuration:
        return None, False

    location = configuration["location"]
    if not isinstance(location, dict) or not location:
        _terminate_cli(
            hooks.cli_exit,
            "location must be a non-empty mapping with lat, lon, and optional alt",
        )
    non_string_keys = [key for key in location if not isinstance(key, str)]
    if non_string_keys:
        rendered_keys = ", ".join(sorted(repr(key) for key in non_string_keys))
        _terminate_cli(
            hooks.cli_exit,
            f"location keys must be strings, got: {rendered_keys}",
        )
    unknown_keys = set(location) - {"lat", "lon", "alt"}
    if unknown_keys:
        _terminate_cli(
            hooks.cli_exit,
            "location contains unknown keys: "
            f"{', '.join(sorted(unknown_keys))}. Allowed: lat, lon, alt",
        )
    if "lat" not in location or "lon" not in location:
        _terminate_cli(hooks.cli_exit, "location requires both lat and lon")

    latitude = _validate_coordinate(hooks, location, key="lat", limit=90.0)
    longitude = _validate_coordinate(hooks, location, key="lon", limit=180.0)
    altitude_specified = "alt" in location
    altitude = _validate_altitude(hooks, location["alt"]) if altitude_specified else 0
    return (latitude, longitude, altitude), altitude_specified


def _optional_string(
    hooks: _ConfigureValueHooks,
    configuration: dict[str, Any],
    key: str,
) -> str | None:
    """Return one optional top-level value after string type validation."""
    if key not in configuration:
        return None
    value = configuration[key]
    if not isinstance(value, str):
        _terminate_cli(hooks.cli_exit, f"ERROR: {key} must be a string.")
    return value


def _normalize_channel_url(
    hooks: _ConfigureValueHooks,
    configuration: dict[str, Any],
) -> tuple[str | None, str | None]:
    """Return a normalized channel URL and the alias used by the document."""
    channel_url_key: str | None = None
    if "channel_url" in configuration:
        channel_url_key = "channel_url"
    elif "channelUrl" in configuration:
        channel_url_key = "channelUrl"
    if channel_url_key is None:
        return None, None

    raw_channel_url = configuration[channel_url_key]
    if not isinstance(raw_channel_url, str):
        _terminate_cli(
            hooks.cli_exit,
            f"ERROR: {channel_url_key} must be a string.",
        )
    channel_url = raw_channel_url.strip()
    if not channel_url:
        _terminate_cli(
            hooks.cli_exit,
            f"ERROR: {channel_url_key} must not be blank.",
        )
    return channel_url, channel_url_key


def _validate_direct_configuration(
    hooks: _ConfigureValueHooks,
    configuration: dict[str, Any],
) -> _DirectConfigureValues:
    """Validate all direct values before allowing any device mutation."""
    owner = _normalize_name(
        hooks,
        configuration,
        key="owner",
        empty_message=(
            "ERROR: Long Name cannot be empty or contain only whitespace characters"
        ),
    )
    owner_short_key = "owner_short" if "owner_short" in configuration else "ownerShort"
    owner_short = _normalize_name(
        hooks,
        configuration,
        key=owner_short_key,
        empty_message=(
            "ERROR: Short Name cannot be empty or contain only whitespace characters"
        ),
    )
    location, altitude_specified = _validate_location(hooks, configuration)
    channel_url, channel_url_key = _normalize_channel_url(hooks, configuration)
    return _DirectConfigureValues(
        owner=owner,
        owner_short=owner_short,
        location=location,
        altitude_specified=altitude_specified,
        canned_messages=_optional_string(hooks, configuration, "canned_messages"),
        ringtone=_optional_string(hooks, configuration, "ringtone"),
        channel_url=channel_url,
        channel_url_key=channel_url_key,
    )
