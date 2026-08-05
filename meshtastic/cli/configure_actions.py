"""Configure/set execution runtime for the connected Meshtastic CLI.

The public entrypoint remains :mod:`meshtastic.__main__`. This module owns the
configure transaction lifecycle, SetURL stability handling, reconnect verification,
and configure-file action dispatch. Preference parsing remains a separately preserved
entrypoint compatibility seam and is injected explicitly.
"""

from __future__ import annotations

import contextvars
import enum
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, NoReturn

import yaml

import meshtastic.util
from meshtastic._core_constants import BROADCAST_ADDR
from meshtastic.cli.context import CliContext
from meshtastic.configure_verify import (
    _verify_channel_url_against_state,
    _verify_requested_fields,
)
from meshtastic.mesh_interface import MeshInterface

logger = logging.getLogger(__name__)

CONFIG_APPLY_DELAY_SECONDS = 0.5
CONFIG_WRITE_PACE_SECONDS = 0.1
CONFIG_SETURL_DELAY_SECONDS = 2.0
CONFIG_COMMIT_SETTLE_SECONDS = 1.0
CONFIG_RECONNECT_WAIT_SECONDS = 15.0
SETURL_STABILITY_TIMEOUT_SECONDS = 30.0
CONFIGURE_PHASE1_HEADER = (
    "Phase 1: Applying direct configuration "
    "(channel URL updates may trigger reconnect/reboot)..."
)
ALLOWED_CONFIGURE_KEYS = frozenset(
    {
        "owner",
        "owner_short",
        "ownerShort",
        "channel_url",
        "channelUrl",
        "canned_messages",
        "ringtone",
        "location",
        "config",
        "module_config",
    }
)


class ConfigureReconnectResult(enum.Enum):
    """Outcome of local reconnect/config reload verification after configure."""

    RECONNECT_FAILED = "reconnect_failed"
    CONFIG_RELOAD_FAILED = "config_reload_failed"
    VERIFICATION_INCOMPLETE = "verification_incomplete"
    VERIFIED = "verified"


@dataclass(frozen=True, slots=True)
class ConfigureHooks:
    """Entrypoint-owned dependencies used by configure execution.

    Parameters
    ----------
    cli_exit : Callable[[str, int], NoReturn]
        User-facing exit handler.
    cli_print : Callable[[str], None]
        Quiet-aware reporter.
    traverse_config : Callable[..., bool]
        Preference-tree application compatibility seam.
    preflight_mode : contextvars.ContextVar[bool]
        Context flag shared with preference assignment output suppression.
    is_local_destination : Callable[[Any, str], bool]
        Destination classifier.
    """

    cli_exit: Callable[[str, int], NoReturn]
    cli_print: Callable[[str], None]
    traverse_config: Callable[..., bool]
    preflight_mode: contextvars.ContextVar[bool]
    is_local_destination: Callable[[Any, str], bool]
    post_seturl_stability_check: Callable[..., bool]
    post_configure_reconnect_and_verify: Callable[..., ConfigureReconnectResult]
    channel_url_matches_current_device_state: Callable[[Any, str], bool]
    pace_configure_write: Callable[..., None]


@dataclass(frozen=True, slots=True)
class ConfigureActionHooks:
    """Compatibility seams used by connected configure action dispatch."""

    handle_set_command: Callable[[MeshInterface, Any, dict[str, Any]], None]
    handle_configure_command: Callable[
        [MeshInterface, Any, dict[str, Any]], tuple[bool, bool]
    ]
    export_config: Callable[[MeshInterface], str]
    cli_exit: Callable[[str, int], NoReturn]
    cli_print: Callable[[str], None]


