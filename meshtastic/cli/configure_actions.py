"""Configure/set execution runtime for the connected Meshtastic CLI.

Owns the configure transaction lifecycle, SetURL stability handling, reconnect
verification, and configure-file dispatch; preference parsing is injected explicitly.
"""

from __future__ import annotations

import contextvars
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, NamedTuple

import meshtastic.util
from meshtastic.cli import config_io as _config_io
from meshtastic.cli import configure_values
from meshtastic.cli.context import CliContext, CliExit, _terminate_cli

# COMPAT_STABLE_SHIM: verification helpers moved to meshtastic.configure_verify.
# Historical module patch seams in tests and the meshtastic.__main__ compat
# wrappers address them through this module, so rebind them explicitly.
# pylint: disable=unused-import,useless-import-alias
from meshtastic.configure_verify import (  # noqa: F401 - compatibility re-export
    ConfigureReconnectResult,
)
from meshtastic.configure_verify import (
    _channel_url_matches_current_device_state as _channel_url_matches_current_device_state,
)
from meshtastic.configure_verify import _device_lora_config as _device_lora_config
from meshtastic.configure_verify import _flatten_leaf_paths as _flatten_leaf_paths
from meshtastic.configure_verify import (  # noqa: F401 - compatibility re-export
    _refresh_no_disconnect_verify_state,
    _verify_channel_url_against_state,
)
from meshtastic.configure_verify import (
    _verify_config_sections as _verify_config_sections,
)
from meshtastic.configure_verify import (  # noqa: F401 - compatibility re-export
    _verify_post_reconnect_config,
)
from meshtastic.mesh_interface import MeshInterface
from meshtastic.node import ADMIN_RESPONSE_WAIT_SECONDS
from meshtastic.protobuf import admin_pb2, mesh_pb2

logger = logging.getLogger(__name__)

CONFIG_APPLY_DELAY_SECONDS = 0.5
CONFIG_WRITE_PACE_SECONDS = 0.1
CONFIG_SETURL_DELAY_SECONDS = 2.0
CONFIG_COMMIT_SETTLE_SECONDS = 1.0
CONFIG_RECONNECT_WAIT_SECONDS = 15.0
CONFIG_REBOOT_PROBE_SECONDS = 2.0
CONFIG_POLL_INTERVAL_SECONDS = 0.2
SETURL_STABILITY_TIMEOUT_SECONDS = 30.0
SETURL_STABILITY_MAX_ATTEMPTS = 3
SETURL_STABILITY_WINDOW_SECONDS = 1.5
SETURL_RECONNECT_WAIT_SECONDS = 10.0
SETURL_STABILITY_POLL_SECONDS = 0.1
CONFIGURE_DIRECT_SETTINGS_HEADER = (
    "Applying direct configuration values "
    "(channel URL updates may trigger reconnect/reboot)..."
)
ALLOWED_CONFIGURE_KEYS = frozenset(
    {
        "owner",
        "owner_short",
        "ownerShort",
        "is_unmessagable",
        "is_licensed",
        "channel_url",
        "channelUrl",
        "canned_messages",
        "ringtone",
        "location",
        "config",
        "module_config",
    }
)


CONFIGURE_RECONNECT_MESSAGES: dict[ConfigureReconnectResult, str] = {
    ConfigureReconnectResult.VERIFIED: (
        "Post-reconnect verification: device reconnected, configuration reloaded, "
        "and all requested settings were verified."
    ),
    ConfigureReconnectResult.VERIFICATION_INCOMPLETE: (
        "Post-reconnect verification: device reconnected and configuration reloaded, "
        "but not all requested settings could be verified."
    ),
    ConfigureReconnectResult.CONFIG_RELOAD_FAILED: (
        "Post-reconnect verification: device reconnected, but configuration reload "
        "failed. Settings may still be applying."
    ),
    ConfigureReconnectResult.RECONNECT_FAILED: (
        "Post-reconnect verification: device did not reconnect within the timeout. "
        "Configuration may still be applying."
    ),
}


