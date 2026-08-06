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
from typing import Any, NamedTuple

import yaml

import meshtastic.util
from meshtastic.cli.context import CliContext, CliExit
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
CONFIG_DISCONNECT_WINDOW_SECONDS = 2.0
CONFIG_POLL_INTERVAL_SECONDS = 0.2
SETURL_STABILITY_TIMEOUT_SECONDS = 30.0
SETURL_STABILITY_MAX_ATTEMPTS = 3
SETURL_STABILITY_WINDOW_SECONDS = 1.5
SETURL_RECONNECT_WAIT_SECONDS = 10.0
SETURL_STABILITY_POLL_SECONDS = 0.1
POSITION_ALTITUDE_MIN = -(1 << 31)
POSITION_ALTITUDE_MAX = (1 << 31) - 1
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


class _ConfigureCommandResult(NamedTuple):
    """Outcome flags returned by one ``--configure`` execution."""

    settings_transaction_started: bool
    phase1_channel_url_applied: bool


@dataclass(frozen=True, slots=True)
class _Phase1ConfigureValues:
    """Validated direct-write values that are safe to apply in Phase 1.

    Attributes
    ----------
    owner : str | None
        Normalized long owner name when requested.
    owner_short : str | None
        Normalized short owner name when requested.
    location : tuple[float, float, int] | None
        Validated latitude, longitude, and altitude when requested.
    canned_messages : str | None
        Canned-message payload when requested.
    ringtone : str | None
        Ringtone payload when requested.
    channel_url : str | None
        Stripped channel URL when requested.
    """

    owner: str | None = None
    owner_short: str | None = None
    location: tuple[float, float, int] | None = None
    canned_messages: str | None = None
    ringtone: str | None = None
    channel_url: str | None = None


class _PreparedConfigureDocument(NamedTuple):
    """Validated YAML document and normalized values ready for device access.

    Attributes
    ----------
    configuration : dict[str, Any]
        Validated top-level YAML mapping.
    phase1 : _Phase1ConfigureValues
        Normalized direct-write values.
    config_sections : dict[str, dict[str, Any]]
        Validated LocalConfig section mappings.
    module_config_sections : dict[str, dict[str, Any]]
        Validated LocalModuleConfig section mappings.
    """

    configuration: dict[str, Any]
    phase1: _Phase1ConfigureValues
    config_sections: dict[str, dict[str, Any]]
    module_config_sections: dict[str, dict[str, Any]]