def _post_configure_reconnect_and_verify(
    interface: MeshInterface,
    *,
    timeout: float,
    node_dest: str,
    verify_channel_url: str | None = None,
    verify_config_fields: dict[str, dict[str, Any]] | None = None,
    verify_module_config_fields: dict[str, dict[str, Any]] | None = None,
    verify_channel_url_against_state: Callable[..., bool] = (
        _verify_channel_url_against_state
    ),
) -> ConfigureReconnectResult:
    """Reconnect after a configure commit, reload config, and verify values.

    After ``commitSettingsTransaction()``, the firmware may reboot the device.
    This helper:

    1. Waits for the interface to disconnect and reconnect within *timeout*.
    2. Calls ``waitForConfig()`` to reload the device configuration.
    3. If any verification targets were provided (channel URL, config fields,
       or module config fields), performs value-aware comparison of the
       explicitly requested settings against what the device reports.

    Returns a ConfigureReconnectResult indicating the outcome.
    """
    deadline = time.monotonic() + timeout

    disconnect_window = 2.0
    logger.debug(
        "Waiting up to %.1fs for device disconnect (reboot indication)...",
        disconnect_window,
    )
    disconnect_deadline = time.monotonic() + disconnect_window
    disconnected = False
    while time.monotonic() < disconnect_deadline:
        if not interface.isConnected.is_set():
            disconnected = True
            logger.info("Device disconnected (reboot indication received).")
            break
        time.sleep(0.2)

    if not disconnected:
        logger.debug(
            "No disconnect detected within %.1fs; device may not require reboot.",
            disconnect_window,
        )

    reconnect_deadline = deadline
    if disconnected:
        logger.debug(
            "Waiting up to %.1fs for device reconnect...",
            reconnect_deadline - time.monotonic(),
        )
    while time.monotonic() < reconnect_deadline:
        if interface.isConnected.is_set():
            logger.info("Device reconnected.")
            break
        time.sleep(0.2)

    if not interface.isConnected.is_set():
        logger.warning(
            "Device did not reconnect within %.1fs after configure commit. "
            "Configuration may still be applying.",
            timeout,
        )
        return ConfigureReconnectResult.RECONNECT_FAILED

    try:
        interface.waitForConfig()
        logger.info("Device config reloaded after reboot.")
    except Exception:
        logger.warning(
            "Device reconnected but config reload failed; "
            "configuration may still be applying.",
            exc_info=True,
        )
        return ConfigureReconnectResult.CONFIG_RELOAD_FAILED

    has_verification = (
        verify_channel_url or verify_config_fields or verify_module_config_fields
    )
    if not has_verification:
        return ConfigureReconnectResult.VERIFIED

    if not disconnected:
        try:
            _refresh_no_disconnect_verify_state(
                interface.getNode(node_dest),
                verify_channel_url=verify_channel_url,
                verify_config_fields=verify_config_fields,
                verify_module_config_fields=verify_module_config_fields,
            )
            interface.waitForConfig()
            logger.debug(
                "No disconnect observed; touched config/channel state refreshed before verification."
            )
        except Exception:
            logger.warning(
                "No-disconnect verify refresh failed while reloading config.",
                exc_info=True,
            )
            return ConfigureReconnectResult.CONFIG_RELOAD_FAILED

    try:
        result = _verify_post_reconnect_config(
            interface,
            node_dest,
            verify_channel_url=verify_channel_url,
            verify_config_fields=verify_config_fields,
            verify_module_config_fields=verify_module_config_fields,
            verify_channel_url_against_state=verify_channel_url_against_state,
        )
    except Exception:
        logger.warning(
            "Post-reconnect verification failed unexpectedly.",
            exc_info=True,
        )
        return ConfigureReconnectResult.VERIFICATION_INCOMPLETE

    return result