def _configure_reconnect_message(result: ConfigureReconnectResult) -> str:
    """Return a fail-soft status message for reconnect verification.

    Parameters
    ----------
    result : ConfigureReconnectResult
        Verification result returned by the reconnect compatibility seam.

    Returns
    -------
    str
        Stable human-readable status, including a conservative fallback for an
        unrecognized future result.
    """
    return CONFIGURE_RECONNECT_MESSAGES.get(
        result,
        "Post-reconnect verification: unrecognized verification result "
        f"{result!r}. Configuration may still be applying.",
    )


class _ConfigureCommandResult(tuple[bool, bool]):
    """Two-item compatibility result with internal request-sent metadata.

    The tuple payload intentionally remains ``(settings_transaction_started,
    local_channel_url_applied)`` so existing private compatibility callers can
    continue unpacking or comparing the historical two-item result. Dispatch also
    consumes ``request_sent`` to avoid waiting for an ACK after a true no-op.
    """

    request_sent: bool

    def __new__(
        cls,
        settings_transaction_started: bool,
        local_channel_url_applied: bool,
        *,
        request_sent: bool,
    ) -> _ConfigureCommandResult:
        """Create a compatibility tuple carrying internal lifecycle metadata."""
        result = super().__new__(
            cls, (settings_transaction_started, local_channel_url_applied)
        )
        result.request_sent = request_sent
        return result

    @property
    def settings_transaction_started(self) -> bool:
        """Return whether a firmware settings transaction was started."""
        return self[0]

    @property
    def local_channel_url_applied(self) -> bool:
        """Return whether a local channel URL write was actually sent."""
        return self[1]


class _PreparedConfigureDocument(NamedTuple):
    """Validated YAML document and normalized values ready for device access.

    Attributes
    ----------
    direct_values : configure_values._DirectConfigureValues
        Normalized direct-write values.
    config_sections : dict[str, dict[str, Any]]
        Validated LocalConfig section mappings.
    module_config_sections : dict[str, dict[str, Any]]
        Validated LocalModuleConfig section mappings.
    """

    direct_values: configure_values._DirectConfigureValues
    config_sections: dict[str, dict[str, Any]]
    module_config_sections: dict[str, dict[str, Any]]