@dataclass(frozen=True, slots=True)
class ConfigureHooks:
    """Entrypoint-owned dependencies used by configure execution.

    Parameters
    ----------
    cli_exit : CliExit
        User-facing exit handler with an optional status code.
    cli_print : Callable[[str], None]
        Quiet-aware reporter.
    traverse_config : Callable[..., bool]
        Preference-tree application compatibility seam.
    preflight_mode : contextvars.ContextVar[bool]
        Context flag shared with preference assignment output suppression.
    is_local_destination : Callable[[Any, str], bool]
        Destination classifier.
    post_seturl_stability_check : Callable[..., bool]
        Transport-stability verifier used after a local channel URL write.
    post_configure_reconnect_and_verify : Callable[..., ConfigureReconnectResult]
        Reconnect and value-verification helper used after transaction commit.
    channel_url_matches_current_device_state : Callable[[Any, str], bool]
        Comparator used to skip redundant channel URL writes.
    pace_configure_write : Callable[..., None]
        Inter-write pacing hook used while applying a transaction.
    """

    cli_exit: CliExit
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
    cli_exit: CliExit
    cli_print: Callable[[str], None]
    is_local_destination: Callable[[Any, str], bool]


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

    Parameters
    ----------
    interface : MeshInterface
        Connected interface observed for disconnect/reconnect.
    timeout : float
        Total reconnect budget in seconds.
    node_dest : str
        Destination whose configuration is reloaded and verified.
    verify_channel_url : str | None
        Normalized channel URL expected after reload.
    verify_config_fields : dict[str, dict[str, Any]] | None
        Requested local-config sections and fields to compare.
    verify_module_config_fields : dict[str, dict[str, Any]] | None
        Requested module-config sections and fields to compare.
    verify_channel_url_against_state : Callable[..., bool]
        Channel-state comparison seam.

    Returns
    -------
    ConfigureReconnectResult
        Reconnect, reload, and requested-value verification outcome.
    """
    deadline = time.monotonic() + timeout

    disconnect_window = CONFIG_DISCONNECT_WINDOW_SECONDS
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
        time.sleep(CONFIG_POLL_INTERVAL_SECONDS)

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
        time.sleep(CONFIG_POLL_INTERVAL_SECONDS)

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
    timeout: float = SETURL_STABILITY_TIMEOUT_SECONDS,
) -> bool:
    """Confirm that the transport stabilizes after a local ``setURL`` write.

    Parameters
    ----------
    interface : MeshInterface
        Connected interface whose transport and config reload are observed.
    timeout : float
        Total reconnect/stability budget in seconds.

    Returns
    -------
    bool
        ``True`` when the transport remains connected through a stability window
        and ``waitForConfig()`` succeeds; otherwise ``False``.
    """
    deadline = time.monotonic() + timeout

    is_connected_event = getattr(interface, "isConnected", None)

    def _event_is_set() -> bool:
        """Return whether the interface exposes a currently-set connection event."""
        return bool(
            is_connected_event is not None
            and hasattr(is_connected_event, "is_set")
            and is_connected_event.is_set()
        )

    def _event_wait(timeout_seconds: float) -> bool:
        """Wait on the interface connection event when that seam is available.

        Parameters
        ----------
        timeout_seconds : float
            Maximum wait in seconds.

        Returns
        -------
        bool
            Event wait result, or ``False`` when no compatible event exists.
        """
        return bool(
            is_connected_event is not None
            and hasattr(is_connected_event, "wait")
            and is_connected_event.wait(timeout_seconds)
        )

    def _trigger_reconnect() -> bool:
        """Best-effort trigger one reconnect attempt and report connected state.

        Returns
        -------
        bool
            ``True`` when the interface is connected after the reconnect trigger.
        """
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

    for _attempt in range(SETURL_STABILITY_MAX_ATTEMPTS):
        if time.monotonic() >= deadline:
            return False

        if not _event_is_set():
            _trigger_reconnect()
            remaining = deadline - time.monotonic()
            if remaining > 0:
                _event_wait(min(SETURL_RECONNECT_WAIT_SECONDS, remaining))

        if not _event_is_set():
            logger.warning(
                "Transport not connected after setURL (attempt %d/%d)",
                _attempt + 1,
                SETURL_STABILITY_MAX_ATTEMPTS,
            )
            continue

        stability_end = time.monotonic() + SETURL_STABILITY_WINDOW_SECONDS
        stable = True
        while time.monotonic() < stability_end:
            if not _event_is_set():
                stable = False
                break
            time.sleep(SETURL_STABILITY_POLL_SECONDS)

        if not stable:
            logger.warning(
                "Transport dropped during stability window (attempt %d/%d)",
                _attempt + 1,
                SETURL_STABILITY_MAX_ATTEMPTS,
            )
            continue

        try:
            interface.waitForConfig()
            return True
        except Exception:
            logger.warning(
                "Config reload failed after setURL (attempt %d/%d)",
                _attempt + 1,
                SETURL_STABILITY_MAX_ATTEMPTS,
                exc_info=True,
            )
            continue

    return False


def _validate_mapping_sections(
    hooks: ConfigureHooks,
    *,
    top_level_key: str,
    section_mapping: dict[str, Any],
) -> dict[str, dict[str, Any]]:
    """Validate that each section payload is a mapping.

    Parameters
    ----------
    hooks : ConfigureHooks
        CLI exit/reporting hooks used for validation failures.
    top_level_key : str
        Parent YAML key used to build diagnostics.
    section_mapping : dict[str, Any]
        Section names and their raw YAML payloads.

    Returns
    -------
    dict[str, dict[str, Any]]
        The same section mapping narrowed to mapping-valued payloads. Empty
        mappings (for example ``audio: {}``) are valid because exports use them
        to represent protobuf default values.
    """
    validated_sections: dict[str, dict[str, Any]] = {}
    for section_name, section_value in section_mapping.items():
        if not isinstance(section_value, dict):
            hooks.cli_exit(
                f"ERROR: '{top_level_key}.{section_name}' must be a mapping, got "
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
        target_node._invalidate_channel_cache()  # noqa: SLF001 - Node cache owner API
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
    """Verify requested configuration sections against a reloaded protobuf.

    Parameters
    ----------
    config_fields : dict[str, dict[str, Any]]
        Requested section/value mappings from the configure document.
    proto_config : Any
        Reloaded protobuf configuration root.
    label : str
        Human-readable label used in diagnostics.
    verified_fields : list[str] | None
        Optional list mutated in place with verified dotted leaf paths.

    Returns
    -------
    bool
        ``True`` only when every requested section and field matches.
    """
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
    """Verify requested values after reconnect/config reload.

    Parameters
    ----------
    interface : MeshInterface
        Reconnected interface containing refreshed device state.
    node_dest : str
        Destination whose configuration is verified.
    verify_channel_url : str | None
        Normalized channel URL expected after reload.
    verify_config_fields : dict[str, dict[str, Any]] | None
        Requested local-config sections/fields to compare.
    verify_module_config_fields : dict[str, dict[str, Any]] | None
        Requested module-config sections/fields to compare.
    verify_channel_url_against_state : Callable[..., bool]
        Channel-state comparison seam.

    Returns
    -------
    ConfigureReconnectResult
        ``VERIFIED`` on a complete match, otherwise ``VERIFICATION_INCOMPLETE``.
    """
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


def _close_failed_settings_transaction(
    hooks: ConfigureHooks,
    target_node: Any,
    *,
    commit_attempted: bool,
) -> None:
    """Best-effort close an open settings transaction after Phase 2 failure.

    Parameters
    ----------
    hooks : ConfigureHooks
        CLI reporting hooks used to surface partial-application risk.
    target_node : Any
        Node whose settings transaction was opened.
    commit_attempted : bool
        Whether the normal commit call was already attempted.

    Notes
    -----
    Firmware exposes begin/commit but no rollback/cancel operation. When Phase 2
    fails before commit is attempted, committing is the only available way to
    close the transaction; writes already accepted may therefore be applied. If
    the normal commit itself failed, final device-side state is unknown and a
    second commit is intentionally not sent.
    """
    if commit_attempted:
        message = (
            "Settings transaction commit failed; device transaction state is unknown "
            "and configuration may be partially applied."
        )
        logger.warning(message)
        hooks.cli_print(f"WARNING: {message}")
        return

    message = (
        "Configuration failed during Phase 2; attempting to close the settings "
        "transaction. Any writes already accepted may be committed."
    )
    logger.warning(message)
    hooks.cli_print(f"WARNING: {message}")
    try:
        target_node.commitSettingsTransaction()
    except Exception:
        logger.warning(
            "Failed to close settings transaction after configure failure; "
            "device transaction may remain open.",
            exc_info=True,
        )
        hooks.cli_print(
            "WARNING: Could not close the failed settings transaction; the device "
            "may still have an open transaction."
        )


def _validate_phase1_configuration(
    hooks: ConfigureHooks,
    configuration: dict[str, Any],
) -> _Phase1ConfigureValues:
    """Validate and normalize every deterministic Phase-1 input before writes.

    Parameters
    ----------
    hooks : ConfigureHooks
        CLI reporting and exit hooks.
    configuration : dict[str, Any]
        Parsed top-level YAML mapping.

    Returns
    -------
    _Phase1ConfigureValues
        Normalized values for all present direct-write actions. Missing actions
        remain ``None``.

    Notes
    -----
    Validation is intentionally completed before any device mutation. This
    prevents a later malformed direct-write value from leaving an avoidable
    partially applied Phase-1 configuration.
    """
    owner: str | None = None
    if "owner" in configuration:
        raw_owner = configuration["owner"]
        owner = "" if raw_owner is None else str(raw_owner).strip()
        if not owner:
            hooks.cli_exit(
                "ERROR: Long Name cannot be empty or contain only whitespace characters"
            )

    owner_short: str | None = None
    owner_short_key = (
        "owner_short" if "owner_short" in configuration else "ownerShort"
    )
    if owner_short_key in configuration:
        raw_owner_short = configuration[owner_short_key]
        owner_short = (
            "" if raw_owner_short is None else str(raw_owner_short).strip()
        )
        if not owner_short:
            hooks.cli_exit(
                "ERROR: Short Name cannot be empty or contain only whitespace characters"
            )

    location_values: tuple[float, float, int] | None = None
    if "location" in configuration:
        location = configuration["location"]
        if not isinstance(location, dict) or not location:
            hooks.cli_exit(
                "location must be a non-empty mapping with lat, lon, and optional alt"
            )
        unknown_location_keys = set(location) - {"lat", "lon", "alt"}
        if unknown_location_keys:
            hooks.cli_exit(
                "location contains unknown keys: "
                f"{', '.join(sorted(unknown_location_keys))}. Allowed: lat, lon, alt"
            )
        if "lat" not in location or "lon" not in location:
            hooks.cli_exit("location requires both lat and lon")
        if isinstance(location["lat"], bool):
            hooks.cli_exit(
                f"location.lat must be a number, got: {location['lat']!r}"
            )
        try:
            lat = float(location["lat"])
        except (ValueError, TypeError):
            hooks.cli_exit(
                f"location.lat must be a number, got: {location['lat']!r}"
            )
        if isinstance(location["lon"], bool):
            hooks.cli_exit(
                f"location.lon must be a number, got: {location['lon']!r}"
            )
        try:
            lon = float(location["lon"])
        except (ValueError, TypeError):
            hooks.cli_exit(
                f"location.lon must be a number, got: {location['lon']!r}"
            )
        if not -90.0 <= lat <= 90.0:
            hooks.cli_exit(f"location.lat must be between -90 and 90, got: {lat}")
        if not -180.0 <= lon <= 180.0:
            hooks.cli_exit(f"location.lon must be between -180 and 180, got: {lon}")
        alt = 0
        if "alt" in location:
            if isinstance(location["alt"], bool):
                hooks.cli_exit(
                    f"location.alt must be an integer, got: {location['alt']!r}"
                )
            try:
                alt = int(location["alt"])
            except (ValueError, TypeError):
                hooks.cli_exit(
                    f"location.alt must be an integer, got: {location['alt']!r}"
                )
            if not POSITION_ALTITUDE_MIN <= alt <= POSITION_ALTITUDE_MAX:
                hooks.cli_exit(
                    "location.alt must fit the signed 32-bit position field, "
                    f"got: {alt}"
                )
        location_values = (lat, lon, alt)

    def _optional_string(key: str) -> str | None:
        """Return an optional top-level string value after type validation.

        Parameters
        ----------
        key : str
            Top-level configuration key to inspect.

        Returns
        -------
        str | None
            String value when present, otherwise ``None``.
        """
        if key not in configuration:
            return None
        value = configuration[key]
        if not isinstance(value, str):
            hooks.cli_exit(f"ERROR: {key} must be a string.")
        return value

    channel_url_key = (
        "channel_url" if "channel_url" in configuration else "channelUrl"
    )
    channel_url: str | None = None
    if channel_url_key in configuration:
        raw_channel_url = configuration[channel_url_key]
        if not isinstance(raw_channel_url, str):
            hooks.cli_exit(f"ERROR: {channel_url_key} must be a string.")
        channel_url = raw_channel_url.strip()
        if not channel_url:
            hooks.cli_exit(f"ERROR: {channel_url_key} must not be blank.")

    return _Phase1ConfigureValues(
        owner=owner,
        owner_short=owner_short,
        location=location_values,
        canned_messages=_optional_string("canned_messages"),
        ringtone=_optional_string("ringtone"),
        channel_url=channel_url,
    )


def _load_and_validate_configure_document(
    hooks: ConfigureHooks,
    path: str,
) -> _PreparedConfigureDocument:
    """Load, structurally validate, and normalize one configure YAML document.

    Parameters
    ----------
    hooks : ConfigureHooks
        CLI reporting and validation hooks.
    path : str
        YAML document path.

    Returns
    -------
    _PreparedConfigureDocument
        Validated top-level mapping, normalized direct-write values, and narrowed
        config/module-config section mappings.
    """
    try:
        with open(path, encoding="utf8") as file:
            raw_text = file.read()
        configuration = yaml.safe_load(raw_text)
    except OSError as exc:
        hooks.cli_exit(f"ERROR: Failed to read configuration file: {exc}")
    except (yaml.YAMLError, UnicodeDecodeError) as exc:
        hooks.cli_exit(f"ERROR: Failed to parse YAML configuration: {exc}")

    if configuration is None:
        hooks.cli_exit("ERROR: YAML configuration file is empty")
    if not isinstance(configuration, dict):
        hooks.cli_exit(
            "ERROR: YAML configuration must be a mapping/dictionary, got "
            f"{type(configuration).__name__}"
        )
    if not configuration:
        hooks.cli_exit("ERROR: Configuration file is empty; nothing to configure.")

    unknown_keys = set(configuration) - ALLOWED_CONFIGURE_KEYS
    if unknown_keys:
        hooks.cli_exit(
            f"ERROR: Unknown top-level key(s) in YAML: {', '.join(sorted(unknown_keys))}"
        )
    if "channel_url" in configuration and "channelUrl" in configuration:
        hooks.cli_exit(
            "ERROR: Cannot specify both 'channel_url' and 'channelUrl' in the same "
            "configuration file; use one."
        )
    if "owner_short" in configuration and "ownerShort" in configuration:
        hooks.cli_exit(
            "ERROR: Cannot specify both 'owner_short' and 'ownerShort' in the same "
            "configuration file; use one."
        )

    phase1_values = _validate_phase1_configuration(hooks, configuration)

    config_sections: dict[str, dict[str, Any]] = {}
    if "config" in configuration:
        config_value = configuration["config"]
        if not isinstance(config_value, dict) or not config_value:
            hooks.cli_exit(
                "ERROR: 'config' must be a non-empty mapping, got "
                f"{type(config_value).__name__}"
                f"{' (empty)' if isinstance(config_value, dict) else ''}"
            )
        config_sections = _validate_mapping_sections(
            hooks, top_level_key="config", section_mapping=config_value
        )

    module_config_sections: dict[str, dict[str, Any]] = {}
    if "module_config" in configuration:
        module_config_value = configuration["module_config"]
        if not isinstance(module_config_value, dict) or not module_config_value:
            hooks.cli_exit(
                "ERROR: 'module_config' must be a non-empty mapping, got "
                f"{type(module_config_value).__name__}"
                f"{' (empty)' if isinstance(module_config_value, dict) else ''}"
            )
        module_config_sections = _validate_mapping_sections(
            hooks,
            top_level_key="module_config",
            section_mapping=module_config_value,
        )

    return _PreparedConfigureDocument(
        configuration=configuration,
        phase1=phase1_values,
        config_sections=config_sections,
        module_config_sections=module_config_sections,
    )


def _apply_phase1_configuration(
    hooks: ConfigureHooks,
    target_node: Any,
    prepared: _PreparedConfigureDocument,
) -> bool:
    """Apply validated direct-write values in historical Phase-1 order.

    Parameters
    ----------
    hooks : ConfigureHooks
        CLI reporting and channel-URL hooks.
    target_node : Any
        Node receiving the direct writes.
    prepared : _PreparedConfigureDocument
        Fully validated configure document.

    Returns
    -------
    bool
        ``True`` only when a channel URL write was actually sent.
    """
    configuration = prepared.configuration
    values = prepared.phase1
    phase1_started = False

    def _begin_phase1() -> None:
        """Print the direct-write phase header exactly once."""
        nonlocal phase1_started
        if not phase1_started:
            hooks.cli_print(CONFIGURE_PHASE1_HEADER)
            phase1_started = True

    if values.owner is not None:
        _begin_phase1()
        hooks.cli_print(f"Setting device owner to {values.owner}")
        target_node.setOwner(long_name=values.owner)
        time.sleep(CONFIG_APPLY_DELAY_SECONDS)

    if values.owner_short is not None:
        _begin_phase1()
        hooks.cli_print(f"Setting device owner short to {values.owner_short}")
        target_node.setOwner(long_name=None, short_name=values.owner_short)
        time.sleep(CONFIG_APPLY_DELAY_SECONDS)

    if values.location is not None:
        _begin_phase1()
        lat, lon, alt = values.location
        if "alt" in configuration["location"]:
            hooks.cli_print(f"Fixing altitude at {alt} meters")
        hooks.cli_print(f"Fixing latitude at {lat} degrees")
        hooks.cli_print(f"Fixing longitude at {lon} degrees")
        hooks.cli_print("Setting device position")
        target_node.setFixedPosition(lat, lon, alt)
        time.sleep(CONFIG_APPLY_DELAY_SECONDS)

    if values.canned_messages is not None:
        _begin_phase1()
        hooks.cli_print(f"Setting canned message messages to {values.canned_messages}")
        target_node.set_canned_message(values.canned_messages)
        time.sleep(CONFIG_APPLY_DELAY_SECONDS)

    if values.ringtone is not None:
        _begin_phase1()
        hooks.cli_print(f"Setting ringtone to {values.ringtone}")
        target_node.set_ringtone(values.ringtone)
        time.sleep(CONFIG_APPLY_DELAY_SECONDS)

    seturl_executed = False
    if values.channel_url is not None:
        _begin_phase1()
        seturl_executed = _apply_configure_channel_url(
            hooks,
            target_node,
            values.channel_url,
            config_key=(
                "channel_url" if "channel_url" in configuration else "channelUrl"
            ),
        )

    if phase1_started:
        hooks.cli_print("Phase 1 complete.")
    return seturl_executed


def _apply_settings_transaction(
    hooks: ConfigureHooks,
    target_node: Any,
    *,
    config_sections: dict[str, dict[str, Any]],
    module_config_sections: dict[str, dict[str, Any]],
) -> None:
    """Apply validated config sections inside one firmware settings transaction.

    Parameters
    ----------
    hooks : ConfigureHooks
        Traversal, pacing, and reporting hooks.
    target_node : Any
        Node receiving the configuration writes.
    config_sections : dict[str, dict[str, Any]]
        Validated LocalConfig sections.
    module_config_sections : dict[str, dict[str, Any]]
        Validated LocalModuleConfig sections.
    """
    hooks.cli_print(
        "Phase 2: Applying configuration transaction (may trigger device reboot)..."
    )
    target_node.beginSettingsTransaction()
    remaining_writes = len(config_sections) + len(module_config_sections)
    commit_attempted = False

    def _apply_sections(
        sections: dict[str, dict[str, Any]],
        protobuf_root: Any,
        label: str,
    ) -> None:
        """Apply one validated section group and pace each device write.

        Parameters
        ----------
        sections : dict[str, dict[str, Any]]
            Validated configuration sections to traverse and write.
        protobuf_root : Any
            Protobuf configuration root mutated by ``traverse_config``.
        label : str
            Human-readable section group used in diagnostics.
        """
        nonlocal remaining_writes
        for section, section_values in sections.items():
            failed_fields: list[str] = []
            applied = hooks.traverse_config(
                section,
                section_values,
                protobuf_root,
                failed_fields=failed_fields,
            )
            if failed_fields:
                logger.warning(
                    "Skipped %d unknown field(s) in %s section %s: %s",
                    len(failed_fields),
                    label,
                    section,
                    ", ".join(repr(field) for field in failed_fields),
                )
            if not applied:
                hooks.cli_exit(
                    f"Failed to apply {label} section {section!r} due to structural errors."
                )
            target_node.writeConfig(meshtastic.util.camel_to_snake(section))
            remaining_writes -= 1
            hooks.pace_configure_write(remaining_writes)

    try:
        _apply_sections(config_sections, target_node.localConfig, "config")
        _apply_sections(
            module_config_sections,
            target_node.moduleConfig,
            "module_config",
        )
        commit_attempted = True
        target_node.commitSettingsTransaction()
    except BaseException:
        _close_failed_settings_transaction(
            hooks,
            target_node,
            commit_attempted=commit_attempted,
        )
        raise

    time.sleep(CONFIG_COMMIT_SETTLE_SECONDS)
    hooks.cli_print(
        "Configuration transaction committed. Device may reboot to apply changes."
    )


def _report_configure_result(
    hooks: ConfigureHooks,
    interface: MeshInterface,
    *,
    destination: str,
    is_local_target: bool,
    settings_transaction_started: bool,
    seturl_executed: bool,
    channel_url: str | None,
    config_sections: dict[str, dict[str, Any]],
    module_config_sections: dict[str, dict[str, Any]],
) -> None:
    """Report post-apply reconnect/verification status for one configure run.

    Parameters
    ----------
    hooks : ConfigureHooks
        Reconnect-verification and output hooks.
    interface : MeshInterface
        Connected interface whose post-apply state is observed.
    destination : str
        Configured node destination.
    is_local_target : bool
        Whether *destination* resolves to the directly connected node.
    settings_transaction_started : bool
        Whether Phase 2 ran and therefore may have triggered a reboot.
    seturl_executed : bool
        Whether Phase 1 actually wrote a channel URL.
    channel_url : str | None
        Normalized requested channel URL for verification.
    config_sections : dict[str, dict[str, Any]]
        LocalConfig fields requested by the document.
    module_config_sections : dict[str, dict[str, Any]]
        LocalModuleConfig fields requested by the document.
    """
    if settings_transaction_started:
        if is_local_target:
            reconnect_result = hooks.post_configure_reconnect_and_verify(
                interface,
                timeout=CONFIG_RECONNECT_WAIT_SECONDS,
                node_dest=destination,
                verify_channel_url=channel_url,
                verify_config_fields=config_sections or None,
                verify_module_config_fields=module_config_sections or None,
            )
            messages = {
                ConfigureReconnectResult.VERIFIED: (
                    "Phase 3: Device reconnected and config reloaded. All settings verified."
                ),
                ConfigureReconnectResult.VERIFICATION_INCOMPLETE: (
                    "Phase 3: Device reconnected and config reloaded. Could not fully "
                    "verify applied settings."
                ),
                ConfigureReconnectResult.CONFIG_RELOAD_FAILED: (
                    "Phase 3: Device reconnected but config reload failed. Settings may "
                    "still be applying."
                ),
                ConfigureReconnectResult.RECONNECT_FAILED: (
                    "Phase 3: Device did not reconnect within timeout. Configuration may "
                    "still be applying."
                ),
            }
            hooks.cli_print(messages[reconnect_result])
        else:
            hooks.cli_print(
                "Phase 3: Reboot/reconnect verification skipped for remote target. "
                "Local transport state does not confirm remote node reload status."
            )
        return

    if seturl_executed:
        hooks.cli_print(
            "Configuration applied. Channel URL updates may still trigger reconnect/reboot."
        )
    else:
        hooks.cli_print("Configuration applied (no reboot expected).")


def _handle_configure_command(
    hooks: ConfigureHooks,
    interface: MeshInterface,
    args: Any,
    get_node_kwargs: dict[str, Any],
) -> _ConfigureCommandResult:
    """Load and apply one YAML configuration document.

    Parameters
    ----------
    hooks : ConfigureHooks
        Entrypoint-owned compatibility and reporting seams.
    interface : MeshInterface
        Connected interface used to resolve the target node.
    args : Any
        Parsed CLI arguments containing ``configure`` and destination values.
    get_node_kwargs : dict[str, Any]
        Historical keyword arguments forwarded to ``MeshInterface.getNode``.

    Returns
    -------
    _ConfigureCommandResult
        Named lifecycle flags describing whether Phase 2 opened a transaction
        and whether a local Phase-1 channel URL write was actually performed.
    """
    prepared = _load_and_validate_configure_document(hooks, args.configure[0])
    target_node = interface.getNode(args.dest, False, **get_node_kwargs)
    has_config_writes = bool(prepared.config_sections or prepared.module_config_sections)
    is_local_target = hooks.is_local_destination(interface, args.dest)

    if has_config_writes:
        _preflight_configure_sections(
            hooks,
            target_node,
            config_sections=prepared.config_sections,
            module_config_sections=prepared.module_config_sections,
        )
        if prepared.phase1.channel_url is not None and not is_local_target:
            hooks.cli_exit(
                "ERROR: Combining channel_url with additional configuration writes "
                "is not supported for remote nodes. Apply channel_url and "
                "configuration in separate operations."
            )

    seturl_executed = _apply_phase1_configuration(hooks, target_node, prepared)
    if seturl_executed and has_config_writes and is_local_target:
        if not hooks.post_seturl_stability_check(
            interface, timeout=SETURL_STABILITY_TIMEOUT_SECONDS
        ):
            hooks.cli_exit(
                "ERROR: channel_url applied, but transport did not stabilize for "
                "additional configuration writes; aborting before Phase 2."
            )

    settings_transaction_started = has_config_writes
    if has_config_writes:
        _apply_settings_transaction(
            hooks,
            target_node,
            config_sections=prepared.config_sections,
            module_config_sections=prepared.module_config_sections,
        )

    _report_configure_result(
        hooks,
        interface,
        destination=args.dest,
        is_local_target=is_local_target,
        settings_transaction_started=settings_transaction_started,
        seturl_executed=seturl_executed,
        channel_url=prepared.phase1.channel_url,
        config_sections=prepared.config_sections,
        module_config_sections=prepared.module_config_sections,
    )
    return _ConfigureCommandResult(
        settings_transaction_started=settings_transaction_started,
        phase1_channel_url_applied=seturl_executed and is_local_target,
    )

def _handle_configure_actions(
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

    outcome.close_now = True
    if not hooks.is_local_destination(context.interface, args.dest):
        hooks.cli_print("Exporting configuration of remote nodes is not supported.")
        outcome.stop_processing = True
        return

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