def _post_seturl_stability_check(
    interface: MeshInterface,
    *,
    timeout: float = 15.0,
) -> bool:
    _MAX_STABILITY_ATTEMPTS = 3
    _STABILITY_WINDOW_SECONDS = 1.5
    _RECONNECT_WAIT_SECONDS = 10.0

    deadline = time.monotonic() + timeout

    is_connected_event = getattr(interface, "isConnected", None)

    def _event_is_set() -> bool:
        return bool(
            is_connected_event is not None
            and hasattr(is_connected_event, "is_set")
            and is_connected_event.is_set()
        )

    def _event_wait(timeout_seconds: float) -> bool:
        return bool(
            is_connected_event is not None
            and hasattr(is_connected_event, "wait")
            and is_connected_event.wait(timeout_seconds)
        )

    def _trigger_reconnect() -> bool:
        reconnect = getattr(interface, "_attempt_reconnect", None)
        if callable(reconnect):
            try:
                if reconnect():
                    return _event_is_set()
            except Exception:
                logger.debug(
                    "post-setURL reconnect hook failed.",
                    exc_info=True,
                )
        connect = getattr(interface, "connect", None)
        if callable(connect):
            try:
                connect()
            except Exception:
                logger.debug(
                    "post-setURL connect() trigger failed.",
                    exc_info=True,
                )
        return _event_is_set()

    for _attempt in range(_MAX_STABILITY_ATTEMPTS):
        if time.monotonic() >= deadline:
            return False

        if not _event_is_set():
            _trigger_reconnect()
            remaining = deadline - time.monotonic()
            if remaining > 0:
                _event_wait(min(_RECONNECT_WAIT_SECONDS, remaining))

        if not _event_is_set():
            logger.warning(
                "Transport not connected after setURL (attempt %d/%d)",
                _attempt + 1,
                _MAX_STABILITY_ATTEMPTS,
            )
            continue

        stability_end = time.monotonic() + _STABILITY_WINDOW_SECONDS
        stable = True
        while time.monotonic() < stability_end:
            if not _event_is_set():
                stable = False
                break
            time.sleep(0.1)

        if not stable:
            logger.warning(
                "Transport dropped during stability window (attempt %d/%d)",
                _attempt + 1,
                _MAX_STABILITY_ATTEMPTS,
            )
            continue

        try:
            interface.waitForConfig()
            return True
        except Exception:
            logger.warning(
                "Config reload failed after setURL (attempt %d/%d)",
                _attempt + 1,
                _MAX_STABILITY_ATTEMPTS,
                exc_info=True,
            )
            continue

    return False


def _validate_non_empty_mapping_sections(
    hooks: ConfigureHooks,
    *,
    top_level_key: str,
    section_mapping: dict[str, Any],
) -> dict[str, dict[str, Any]]:
    """Validate that each section payload is a mapping.

    Empty mappings (e.g., ``audio: {}``) are allowed — they represent
    protobuf default values and are emitted by ``--export-config``.
    """
    validated_sections: dict[str, dict[str, Any]] = {}
    for section_name, section_value in section_mapping.items():
        if not isinstance(section_value, dict):
            hooks.cli_exit(
                f"ERROR: '{top_level_key}.{section_name}' must be a non-empty mapping, got "
                f"{type(section_value).__name__}"
            )
        validated_sections[section_name] = section_value
    return validated_sections


def _preflight_configure_sections(
    hooks: ConfigureHooks,
    target_node: Any,
    *,
    config_sections: dict[str, dict[str, Any]],
    module_config_sections: dict[str, dict[str, Any]],
) -> None:
    """Validate configuration values on protobuf copies before device mutation."""
    roots = (
        ("config", target_node.localConfig, config_sections),
        ("module_config", target_node.moduleConfig, module_config_sections),
    )
    token = hooks.preflight_mode.set(True)
    try:
        for top_level_key, source_message, sections in roots:
            if not sections:
                continue
            candidate = type(source_message)()
            candidate.CopyFrom(source_message)
            for section, section_values in sections.items():
                failed_fields: list[str] = []
                applied = hooks.traverse_config(
                    section,
                    section_values,
                    candidate,
                    failed_fields=failed_fields,
                )
                if applied:
                    continue
                field_suffix = (
                    f" Invalid field: {failed_fields[0]}." if failed_fields else ""
                )
                hooks.cli_exit(
                    f"Failed to apply {top_level_key} section {section!r} "
                    f"due to structural errors.{field_suffix}"
                )
    finally:
        hooks.preflight_mode.reset(token)