@dataclass(frozen=True, slots=True)
class _ConfigureExecutionPlan:
    """Immutable device-independent plan for one configure document.

    Parameters
    ----------
    prepared : _PreparedConfigureDocument
        Validated document with normalized direct and protobuf-section values.
    destination : str
        Destination value from the parsed CLI invocation.
    is_local_target : bool
        Whether the destination resolves to the locally connected node.
    has_config_writes : bool
        Whether a firmware settings transaction is required.
    """

    prepared: _PreparedConfigureDocument
    destination: str
    is_local_target: bool
    has_config_writes: bool


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
    export_profile: Callable[[MeshInterface], bytes] = _config_io._export_profile


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
    The mesh interface already tracks a generation counter (``configId``) that
    the device echoes back through ``config_complete_id`` when it finishes
    shipping the post-reboot config. ``_handle_from_radio_rebooted`` calls
    ``_start_config()`` on each reboot, which bumps ``configId`` and re-issues
    ``want_config_id`` to the device.

    This helper:

    1. Snapshots the pre-operation ``configId`` (the authoritative generation
       signal) and probes for any reboot indication (disconnect or generation
       bump) for a short window.

    2. If a disconnect was observed, waits for reconnect within the remaining
       timeout budget.

    3. If ``configId`` is still the snapshot value, no reboot was observed on
       the receive side, so the helper bumps it via ``_start_config()`` to ask
       the device for a fresh full config (single, generation-aware refresh).

    4. Calls ``waitForConfig()`` exactly once. A failure here is reported as
       ``CONFIG_RELOAD_FAILED``; success moves to value verification.

    5. If verification targets were provided, runs the value-aware comparator
       once; mismatches become ``VERIFICATION_INCOMPLETE``.

    Parameters
    ----------
    interface : MeshInterface
        Connected interface observed for disconnect/reconnect.
    timeout : float
        Reboot-recovery budget in seconds, covering the fixed reboot probe
        and the post-disconnect reconnect wait. It does not bound the
        config reload: ``waitForConfig()`` applies its own internal
        wait/retry timing.
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
    start_time = time.monotonic()
    deadline = start_time + timeout
    pre_op_config_id = getattr(interface, "configId", None)

    probe_deadline = min(start_time + CONFIG_REBOOT_PROBE_SECONDS, deadline)
    logger.debug(
        "Probing for reboot indication up to %.1fs (configId or isConnected)...",
        max(probe_deadline - time.monotonic(), 0.0),
    )
    while time.monotonic() < probe_deadline:
        if not interface.isConnected.is_set():
            logger.info("Device disconnected (reboot indication received).")
            break
        if getattr(interface, "configId", None) != pre_op_config_id:
            logger.info("Device rebooted (generation counter advanced).")
            break
        time.sleep(CONFIG_POLL_INTERVAL_SECONDS)

    if not interface.isConnected.is_set():
        while time.monotonic() < deadline:
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

    if getattr(interface, "configId", None) == pre_op_config_id:
        start_config = getattr(interface, "_start_config", None)
        if callable(start_config):
            try:
                start_config()
            except Exception:
                logger.warning(
                    "Failed to bump config generation after reconnect; "
                    "configuration may still be applying.",
                    exc_info=True,
                )
                return ConfigureReconnectResult.CONFIG_RELOAD_FAILED
            logger.debug(
                "No reboot observed via generation counter; "
                "requested fresh full config via want_config_id."
            )

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
    _validate_string_mapping_keys(
        hooks,
        section_mapping,
        path=top_level_key,
    )
    validated_sections: dict[str, dict[str, Any]] = {}
    for section_name, section_value in section_mapping.items():
        if not isinstance(section_value, dict):
            _terminate_cli(
                hooks.cli_exit,
                f"ERROR: '{top_level_key}.{section_name}' must be a mapping, got "
                f"{type(section_value).__name__}",
            )
        validated_sections[section_name] = section_value
    return validated_sections