def _refresh_no_disconnect_verify_state(
    target_node: Any,
    *,
    verify_channel_url: str | None,
    verify_config_fields: dict[str, dict[str, Any]] | None,
    verify_module_config_fields: dict[str, dict[str, Any]] | None,
) -> None:
    """Invalidate touched cached state and request fresh values for Phase 3 verification."""
    request_config = getattr(target_node, "requestConfig", None)

    for section_name in verify_config_fields or {}:
        section_snake = meshtastic.util.camel_to_snake(section_name)
        field_desc = target_node.localConfig.DESCRIPTOR.fields_by_name.get(
            section_snake
        )
        if field_desc is None:
            logger.warning(
                "Skipping config refresh for unknown section %r.",
                section_name,
            )
            continue
        target_node.localConfig.ClearField(section_snake)
        if callable(request_config):
            request_config(field_desc)

    for section_name in verify_module_config_fields or {}:
        section_snake = meshtastic.util.camel_to_snake(section_name)
        field_desc = target_node.moduleConfig.DESCRIPTOR.fields_by_name.get(
            section_snake
        )
        if field_desc is None:
            logger.warning(
                "Skipping module_config refresh for unknown section %r.",
                section_name,
            )
            continue
        target_node.moduleConfig.ClearField(section_snake)
        if callable(request_config):
            request_config(field_desc)

    if verify_channel_url:
        target_node.channels = None
        target_node.partialChannels = []
        request_channels = getattr(target_node, "requestChannels", None)
        if callable(request_channels):
            request_channels(0)


def _channel_url_matches_current_device_state(
    target_node: Any,
    requested_channel_url: str,
    *,
    verify_channel_url_against_state: Callable[..., bool] = (
        _verify_channel_url_against_state
    ),
) -> bool:
    """Return True when requested channel URL already matches loaded device state."""
    local_config = getattr(target_node, "localConfig", None)
    has_field = getattr(local_config, "HasField", None)
    if local_config is None or not callable(has_field) or not has_field("lora"):
        return False
    return verify_channel_url_against_state(
        requested_channel_url,
        device_channels=getattr(target_node, "channels", None),
        device_lora_config=local_config.lora,
        emit_warnings=False,
    )


def _flatten_leaf_paths(prefix: str, mapping: dict[str, Any]) -> list[str]:
    """Recursively flatten a nested mapping into dotted leaf paths."""
    paths: list[str] = []
    for key, value in mapping.items():
        dotted = f"{prefix}.{key}"
        if isinstance(value, dict) and value:
            paths.extend(_flatten_leaf_paths(dotted, value))
        else:
            paths.append(dotted)
    return paths


def _verify_config_sections(
    config_fields: dict[str, dict[str, Any]],
    proto_config: Any,
    label: str,
    verified_fields: list[str] | None = None,
) -> bool:
    for section_name, yaml_values in config_fields.items():
        section_snake = meshtastic.util.camel_to_snake(section_name)
        if not proto_config.HasField(section_snake):
            logger.warning(
                "%s section %r not present after reload.",
                label,
                section_name,
            )
            return False
        proto_section = getattr(proto_config, section_snake)
        mismatches = _verify_requested_fields(yaml_values, proto_section, section_name)
        if mismatches:
            logger.warning(
                "%s section %r field mismatches: %s",
                label,
                section_name,
                ", ".join(mismatches),
            )
            return False
        if verified_fields is not None:
            verified_fields.extend(_flatten_leaf_paths(section_snake, yaml_values))
        logger.debug(
            "%s section %r verified (all requested field values match).",
            label,
            section_name,
        )
    return True


def _verify_post_reconnect_config(
    interface: MeshInterface,
    node_dest: str,
    *,
    verify_channel_url: str | None = None,
    verify_config_fields: dict[str, dict[str, Any]] | None = None,
    verify_module_config_fields: dict[str, dict[str, Any]] | None = None,
    verify_channel_url_against_state: Callable[..., bool] = (
        _verify_channel_url_against_state
    ),
) -> ConfigureReconnectResult:
    if not interface.isConnected.is_set():
        logger.warning("Post-reconnect verification skipped: transport disconnected.")
        return ConfigureReconnectResult.VERIFICATION_INCOMPLETE

    target_node = interface.getNode(node_dest)
    verified_fields: list[str] = []

    if verify_channel_url:
        local_config = getattr(target_node, "localConfig", None)
        has_field = getattr(local_config, "HasField", None)
        device_lora_config = (
            local_config.lora
            if local_config is not None and callable(has_field) and has_field("lora")
            else None
        )
        if not verify_channel_url_against_state(
            verify_channel_url,
            device_channels=getattr(target_node, "channels", None),
            device_lora_config=device_lora_config,
        ):
            logger.warning(
                "Channel URL verification: device state does not match requested URL."
            )
            return ConfigureReconnectResult.VERIFICATION_INCOMPLETE
        verified_fields.append("channel_url")

    if verify_config_fields and not _verify_config_sections(
        verify_config_fields,
        target_node.localConfig,
        "Config",
        verified_fields=verified_fields,
    ):
        return ConfigureReconnectResult.VERIFICATION_INCOMPLETE

    if verify_module_config_fields and not _verify_config_sections(
        verify_module_config_fields,
        target_node.moduleConfig,
        "Module config",
        verified_fields=verified_fields,
    ):
        return ConfigureReconnectResult.VERIFICATION_INCOMPLETE

    if not interface.isConnected.is_set():
        logger.warning(
            "Post-reconnect verification did not complete: transport disconnected."
        )
        return ConfigureReconnectResult.VERIFICATION_INCOMPLETE

    if verified_fields:
        logger.info("Verified: %s", ", ".join(verified_fields))

    return ConfigureReconnectResult.VERIFIED


def _pace_configure_write(
    remaining_writes: int,
    *,
    sleep_fn: Callable[[float], None] = time.sleep,
) -> None:
    """Yield briefly between section writes while keeping transactions short."""
    if remaining_writes > 0:
        sleep_fn(CONFIG_WRITE_PACE_SECONDS)


def _apply_configure_channel_url(
    hooks: ConfigureHooks,
    target_node: Any,
    raw_channel_url: Any,
    *,
    config_key: str,
) -> bool:
    """Validate and apply one configured channel URL without exposing it."""
    if not isinstance(raw_channel_url, str):
        hooks.cli_exit(f"ERROR: {config_key} must be a string.")
    requested_channel_url = raw_channel_url.strip()
    if not requested_channel_url:
        hooks.cli_exit(f"ERROR: {config_key} must not be blank.")

    if hooks.channel_url_matches_current_device_state(
        target_node, requested_channel_url
    ):
        hooks.cli_print("Channel url already matches device state; skipping apply.")
        logger.info("Skipping setURL apply because channel URL already matches.")
        return False

    hooks.cli_print("Setting channel url to <redacted>")
    target_node.setURL(requested_channel_url)
    time.sleep(CONFIG_SETURL_DELAY_SECONDS)
    return True


def _handle_configure_command(
    hooks: ConfigureHooks,
    interface: MeshInterface,
    args: Any,
    getNode_kwargs: dict[str, Any],
) -> tuple[bool, bool]:
    try:
        with open(args.configure[0], encoding="utf8") as file:
            raw_text = file.read()
        configuration = yaml.safe_load(raw_text)
    except (yaml.YAMLError, UnicodeDecodeError) as exc:
        hooks.cli_exit(f"ERROR: Failed to parse YAML configuration: {exc}")

    if configuration is None:
        hooks.cli_exit("ERROR: YAML configuration file is empty")
    if not isinstance(configuration, dict):
        hooks.cli_exit(
            f"ERROR: YAML configuration must be a mapping/dictionary, got {type(configuration).__name__}"
        )
    if not configuration:
        hooks.cli_exit("ERROR: Configuration file is empty; nothing to configure.")
    _unknown_keys = set(configuration.keys()) - ALLOWED_CONFIGURE_KEYS
    if _unknown_keys:
        hooks.cli_exit(
            f"ERROR: Unknown top-level key(s) in YAML: {', '.join(sorted(_unknown_keys))}"
        )

    if "channel_url" in configuration and "channelUrl" in configuration:
        hooks.cli_exit(
            "ERROR: Cannot specify both 'channel_url' and 'channelUrl' in the same configuration file; use one."
        )
    if "owner_short" in configuration and "ownerShort" in configuration:
        hooks.cli_exit(
            "ERROR: Cannot specify both 'owner_short' and 'ownerShort' in the same configuration file; use one."
        )

    # Pre-validate config/module_config shapes before any Phase-1 mutations.
    validated_config_sections: dict[str, dict[str, Any]] = {}
    validated_module_config_sections: dict[str, dict[str, Any]] = {}
    if "config" in configuration:
        _cfg_val = configuration["config"]
        if not isinstance(_cfg_val, dict) or not _cfg_val:
            hooks.cli_exit(
                f"ERROR: 'config' must be a non-empty mapping, got "
                f"{type(_cfg_val).__name__}{' (empty)' if isinstance(_cfg_val, dict) else ''}"
            )
        validated_config_sections = _validate_non_empty_mapping_sections(
            hooks,
            top_level_key="config",
            section_mapping=_cfg_val,
        )
    if "module_config" in configuration:
        _mcfg_val = configuration["module_config"]
        if not isinstance(_mcfg_val, dict) or not _mcfg_val:
            hooks.cli_exit(
                f"ERROR: 'module_config' must be a non-empty mapping, got "
                f"{type(_mcfg_val).__name__}{' (empty)' if isinstance(_mcfg_val, dict) else ''}"
            )
        validated_module_config_sections = _validate_non_empty_mapping_sections(
            hooks,
            top_level_key="module_config",
            section_mapping=_mcfg_val,
        )

    target_node = interface.getNode(args.dest, False, **getNode_kwargs)
    if validated_config_sections or validated_module_config_sections:
        _preflight_configure_sections(
            hooks,
            target_node,
            config_sections=validated_config_sections,
            module_config_sections=validated_module_config_sections,
        )

    phase1_started = False
    phase1_may_reconnect = False
    seturl_executed = False

    if "owner" in configuration:
        if not phase1_started:
            hooks.cli_print(CONFIGURE_PHASE1_HEADER)
            phase1_started = True
        owner_name = str(configuration["owner"]).strip()
        if not owner_name:
            hooks.cli_exit(
                "ERROR: Long Name cannot be empty or contain only whitespace characters"
            )
        hooks.cli_print(f"Setting device owner to {owner_name}")
        target_node.setOwner(long_name=owner_name)
        time.sleep(CONFIG_APPLY_DELAY_SECONDS)

    if "owner_short" in configuration:
        if not phase1_started:
            hooks.cli_print(CONFIGURE_PHASE1_HEADER)
            phase1_started = True
        owner_short_name = str(configuration["owner_short"]).strip()
        if not owner_short_name:
            hooks.cli_exit(
                "ERROR: Short Name cannot be empty or contain only whitespace characters"
            )
        hooks.cli_print(f"Setting device owner short to {owner_short_name}")
        target_node.setOwner(long_name=None, short_name=owner_short_name)
        time.sleep(CONFIG_APPLY_DELAY_SECONDS)

    if "ownerShort" in configuration:
        if not phase1_started:
            hooks.cli_print(CONFIGURE_PHASE1_HEADER)
            phase1_started = True
        owner_short_name = str(configuration["ownerShort"]).strip()
        if not owner_short_name:
            hooks.cli_exit(
                "ERROR: Short Name cannot be empty or contain only whitespace characters"
            )
        hooks.cli_print(f"Setting device owner short to {owner_short_name}")
        target_node.setOwner(long_name=None, short_name=owner_short_name)
        time.sleep(CONFIG_APPLY_DELAY_SECONDS)

    if "location" in configuration:
        if not phase1_started:
            hooks.cli_print(CONFIGURE_PHASE1_HEADER)
            phase1_started = True
        _loc = configuration["location"]
        if not isinstance(_loc, dict) or not _loc:
            hooks.cli_exit(
                "location must be a non-empty mapping with lat, lon, and optional alt"
            )
        _allowed_loc_keys = {"lat", "lon", "alt"}
        _unknown_loc_keys = set(_loc.keys()) - _allowed_loc_keys
        if _unknown_loc_keys:
            hooks.cli_exit(
                f"location contains unknown keys: {', '.join(sorted(_unknown_loc_keys))}. "
                f"Allowed: lat, lon, alt"
            )
        if "lat" not in _loc or "lon" not in _loc:
            hooks.cli_exit("location requires both lat and lon")
        try:
            lat = float(_loc["lat"])
        except (ValueError, TypeError):
            hooks.cli_exit(f"location.lat must be a number, got: {_loc['lat']!r}")
        try:
            lon = float(_loc["lon"])
        except (ValueError, TypeError):
            hooks.cli_exit(f"location.lon must be a number, got: {_loc['lon']!r}")
        alt = 0
        if "alt" in _loc:
            try:
                alt = int(_loc["alt"])
            except (ValueError, TypeError):
                hooks.cli_exit(f"location.alt must be an integer, got: {_loc['alt']!r}")
            hooks.cli_print(f"Fixing altitude at {alt} meters")
        hooks.cli_print(f"Fixing latitude at {lat} degrees")
        hooks.cli_print(f"Fixing longitude at {lon} degrees")
        hooks.cli_print("Setting device position")
        target_node.setFixedPosition(lat, lon, alt)
        time.sleep(CONFIG_APPLY_DELAY_SECONDS)

    if "canned_messages" in configuration:
        if not phase1_started:
            hooks.cli_print(CONFIGURE_PHASE1_HEADER)
            phase1_started = True
        hooks.cli_print(
            f"Setting canned message messages to {configuration['canned_messages']}",
        )
        target_node.set_canned_message(configuration["canned_messages"])
        time.sleep(CONFIG_APPLY_DELAY_SECONDS)

    if "ringtone" in configuration:
        if not phase1_started:
            hooks.cli_print(CONFIGURE_PHASE1_HEADER)
            phase1_started = True
        hooks.cli_print(f"Setting ringtone to {configuration['ringtone']}")
        target_node.set_ringtone(configuration["ringtone"])
        time.sleep(CONFIG_APPLY_DELAY_SECONDS)

    channel_url_key = "channel_url" if "channel_url" in configuration else "channelUrl"
    if channel_url_key in configuration:
        if not phase1_started:
            hooks.cli_print(CONFIGURE_PHASE1_HEADER)
            phase1_started = True
        seturl_executed = _apply_configure_channel_url(
            hooks,
            target_node,
            configuration[channel_url_key],
            config_key=channel_url_key,
        )
        phase1_may_reconnect = seturl_executed

    if phase1_started:
        hooks.cli_print("Phase 1 complete.")

    settings_transaction_started = False
    has_valid_config_section = bool(
        validated_config_sections or validated_module_config_sections
    )
    if seturl_executed and has_valid_config_section:
        if hooks.is_local_destination(interface, args.dest):
            if not hooks.post_seturl_stability_check(
                interface, timeout=SETURL_STABILITY_TIMEOUT_SECONDS
            ):
                hooks.cli_exit(
                    "ERROR: channel_url applied, but transport did not stabilize "
                    "for additional configuration writes; aborting before Phase 2."
                )
        else:
            hooks.cli_exit(
                "ERROR: Combining channel_url with additional configuration "
                "writes is not supported for remote nodes. Apply channel_url "
                "and configuration in separate operations."
            )
    if has_valid_config_section:
        hooks.cli_print(
            "Phase 2: Applying configuration transaction (may trigger device reboot)..."
        )
        target_node.beginSettingsTransaction()
        settings_transaction_started = True

    remaining_config_writes = len(validated_config_sections) + len(
        validated_module_config_sections
    )

    if validated_config_sections:
        localConfig = target_node.localConfig
        for section, section_values in validated_config_sections.items():
            failed_config_fields: list[str] = []
            applied = hooks.traverse_config(
                section,
                section_values,
                localConfig,
                failed_fields=failed_config_fields,
            )
            if failed_config_fields:
                logger.warning(
                    "Skipped %d unknown field(s) in config section %s: %s",
                    len(failed_config_fields),
                    section,
                    ", ".join(repr(f) for f in failed_config_fields),
                )
            if not applied:
                hooks.cli_exit(
                    f"Failed to apply config section {section!r} due to structural errors."
                )
            target_node.writeConfig(meshtastic.util.camel_to_snake(section))
            remaining_config_writes -= 1
            hooks.pace_configure_write(remaining_config_writes)

    if validated_module_config_sections:
        moduleConfig = target_node.moduleConfig
        for section, section_values in validated_module_config_sections.items():
            failed_module_fields: list[str] = []
            applied = hooks.traverse_config(
                section,
                section_values,
                moduleConfig,
                failed_fields=failed_module_fields,
            )
            if failed_module_fields:
                logger.warning(
                    "Skipped %d unknown field(s) in module_config section %s: %s",
                    len(failed_module_fields),
                    section,
                    ", ".join(repr(f) for f in failed_module_fields),
                )
            if not applied:
                hooks.cli_exit(
                    f"Failed to apply module_config section {section!r} due to structural errors."
                )
            target_node.writeConfig(meshtastic.util.camel_to_snake(section))
            remaining_config_writes -= 1
            hooks.pace_configure_write(remaining_config_writes)

    if settings_transaction_started:
        target_node.commitSettingsTransaction()
        time.sleep(CONFIG_COMMIT_SETTLE_SECONDS)
        hooks.cli_print(
            "Configuration transaction committed. Device may reboot to apply changes."
        )

    if settings_transaction_started:
        _verify_channel_url = configuration.get("channel_url") or configuration.get(
            "channelUrl"
        )
        _verify_config_fields = validated_config_sections or None
        _verify_module_config_fields = validated_module_config_sections or None
        if hooks.is_local_destination(interface, args.dest):
            _reconnect_result = hooks.post_configure_reconnect_and_verify(
                interface,
                timeout=CONFIG_RECONNECT_WAIT_SECONDS,
                node_dest=args.dest,
                verify_channel_url=_verify_channel_url,
                verify_config_fields=_verify_config_fields,
                verify_module_config_fields=_verify_module_config_fields,
            )
            if _reconnect_result == ConfigureReconnectResult.VERIFIED:
                hooks.cli_print(
                    "Phase 3: Device reconnected and config reloaded. All settings verified."
                )
            elif _reconnect_result == ConfigureReconnectResult.VERIFICATION_INCOMPLETE:
                hooks.cli_print(
                    "Phase 3: Device reconnected and config reloaded. "
                    "Could not fully verify applied settings."
                )
            elif _reconnect_result == ConfigureReconnectResult.CONFIG_RELOAD_FAILED:
                hooks.cli_print(
                    "Phase 3: Device reconnected but config reload failed. "
                    "Settings may still be applying."
                )
            elif _reconnect_result == ConfigureReconnectResult.RECONNECT_FAILED:
                hooks.cli_print(
                    "Phase 3: Device did not reconnect within timeout. "
                    "Configuration may still be applying."
                )
        else:
            hooks.cli_print(
                "Phase 3: Reboot/reconnect verification skipped for remote target. "
                "Local transport state does not confirm remote node reload status."
            )
    else:
        if phase1_may_reconnect:
            hooks.cli_print(
                "Configuration applied. Channel URL updates may still trigger reconnect/reboot."
            )
        else:
            hooks.cli_print("Configuration applied (no reboot expected).")

    return settings_transaction_started, (
        seturl_executed and hooks.is_local_destination(interface, args.dest)
    )