def _validate_string_mapping_keys(
    hooks: ConfigureHooks,
    mapping: dict[Any, Any],
    *,
    path: str,
) -> None:
    """Reject YAML mapping keys that cannot name configuration fields."""
    for key, value in mapping.items():
        if not isinstance(key, str):
            _terminate_cli(
                hooks.cli_exit,
                f"ERROR: {path} keys must be strings, got {key!r} "
                f"({type(key).__name__}).",
            )
        if isinstance(value, dict):
            _validate_string_mapping_keys(
                hooks,
                value,
                path=f"{path}.{key}",
            )


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
                _terminate_cli(
                    hooks.cli_exit,
                    f"Failed to apply {top_level_key} section {section!r} "
                    f"due to structural errors.{field_suffix}",
                )
    finally:
        hooks.preflight_mode.reset(token)


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
        _terminate_cli(hooks.cli_exit, f"ERROR: {config_key} must be a string.")
    requested_channel_url = raw_channel_url.strip()
    if not requested_channel_url:
        _terminate_cli(hooks.cli_exit, f"ERROR: {config_key} must not be blank.")

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
    """Best-effort close an open settings transaction after a configuration failure.

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
    Firmware exposes begin/commit but no rollback/cancel operation. When a
    configuration transaction fails before commit is attempted, committing is the
    only available way to close the transaction; writes already accepted may therefore
    be applied. If
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
        "Configuration failed before the settings transaction completed; attempting "
        "to close it. Any writes already accepted may be committed."
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


def _decode_configure_document(
    hooks: ConfigureHooks, raw_bytes: bytes | str, path: str
) -> dict[str, Any] | None:
    """Decode one configure document through the configuration I/O runtime."""
    return _config_io._decode_configure_document(  # noqa: SLF001
        raw_bytes, path, cli_exit=hooks.cli_exit
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
        with open(path, "rb") as file:
            raw_bytes = file.read()
    except OSError as exc:
        _terminate_cli(
            hooks.cli_exit, f"ERROR: Failed to read configuration file: {exc}"
        )
    configuration = _decode_configure_document(hooks, raw_bytes, path)

    if configuration is None:
        _terminate_cli(hooks.cli_exit, "ERROR: YAML configuration file is empty")
    if not isinstance(configuration, dict):
        _terminate_cli(
            hooks.cli_exit,
            "ERROR: YAML configuration must be a mapping/dictionary, got "
            f"{type(configuration).__name__}",
        )
    if not configuration:
        _terminate_cli(
            hooks.cli_exit, "ERROR: Configuration file is empty; nothing to configure."
        )

    _validate_string_mapping_keys(hooks, configuration, path="configuration")

    unknown_keys = set(configuration) - ALLOWED_CONFIGURE_KEYS
    if unknown_keys:
        _terminate_cli(
            hooks.cli_exit,
            f"ERROR: Unknown top-level key(s) in YAML: {', '.join(sorted(unknown_keys))}",
        )
    if "channel_url" in configuration and "channelUrl" in configuration:
        _terminate_cli(
            hooks.cli_exit,
            "ERROR: Cannot specify both 'channel_url' and 'channelUrl' in the same "
            "configuration file; use one.",
        )
    if "owner_short" in configuration and "ownerShort" in configuration:
        _terminate_cli(
            hooks.cli_exit,
            "ERROR: Cannot specify both 'owner_short' and 'ownerShort' in the same "
            "configuration file; use one.",
        )

    direct_values = configure_values._validate_direct_configuration(
        hooks, configuration
    )

    config_sections: dict[str, dict[str, Any]] = {}
    if "config" in configuration:
        config_value = configuration["config"]
        if not isinstance(config_value, dict) or not config_value:
            _terminate_cli(
                hooks.cli_exit,
                "ERROR: 'config' must be a non-empty mapping, got "
                f"{type(config_value).__name__}"
                f"{' (empty)' if isinstance(config_value, dict) else ''}",
            )
        config_sections = _validate_mapping_sections(
            hooks, top_level_key="config", section_mapping=config_value
        )

    module_config_sections: dict[str, dict[str, Any]] = {}
    if "module_config" in configuration:
        module_config_value = configuration["module_config"]
        if not isinstance(module_config_value, dict) or not module_config_value:
            _terminate_cli(
                hooks.cli_exit,
                "ERROR: 'module_config' must be a non-empty mapping, got "
                f"{type(module_config_value).__name__}"
                f"{' (empty)' if isinstance(module_config_value, dict) else ''}",
            )
        module_config_sections = _validate_mapping_sections(
            hooks,
            top_level_key="module_config",
            section_mapping=module_config_value,
        )

    return _PreparedConfigureDocument(
        direct_values=direct_values,
        config_sections=config_sections,
        module_config_sections=module_config_sections,
    )


def _local_owner_via_admin(target_node: Any) -> mesh_pb2.User | None:
    """Read the local node's owner record from the device's own admin getter.

    A nodeless connection (``--no-nodes``) can leave the client node database
    without the local entry that ``getMyUser()`` reads, so fall back to the
    admin ``get_owner`` getter — the same channel the owner write itself uses.
    The device's answer is authoritative and does not depend on ``nodesByNum``.

    Parameters
    ----------
    target_node : Any
        Local node whose owner record is read.

    Returns
    -------
    mesh_pb2.User | None
        The current owner record, or ``None`` when the node does not support
        the admin owner-getter seam, does not answer in time, or the
        transport rejects the request.
    """
    request_admin_response = getattr(target_node, "_request_admin_response", None)
    if not callable(request_admin_response):
        return None
    message = admin_pb2.AdminMessage()
    message.get_owner_request = True
    try:
        return request_admin_response(
            message,
            "get_owner_response",
            mesh_pb2.User,
            response_timeout_seconds=ADMIN_RESPONSE_WAIT_SECONDS,
        )
    except Exception:  # pylint: disable=broad-except
        logger.debug("Local owner state request failed.", exc_info=True)
        return None


class _CurrentOwnerState(NamedTuple):
    """Current owner state resolved from the client cache or device."""

    user: dict[str, Any] | None
    owner: mesh_pb2.User | None


def _current_owner_state(
    hooks: ConfigureHooks, target_node: Any, *, purpose: str
) -> _CurrentOwnerState:
    """Resolve owner state from the client snapshot, then the device.

    Parameters
    ----------
    hooks : ConfigureHooks
        CLI reporting and termination seams.
    target_node : Any
        Target node whose owner state is read.
    purpose : str
        Description of the owner field being preserved for error messages.

    Returns
    -------
    _CurrentOwnerState
        Client-side user mapping or the local device's owner protobuf.

    Raises
    ------
    SystemExit
        Via the CLI exit seam when target identity/state cannot be resolved.
    """
    node_num = getattr(target_node, "nodeNum", None)
    interface = getattr(target_node, "iface", None)
    node_db_lock = getattr(interface, "_node_db_lock", None)
    my_node_num = getattr(getattr(interface, "myInfo", None), "my_node_num", None)
    if (
        not isinstance(node_num, int)
        or isinstance(node_num, bool)
        or node_db_lock is None
    ):
        _terminate_cli(
            hooks.cli_exit,
            f"Unable to preserve the current {purpose}: target owner state is unavailable.",
        )

    user: dict[str, Any] | None = None
    is_local_target = my_node_num is not None and node_num == my_node_num
    with node_db_lock:
        if is_local_target:
            get_my_user = getattr(interface, "getMyUser", None)
            stored_user = get_my_user() if callable(get_my_user) else None
            user = dict(stored_user) if isinstance(stored_user, dict) else None

    if user is None and is_local_target:
        # A nodeless connection (--no-nodes) can leave the client node
        # database empty. Query the device outside the node DB lock because
        # the admin getter blocks on a bounded response wait.
        owner = _local_owner_via_admin(target_node)
        if owner is not None:
            return _CurrentOwnerState(None, owner)

    if user is None:
        with node_db_lock:
            nodes_by_num = getattr(interface, "nodesByNum", None)
            node_data = (
                nodes_by_num.get(node_num) if isinstance(nodes_by_num, dict) else None
            )
            stored_user = node_data.get("user") if isinstance(node_data, dict) else None
            user = dict(stored_user) if isinstance(stored_user, dict) else None

    if user is None:
        _terminate_cli(
            hooks.cli_exit,
            f"Unable to preserve the current {purpose}: target owner state is unavailable.",
        )
    return _CurrentOwnerState(user, None)


def _current_owner_is_licensed(hooks: ConfigureHooks, target_node: Any) -> bool:
    """Return the target's current licensed flag for partial owner-profile writes.

    Parameters
    ----------
    hooks : ConfigureHooks
        CLI reporting and termination seams.
    target_node : Any
        Target node whose owner state is read.

    Returns
    -------
    bool
        Current ``isLicensed`` value. Protobuf JSON omits scalar ``False``
        values, so a missing ``isLicensed`` key means ``False``.

    Raises
    ------
    SystemExit
        Via the CLI exit seam when owner state is unavailable or invalid.
    """
    state = _current_owner_state(hooks, target_node, purpose="licensed flag")
    if state.owner is not None:
        return bool(state.owner.is_licensed)
    assert state.user is not None
    current = state.user.get("isLicensed", False)
    if not isinstance(current, bool):
        _terminate_cli(
            hooks.cli_exit,
            "Unable to preserve the current licensed flag: target owner state is invalid.",
        )
    return current


def _current_owner_long_name(hooks: ConfigureHooks, target_node: Any) -> str:
    """Return the target's current long owner name for partial owner writes.

    Parameters
    ----------
    hooks : ConfigureHooks
        CLI reporting and termination seams.
    target_node : Any
        Target node whose owner state is read.

    Returns
    -------
    str
        The current long owner name with surrounding whitespace stripped.

    Raises
    ------
    SystemExit
        Via the CLI exit seam when the current long name is unavailable or blank.
    """
    state = _current_owner_state(hooks, target_node, purpose="owner name")
    if state.owner is not None:
        long_name: object = state.owner.long_name
    else:
        assert state.user is not None
        long_name = state.user.get("longName")
    if not isinstance(long_name, str) or not long_name.strip():
        _terminate_cli(
            hooks.cli_exit,
            "Unable to preserve the current owner name: target owner state is unavailable.",
        )
    return long_name.strip()


def _apply_direct_configuration(
    hooks: ConfigureHooks,
    target_node: Any,
    prepared: _PreparedConfigureDocument,
) -> bool:
    """Apply validated non-transactional values in compatibility-preserving order.

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
    values = prepared.direct_values
    direct_writes_started = False

    def _begin_direct_writes() -> None:
        """Print the direct-configuration header exactly once."""
        nonlocal direct_writes_started
        if not direct_writes_started:
            hooks.cli_print(CONFIGURE_DIRECT_SETTINGS_HEADER)
            direct_writes_started = True

    owner_flags_requested = (
        values.is_licensed is not None or values.is_unmessagable is not None
    )
    if owner_flags_requested:
        _begin_direct_writes()
        if values.owner is not None:
            hooks.cli_print(f"Setting device owner to {values.owner}")
        if values.owner_short is not None:
            hooks.cli_print(f"Setting device owner short to {values.owner_short}")
        if values.is_licensed is not None:
            hooks.cli_print(f"Setting licensed mode to {values.is_licensed}")
        if values.is_unmessagable is not None:
            hooks.cli_print(
                f"Setting owner unmessagable flag to {values.is_unmessagable}"
            )
        is_licensed = (
            values.is_licensed
            if values.is_licensed is not None
            else _current_owner_is_licensed(hooks, target_node)
        )
        # ``Node.setOwner`` applies the owner-profile flags only alongside a
        # long name, so a flag-only write must restate the current name; a
        # flag-only document whose owner name cannot be read is refused
        # before the write instead of silently not applying the flags.
        long_name = (
            values.owner
            if values.owner is not None
            else _current_owner_long_name(hooks, target_node)
        )
        target_node.setOwner(
            long_name=long_name,
            short_name=values.owner_short,
            is_licensed=is_licensed,
            is_unmessagable=values.is_unmessagable,
        )
        time.sleep(CONFIG_APPLY_DELAY_SECONDS)
    else:
        # Preserve the historical configure write shape for the long/short-name-only
        # path. Besides compatibility with observable call ordering, this avoids
        # changing firmware pacing for existing configure documents.
        if values.owner is not None:
            _begin_direct_writes()
            hooks.cli_print(f"Setting device owner to {values.owner}")
            target_node.setOwner(long_name=values.owner)
            time.sleep(CONFIG_APPLY_DELAY_SECONDS)
        if values.owner_short is not None:
            _begin_direct_writes()
            hooks.cli_print(f"Setting device owner short to {values.owner_short}")
            target_node.setOwner(long_name=None, short_name=values.owner_short)
            time.sleep(CONFIG_APPLY_DELAY_SECONDS)

    if values.location is not None:
        _begin_direct_writes()
        lat, lon, alt = values.location
        if values.altitude_specified:
            hooks.cli_print(f"Fixing altitude at {alt} meters")
        hooks.cli_print(f"Fixing latitude at {lat} degrees")
        hooks.cli_print(f"Fixing longitude at {lon} degrees")
        hooks.cli_print("Setting device position")
        target_node.setFixedPosition(lat, lon, alt)
        time.sleep(CONFIG_APPLY_DELAY_SECONDS)

    if values.canned_messages is not None:
        _begin_direct_writes()
        hooks.cli_print(f"Setting canned message messages to {values.canned_messages}")
        target_node.set_canned_message(values.canned_messages)
        time.sleep(CONFIG_APPLY_DELAY_SECONDS)

    if values.ringtone is not None:
        _begin_direct_writes()
        hooks.cli_print(f"Setting ringtone to {values.ringtone}")
        target_node.set_ringtone(values.ringtone)
        time.sleep(CONFIG_APPLY_DELAY_SECONDS)

    seturl_executed = False
    if values.channel_url is not None:
        _begin_direct_writes()
        if values.channel_url_key is None:
            raise AssertionError("normalized channel URL is missing its source key")
        seturl_executed = _apply_configure_channel_url(
            hooks,
            target_node,
            values.channel_url,
            config_key=values.channel_url_key,
        )

    if direct_writes_started:
        hooks.cli_print("Direct configuration values applied.")
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
    hooks.cli_print("Applying configuration transaction (may trigger device reboot)...")
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
                _terminate_cli(
                    hooks.cli_exit,
                    f"Failed to apply {label} section {section!r} due to structural errors.",
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
        Whether the settings transaction ran and therefore may have triggered a reboot.
    seturl_executed : bool
        Whether direct writes actually wrote a channel URL.
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
            hooks.cli_print(_configure_reconnect_message(reconnect_result))
        else:
            hooks.cli_print(
                "Post-reconnect verification skipped for remote target. Local transport "
                "state does not confirm remote node reload status."
            )
        return

    if seturl_executed:
        hooks.cli_print(
            "Configuration applied. Channel URL updates may still trigger reconnect/reboot."
        )
    else:
        hooks.cli_print("Configuration applied (no reboot expected).")


def _prepare_configure_execution(
    hooks: ConfigureHooks,
    interface: MeshInterface,
    args: Any,
) -> _ConfigureExecutionPlan:
    """Build and validate a configure plan before resolving the target node.

    Parameters
    ----------
    hooks : ConfigureHooks
        Entrypoint-owned compatibility and reporting seams.
    interface : MeshInterface
        Connected interface used only for local-destination classification.
    args : Any
        Parsed CLI arguments containing ``configure`` and destination values.

    Returns
    -------
    _ConfigureExecutionPlan
        Immutable normalized plan that is safe to execute against a target node.
    """
    if len(args.configure) != 1:
        _terminate_cli(
            hooks.cli_exit,
            "ERROR: --configure may be specified only once per invocation.",
        )

    prepared = _load_and_validate_configure_document(hooks, args.configure[0])
    has_config_writes = bool(
        prepared.config_sections or prepared.module_config_sections
    )
    is_local_target = hooks.is_local_destination(interface, args.dest)
    if (
        prepared.direct_values.channel_url is not None
        and has_config_writes
        and not is_local_target
    ):
        _terminate_cli(
            hooks.cli_exit,
            "ERROR: Combining channel_url with additional configuration writes "
            "is not supported for remote nodes. Apply channel_url and "
            "configuration in separate operations.",
        )

    return _ConfigureExecutionPlan(
        prepared=prepared,
        destination=args.dest,
        is_local_target=is_local_target,
        has_config_writes=has_config_writes,
    )


def _execute_configure_plan(
    hooks: ConfigureHooks,
    interface: MeshInterface,
    target_node: Any,
    plan: _ConfigureExecutionPlan,
) -> _ConfigureCommandResult:
    """Execute one validated configure plan against the resolved target node.

    Parameters
    ----------
    hooks : ConfigureHooks
        Entrypoint-owned compatibility and reporting seams.
    interface : MeshInterface
        Connected interface used by stability and reconnect verification.
    target_node : Any
        Node selected for direct writes and settings transactions.
    plan : _ConfigureExecutionPlan
        Immutable plan prepared before device access.

    Returns
    -------
    _ConfigureCommandResult
        Named lifecycle flags describing requests sent by the execution.
    """
    prepared = plan.prepared
    if plan.has_config_writes:
        _preflight_configure_sections(
            hooks,
            target_node,
            config_sections=prepared.config_sections,
            module_config_sections=prepared.module_config_sections,
        )

    seturl_executed = _apply_direct_configuration(hooks, target_node, prepared)
    if seturl_executed and plan.has_config_writes and plan.is_local_target:
        if not hooks.post_seturl_stability_check(
            interface, timeout=SETURL_STABILITY_TIMEOUT_SECONDS
        ):
            _terminate_cli(
                hooks.cli_exit,
                "ERROR: channel_url applied, but transport did not stabilize for "
                "additional configuration writes; aborting before the configuration "
                "transaction.",
            )

    if plan.has_config_writes:
        _apply_settings_transaction(
            hooks,
            target_node,
            config_sections=prepared.config_sections,
            module_config_sections=prepared.module_config_sections,
        )

    _report_configure_result(
        hooks,
        interface,
        destination=plan.destination,
        is_local_target=plan.is_local_target,
        settings_transaction_started=plan.has_config_writes,
        seturl_executed=seturl_executed,
        channel_url=prepared.direct_values.channel_url,
        config_sections=prepared.config_sections,
        module_config_sections=prepared.module_config_sections,
    )
    return _ConfigureCommandResult(
        settings_transaction_started=plan.has_config_writes,
        local_channel_url_applied=seturl_executed and plan.is_local_target,
        request_sent=(
            prepared.direct_values.has_non_url_writes
            or seturl_executed
            or plan.has_config_writes
        ),
    )


def _handle_configure_command(
    hooks: ConfigureHooks,
    interface: MeshInterface,
    args: Any,
    get_node_kwargs: dict[str, Any],
) -> _ConfigureCommandResult:
    """Prepare and execute one YAML configuration document.

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
        Named lifecycle flags describing whether a settings transaction ran and whether
        a local channel URL write was actually performed.
    """
    plan = _prepare_configure_execution(hooks, interface, args)
    target_node = interface.getNode(plan.destination, False, **get_node_kwargs)
    return _execute_configure_plan(hooks, interface, target_node, plan)


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
        prior_wait_for_ack_nak = outcome.wait_for_ack_nak
        outcome.close_now = True
        outcome.wait_for_ack_nak = True
        configure_result = hooks.handle_configure_command(
            context.interface,
            args,
            context.get_node_kwargs,
        )
        settings_transaction_started, local_channel_url_applied = configure_result
        if settings_transaction_started or local_channel_url_applied:
            outcome.wait_for_ack_nak = False
            outcome.skip_ack_wait = True
        elif isinstance(configure_result, _ConfigureCommandResult):
            outcome.wait_for_ack_nak = (
                prior_wait_for_ack_nak or configure_result.request_sent
            )

    if not args.export_config:
        return

    outcome.close_now = True
    if not hooks.is_local_destination(context.interface, args.dest):
        hooks.cli_print("Exporting configuration of remote nodes is not supported.")
        outcome.stop_processing = True
        return

    export_format = _config_io._resolve_export_format(  # noqa: SLF001
        getattr(args, "export_format", "auto"), args.export_config
    )
    if export_format == "binary":
        _config_io._write_binary_profile(  # noqa: SLF001
            args.export_config,
            lambda: hooks.export_profile(context.interface),
            hooks.cli_exit,
            hooks.cli_print,
        )
        return

    config_text = hooks.export_config(context.interface)
    if args.export_config == "-":
        print(config_text)
        return

    _config_io._write_export_file(  # noqa: SLF001
        args.export_config, config_text, hooks.cli_exit
    )
    hooks.cli_print(f"Exported configuration to {args.export_config}")