def handle_configure_actions(
    context: CliContext,
    hooks: ConfigureActionHooks,
) -> None:
    """Execute ``--set``, ``--configure``, and ``--export-config`` actions.

    Parameters
    ----------
    context : CliContext
        Connected invocation state and accumulated lifecycle outcome.
    hooks : ConfigureActionHooks
        Entrypoint-owned compatibility seams for preference/config handlers.
    """
    args = context.args
    outcome = context.outcome

    if args.set:
        outcome.close_now = True
        outcome.wait_for_ack_nak = True
        hooks.handle_set_command(context.interface, args, context.get_node_kwargs)

    if args.configure:
        outcome.close_now = True
        outcome.wait_for_ack_nak = True
        settings_transaction_started, phase1_channel_url_applied = (
            hooks.handle_configure_command(
                context.interface,
                args,
                context.get_node_kwargs,
            )
        )
        if settings_transaction_started or phase1_channel_url_applied:
            outcome.wait_for_ack_nak = False
            outcome.skip_ack_wait = True

    if not args.export_config:
        return

    if args.dest != BROADCAST_ADDR:
        print("Exporting configuration of remote nodes is not supported.")
        outcome.stop_processing = True
        return

    outcome.close_now = True
    config_text = hooks.export_config(context.interface)
    if args.export_config == "-":
        print(config_text)
        return

    try:
        with open(args.export_config, "w", encoding="utf-8") as output_file:
            output_file.write(config_text)
    except OSError as exc:
        hooks.cli_exit(f"ERROR: Failed to write config file: {exc}", 1)
    hooks.cli_print(f"Exported configuration to {args.export_config}")
