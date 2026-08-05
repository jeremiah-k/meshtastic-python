"""Main Meshtastic."""

# We just hit the 1600 line limit for main.py, but I currently have a huge set of powermon/structured logging changes
# later we can have a separate changelist to refactor main.py into smaller files
# pylint: disable=R0917,C0302

import argparse
import binascii
import contextlib
import contextvars
import enum
import getpass
import importlib
import logging
import os
import platform
import sys
import textwrap
import time
from collections.abc import Callable, Iterator, Sequence
from types import ModuleType
from typing import Any, NoReturn, Protocol, cast

import yaml
from google.protobuf.descriptor import FieldDescriptor
from google.protobuf.json_format import MessageToDict
from pubsub import pub

import meshtastic.cli.config_io as cli_config_io
import meshtastic.cli.device_actions as cli_device_actions
import meshtastic.cli.runtime as cli_runtime
from meshtastic.cli.context import ActionOutcome, CliContext
import meshtastic.ota
import meshtastic.serial_interface
import meshtastic.tcp_interface
import meshtastic.util
from meshtastic import mt_config, remote_hardware
from meshtastic._core_constants import BROADCAST_ADDR, LOCAL_ADDR  # noqa: F401

# COMPAT_STABLE_SHIM: Preserve legacy imports from meshtastic.cli.parser.
# pylint: disable=unused-import
from meshtastic.cli.parser import (  # noqa: F401,W0611 - legacy __main__ compatibility export
    _MODEM_PRESET_SHORTHANDS,
    addChannelConfigArgs,
    addConfigArgs,
    addConnectionArgs,
    addImportExportArgs,
    addLocalActionArgs,
    addPositionConfigArgs,
    addRemoteActionArgs,
    addRemoteAdminArgs,
    addSelectionArgs,
    parse_cli_args,
)
from meshtastic.cli.values import is_local_destination as _is_local_destination
from meshtastic.cli.values import (  # noqa: F401,W0611 - legacy __main__ compatibility export
    looks_like_integer_literal as _looks_like_integer_literal,
)
from meshtastic.cli.values import parse_bitfield_value as _parse_bitfield_value
from meshtastic.cli.values import (  # noqa: F401,W0611 - legacy __main__ compatibility export
    parse_integer_literal as _parse_integer_literal,
)
from meshtastic.cli.values import (  # noqa: F401,W0611 - legacy __main__ compatibility export
    parse_modem_preset_name as _parse_modem_preset_name,
)
from meshtastic.configure_verify import (
    _verify_channel_url_against_state,
    _verify_requested_fields,
)
from meshtastic.host_port import parseHostAndPort
from meshtastic.interfaces.ble import BLEInterface
from meshtastic.lockdown import (
    build_lockdown_auth,
    read_lockdown_passphrase_file,
    send_lockdown_auth,
    validate_lockdown_passphrase,
)
from meshtastic.mesh_interface import MeshInterface
from meshtastic.mesh_interface_runtime import node_data
from meshtastic.protobuf import (
    admin_pb2,  # noqa: F401 - legacy __main__ compatibility export
    channel_pb2,
    config_pb2,
    localonly_pb2,
    mesh_pb2,
    portnums_pb2,
)
from meshtastic.version import (
    INSTALL_UPGRADE_HINT,
    PROJECT_DISPLAY_NAME,
    get_active_version,
)

# COMPAT_STABLE_SHIM: Preserve legacy private imports while runtime owns behavior.
MAIN_LOOP_IDLE_SLEEP_SECONDS = cli_runtime.MAIN_LOOP_IDLE_SLEEP_SECONDS
SERIAL_RECONNECT_RETRY_SECONDS = cli_runtime.SERIAL_RECONNECT_RETRY_SECONDS
SERIAL_LISTEN_CONNECTED_SLEEP_SECONDS = (
    cli_runtime.SERIAL_LISTEN_CONNECTED_SLEEP_SECONDS
)
SERIAL_RX_THREAD_JOIN_TIMEOUT_SECONDS = (
    cli_runtime.SERIAL_RX_THREAD_JOIN_TIMEOUT_SECONDS
)
_is_serial_reconnect_client = cli_runtime._is_serial_reconnect_client
_serial_transport_is_live = cli_runtime._serial_transport_is_live
_serial_should_reconnect = cli_runtime._serial_should_reconnect
_poll_serial_reconnect = cli_runtime._poll_serial_reconnect
_listen_loop_poll_once = cli_runtime._listen_loop_poll_once

argcomplete: ModuleType | None = None
try:
    import argcomplete as _argcomplete

    argcomplete = _argcomplete
except ImportError:
    pass

pyqrcode: ModuleType | None = None
try:
    import pyqrcode as _pyqrcode  # type: ignore[import-untyped]

    pyqrcode = _pyqrcode
except ImportError:
    pass

meshtastic_test: ModuleType | None = None
try:
    meshtastic_test = importlib.import_module("meshtastic.test")
except ImportError:
    pass

PowerMeter: Any | None = None
PowerStress: Any | None = None
PPK2PowerSupply: Any | None = None
RidenPowerSupply: Any | None = None
SimPowerSupply: Any | None = None
LogSet: Any | None = None

try:
    powermon_module = importlib.import_module("meshtastic.powermon")
    slog_module = importlib.import_module("meshtastic.slog")
    PowerMeter = powermon_module.PowerMeter
    PowerStress = powermon_module.PowerStress
    PPK2PowerSupply = powermon_module.PPK2PowerSupply
    RidenPowerSupply = powermon_module.RidenPowerSupply
    SimPowerSupply = powermon_module.SimPowerSupply
    LogSet = slog_module.LogSet
    powermon_constants = importlib.import_module("meshtastic.powermon.constants")
    MIN_SUPPLY_VOLTAGE_V = powermon_constants.MIN_SUPPLY_VOLTAGE_V
    MAX_SUPPLY_VOLTAGE_V = powermon_constants.MAX_SUPPLY_VOLTAGE_V

    have_powermon = True
    powermon_exception = None
    meter = None
except (ImportError, AttributeError) as exc:
    PowerMeter = None
    PowerStress = None
    PPK2PowerSupply = None
    RidenPowerSupply = None
    SimPowerSupply = None
    LogSet = None
    have_powermon = False
    powermon_exception = exc
    meter = None
    # Provide fallback constants if powermon is not available
    MIN_SUPPLY_VOLTAGE_V = 0.8
    MAX_SUPPLY_VOLTAGE_V = 5.0
    logging.getLogger(__name__).debug("powermon/slog not available: %s", exc)

logger = logging.getLogger(__name__)

_CONFIGURE_PREFLIGHT_MODE: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "configure_preflight_mode", default=False
)
_SET_PREF_VALUE_ERRORS_FATAL: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "set_pref_value_errors_fatal", default=False
)


class _PreferenceValueError(ValueError):
    """Raised internally when CLI preference assignment has an invalid scalar value."""


@contextlib.contextmanager
def _fatal_preference_value_errors() -> Iterator[None]:
    """Temporarily make scalar preference validation failures fatal to the CLI."""
    token = _SET_PREF_VALUE_ERRORS_FATAL.set(True)
    try:
        yield
    finally:
        _SET_PREF_VALUE_ERRORS_FATAL.reset(token)


_PREF_VALIDATION_REPORTER: contextvars.ContextVar[Callable[[str], None] | None] = (
    contextvars.ContextVar("pref_validation_reporter", default=None)
)

# Map dotted preference paths to the protobuf enum that defines their flags.
# These fields are stored as uint32 bitmasks in the protobuf but have an
# associated enum that names the individual flags. Add new bitfield-enum
# fields here as protobufs grow.
BITFIELD_ENUMS = {
    "network.enabled_protocols": config_pb2.Config.NetworkConfig.ProtocolFlags,
    "position.position_flags": config_pb2.Config.PositionConfig.PositionFlags,
}

# Public CLI shorthands for common modem presets. Keep this ordered to preserve
# the historical behavior when callers supply more than one shorthand: later
# options win. ``--ch-preset`` below is the scalable path for every enum value
# present in the active protobuf schema, including future additions.


# ==============================================================================
# CLI Timing Constants
# ==============================================================================

# Delay after applying configuration changes (owner, channel, etc.)
CONFIG_APPLY_DELAY_SECONDS = 0.5
CONFIG_WRITE_PACE_SECONDS = 0.1
"""Short inter-write cadence that drains transport queues without stretching transactions."""

# Delay after setURL operations, which write up to 8 channel snapshots
# plus LoRa config; the device needs extra time to commit all changes
# before accepting further admin messages.
CONFIG_SETURL_DELAY_SECONDS = 2.0

CONFIG_COMMIT_SETTLE_SECONDS = 1.0
"""Settle delay after commitSettingsTransaction before assuming the session may end."""

CONFIG_RECONNECT_WAIT_SECONDS = 15.0
"""Maximum time to wait for device reconnect after a reboot-capable configure commit."""

SETURL_STABILITY_TIMEOUT_SECONDS = 30.0
"""Timeout for post-setURL transport stability before opening Phase 2 writes."""

FACTORY_RESET_READY_PROBE_TIMEOUT_SECONDS = cli_device_actions.FACTORY_RESET_READY_PROBE_TIMEOUT_SECONDS
"""Timeout for post-reset reconnect probe inside factory-reset command."""

FACTORY_RESET_ACCEPTANCE_TIMEOUT_SECONDS = cli_device_actions.FACTORY_RESET_ACCEPTANCE_TIMEOUT_SECONDS
"""Maximum time to observe a local reset ACK/NAK or reboot disconnect."""

FACTORY_RESET_ACCEPTANCE_POLL_SECONDS = cli_device_actions.FACTORY_RESET_ACCEPTANCE_POLL_SECONDS
"""Polling interval while waiting for local reset command acceptance."""

CONFIGURE_PHASE1_HEADER = (
    "Phase 1: Applying direct configuration "
    "(channel URL updates may trigger reconnect/reboot)..."
)
"""Printed once when --configure starts applying Phase 1 settings."""

_ALLOWED_CONFIGURE_KEYS = frozenset(
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

# Delay between GPIO watch iterations
GPIO_WATCH_INTERVAL_SECONDS = 1.0

# Maximum wait time for GPIO read response (each iteration)
GPIO_READ_POLL_INTERVAL_SECONDS = 1.0
GPIO_READ_MAX_POLLS = 10

# Time to wait for device boot after power-on
POWER_ON_BOOT_DELAY_SECONDS = 5.0

# OTA CLI timing and retry delay
OTA_REBOOT_WAIT_SECONDS = cli_device_actions.OTA_REBOOT_WAIT_SECONDS
OTA_RETRY_DELAY_SECONDS = cli_device_actions.OTA_RETRY_DELAY_SECONDS
OTA_MAX_RETRIES = cli_device_actions.OTA_MAX_RETRIES

# Keep-alive sleep interval for main loop (effectively infinite wait)
"""Sleep duration for the CLI main loop when listening on non-serial interfaces."""

"""Polling interval for serial reconnect attempts after device reboot/disconnect."""

"""Sleep duration when a serial listen session is connected and healthy."""

"""Timeout for joining the old reader thread before reconnecting."""


# COMPAT_STABLE_SHIM: accept historical config field spellings.
# Backward-compatible aliases for renamed config fields.
_PREFERENCE_FIELD_ALIASES: dict[str, str] = {
    "display.use_12_hour": "display.use_12h_clock",
    "display.use12_hour": "display.use_12h_clock",
    # Exported configs can contain camelCase keys from MessageToDict(),
    # and camel_to_snake("use12hClock") yields "use12h_clock". Normalize
    # these compatibility spellings to the canonical protobuf field name.
    "display.use12h_clock": "display.use_12h_clock",
    "display.use12_h_clock": "display.use_12h_clock",
}


def _cli_exit(message: str, return_value: int = 1) -> NoReturn:
    """Exit this CLI entrypoint with a user-facing message.

    This helper centralizes CLI exit behavior in the entrypoint module while
    keeping ``meshtastic.util.our_exit`` available as a legacy compatibility
    shim for external callers.

    Parameters
    ----------
    message : str
        Message to print before exiting.
    return_value : int
        Process exit code (0 for success, non-zero for error).
    """
    meshtastic.util.our_exit(message, return_value)


def _cli_print(message: str, *, force: bool = False) -> None:
    """Print a CLI message, optionally bypassing ``--quiet`` suppression.

    Parameters
    ----------
    message : str
        User-facing message to print to stdout.
    force : bool
        When ``True``, print even if ``--quiet`` is active. Use this for
        validation output that must remain visible to the user.
    """
    args = mt_config.args
    if not force and args and getattr(args, "quiet", False):
        return
    print(message)


def _report_pref_validation(message: str) -> None:
    """Print preference validation output through the shared CLI path.

    Parameters
    ----------
    message : str
        User-facing validation diagnostic.

    Notes
    -----
    Outside preflight, validation messages retain their historical direct stdout
    behavior, including visibility under ``--quiet``. Batch preflight installs a
    reporter so diagnostics can be emitted coherently after all entries are checked.
    """
    reporter = _PREF_VALIDATION_REPORTER.get()
    if reporter is not None:
        reporter(message)
        return
    _cli_print(message, force=not _CONFIGURE_PREFLIGHT_MODE.get())


def supportInfo() -> None:
    """Print troubleshooting guidance and environment details useful for reporting CLI or library issues.

    Specifically prints the issue tracker URL and the running environment: system,
    platform string, kernel release, machine architecture, stdin/stdout encodings,
    installed meshtastic version (and available newer PyPI version if any), executable
    path, and Python implementation/version. Advises adding the output of
    `meshtastic --info` when filing an issue.
    """
    print("")
    print(f"If having issues with {PROJECT_DISPLAY_NAME} CLI / python library")
    print("or wish to make feature requests, visit:")
    print("https://github.com/jeremiah-k/mtjk/issues")
    print("When adding an issue, be sure to include the following info:")
    print(f" System: {platform.system()}")
    print(f"   Platform: {platform.platform()}")
    print(f"   Release: {platform.uname().release}")
    print(f"   Machine: {platform.uname().machine}")
    print(f"   Encoding (stdin): {sys.stdin.encoding}")
    print(f"   Encoding (stdout): {sys.stdout.encoding}")
    the_version = get_active_version()
    pypi_version = meshtastic.util.check_if_newer_version()
    if pypi_version:
        print(
            f" {PROJECT_DISPLAY_NAME}: v{the_version} (*** newer version v{pypi_version} available ***)"
        )
    else:
        print(f" {PROJECT_DISPLAY_NAME}: v{the_version}")
    print(f" Executable: {sys.argv[0]}")
    print(
        f" Python: {platform.python_version()} {platform.python_implementation()} {platform.python_compiler()}"
    )
    print("")
    print("Please add the output from the command: meshtastic --info")


class _ConfigureReconnectResult(enum.Enum):
    RECONNECT_FAILED = "reconnect_failed"
    CONFIG_RELOAD_FAILED = "config_reload_failed"
    VERIFICATION_INCOMPLETE = "verification_incomplete"
    VERIFIED = "verified"


def _post_configure_reconnect_and_verify(
    interface: MeshInterface,
    *,
    timeout: float,
    node_dest: str,
    verify_channel_url: str | None = None,
    verify_config_fields: dict[str, dict[str, Any]] | None = None,
    verify_module_config_fields: dict[str, dict[str, Any]] | None = None,
) -> _ConfigureReconnectResult:
    """Reconnect after a configure commit, reload config, and verify values.

    After ``commitSettingsTransaction()``, the firmware may reboot the device.
    This helper:

    1. Waits for the interface to disconnect and reconnect within *timeout*.
    2. Calls ``waitForConfig()`` to reload the device configuration.
    3. If any verification targets were provided (channel URL, config fields,
       or module config fields), performs value-aware comparison of the
       explicitly requested settings against what the device reports.

    Returns a _ConfigureReconnectResult indicating the outcome.
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
        return _ConfigureReconnectResult.RECONNECT_FAILED

    try:
        interface.waitForConfig()
        logger.info("Device config reloaded after reboot.")
    except Exception:
        logger.warning(
            "Device reconnected but config reload failed; "
            "configuration may still be applying.",
            exc_info=True,
        )
        return _ConfigureReconnectResult.CONFIG_RELOAD_FAILED

    has_verification = (
        verify_channel_url or verify_config_fields or verify_module_config_fields
    )
    if not has_verification:
        return _ConfigureReconnectResult.VERIFIED

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
            return _ConfigureReconnectResult.CONFIG_RELOAD_FAILED

    try:
        result = _verify_post_reconnect_config(
            interface,
            node_dest,
            verify_channel_url=verify_channel_url,
            verify_config_fields=verify_config_fields,
            verify_module_config_fields=verify_module_config_fields,
        )
    except Exception:
        logger.warning(
            "Post-reconnect verification failed unexpectedly.",
            exc_info=True,
        )
        return _ConfigureReconnectResult.VERIFICATION_INCOMPLETE

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


def _send_local_factory_reset_and_wait(
    reset_node: Any,
    *,
    full: bool,
    timeout: float | None = None,
) -> mesh_pb2.MeshPacket | None:
    """Compatibility wrapper for the canonical device reset helper."""
    return cli_device_actions.send_local_factory_reset_and_wait(
        reset_node, full=full, timeout=timeout, cli_print=_cli_print
    )



@contextlib.contextmanager
def _temporary_instance_attributes(
    instance: Any, overrides: dict[str, Any]
) -> Iterator[None]:
    """Compatibility wrapper for temporary instance attribute overrides."""
    with cli_device_actions.temporary_instance_attributes(instance, overrides):
        yield



def _post_factory_reset_ready_probe(interface: MeshInterface) -> None:
    """Compatibility wrapper for the canonical factory-reset readiness probe."""
    cli_device_actions.post_factory_reset_ready_probe(interface)



def _validate_non_empty_mapping_sections(
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
            _cli_exit(
                f"ERROR: '{top_level_key}.{section_name}' must be a non-empty mapping, got "
                f"{type(section_value).__name__}"
            )
        validated_sections[section_name] = section_value
    return validated_sections


def _preflight_configure_sections(
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
    token = _CONFIGURE_PREFLIGHT_MODE.set(True)
    try:
        for top_level_key, source_message, sections in roots:
            if not sections:
                continue
            candidate = type(source_message)()
            candidate.CopyFrom(source_message)
            for section, section_values in sections.items():
                failed_fields: list[str] = []
                applied = traverseConfig(
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
                _cli_exit(
                    f"Failed to apply {top_level_key} section {section!r} "
                    f"due to structural errors.{field_suffix}"
                )
    finally:
        _CONFIGURE_PREFLIGHT_MODE.reset(token)


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
) -> bool:
    """Return True when requested channel URL already matches loaded device state."""
    local_config = getattr(target_node, "localConfig", None)
    has_field = getattr(local_config, "HasField", None)
    if local_config is None or not callable(has_field) or not has_field("lora"):
        return False
    return _verify_channel_url_against_state(
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
) -> _ConfigureReconnectResult:
    if not interface.isConnected.is_set():
        logger.warning("Post-reconnect verification skipped: transport disconnected.")
        return _ConfigureReconnectResult.VERIFICATION_INCOMPLETE

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
        if not _verify_channel_url_against_state(
            verify_channel_url,
            device_channels=getattr(target_node, "channels", None),
            device_lora_config=device_lora_config,
        ):
            logger.warning(
                "Channel URL verification: device state does not match requested URL."
            )
            return _ConfigureReconnectResult.VERIFICATION_INCOMPLETE
        verified_fields.append("channel_url")

    if verify_config_fields and not _verify_config_sections(
        verify_config_fields,
        target_node.localConfig,
        "Config",
        verified_fields=verified_fields,
    ):
        return _ConfigureReconnectResult.VERIFICATION_INCOMPLETE

    if verify_module_config_fields and not _verify_config_sections(
        verify_module_config_fields,
        target_node.moduleConfig,
        "Module config",
        verified_fields=verified_fields,
    ):
        return _ConfigureReconnectResult.VERIFICATION_INCOMPLETE

    if not interface.isConnected.is_set():
        logger.warning(
            "Post-reconnect verification did not complete: transport disconnected."
        )
        return _ConfigureReconnectResult.VERIFICATION_INCOMPLETE

    if verified_fields:
        logger.info("Verified: %s", ", ".join(verified_fields))

    return _ConfigureReconnectResult.VERIFIED


# COMPAT_STABLE_SHIM: historical snake_case helper name.
def support_info() -> None:
    """Compatibility alias for supportInfo()."""
    supportInfo()


def onReceive(packet: dict[str, Any], interface: MeshInterface) -> None:
    """Handle an incoming mesh packet, optionally send a text reply, and close the interface when appropriate."""
    args = mt_config.args
    try:
        d = packet.get("decoded")
        logger.debug("in onReceive() d:%s", d)

        is_text_reply = (
            args
            and args.sendtext
            and d is not None
            and interface.myInfo is not None
            and packet.get("to") == interface.myInfo.my_node_num
            and d.get("portnum")
            == portnums_pb2.PortNum.Name(portnums_pb2.PortNum.TEXT_MESSAGE_APP)
        )
        if is_text_reply:
            interface.close()

        if d is not None and args and args.reply:
            msg = d.get("text")
            if msg:
                # Prevent infinite loop: ignore own messages and auto-reply echoes
                if (
                    interface.myInfo
                    and packet.get("from") == interface.myInfo.my_node_num
                ):
                    return
                if msg.startswith("got msg '"):
                    return
                rxChannel = packet.get("channel", 0)
                targetChannel = (
                    int(args.ch_index) if args.ch_index is not None else None
                )
                if targetChannel is None or rxChannel == targetChannel:
                    rxSnr = packet.get("rxSnr", "unknown")
                    hopLimit = packet.get("hopLimit", "unknown")
                    print(f"message: {msg}")
                    reply = (
                        f"got msg '{msg}' with rxSnr: {rxSnr} and hopLimit: {hopLimit}"
                    )
                    print(f"Received channel {rxChannel}. Sending reply: {reply}")
                    interface.sendText(reply, channelIndex=rxChannel)
                else:
                    print(
                        f"Ignored message on channel {rxChannel} (waiting for channel {targetChannel})"
                    )

    except Exception as ex:
        logger.warning("Error processing received packet: %s", ex)


def onConnection(interface: MeshInterface, topic: Any = pub.AUTO_TOPIC) -> None:
    """Notify about a change in the radio connection state."""
    _ = interface
    topic_name = topic.getName() if hasattr(topic, "getName") else str(topic)
    _cli_print(f"Connection changed: {topic_name}")


def checkChannel(interface: MeshInterface, channelIndex: int) -> bool:
    """Determine whether the local node has the channel at the given index enabled."""
    if hasattr(type(interface.localNode), "getChannelCopyByChannelIndex"):
        ch = interface.localNode.getChannelCopyByChannelIndex(channelIndex)
    else:
        ch = interface.localNode.getChannelByChannelIndex(channelIndex)
    logger.debug("ch:%s", ch)
    return bool(ch and ch.role != channel_pb2.Channel.Role.DISABLED)


def _normalize_pref_name(comp_name: str) -> str:
    """Normalize a preference path to canonical snake_case and apply aliases."""
    canonical = ".".join(
        meshtastic.util.camel_to_snake(part.strip()) for part in comp_name.split(".")
    )
    normalized = _PREFERENCE_FIELD_ALIASES.get(canonical, canonical)
    if normalized != canonical:
        logger.debug(
            "Using compatibility alias for config field %s -> %s",
            comp_name,
            normalized,
        )
    return normalized


def _display_pref_name(comp_name: str) -> str:
    """Format a canonical preference path for user-facing output."""
    if not mt_config.camel_case:
        return comp_name
    return ".".join(
        meshtastic.util.snake_to_camel(part) for part in comp_name.split(".")
    )


_SECRET_PREF_FIELDS: frozenset[str] = frozenset(
    {
        "psk",
        "channel_psk",
        "private_key",
        "public_key",
        "admin_key",
        "session_passkey",
        "secret",
        "api_key",
        "auth_token",
    }
)
_SECRET_PREF_PATHS: frozenset[str] = frozenset(
    {
        "network.wifi_ssid",
        "network.wifi_psk",
        "mqtt.username",
        "mqtt.password",
        "bluetooth.fixed_pin",
        "security.session_passkey",
    }
)
_REDACTED_PREF_VALUE = "<redacted>"
_SET_VALUE_REJECTED_MESSAGE = "value rejected by validation"


class _DescriptorLike(Protocol):
    """Structural descriptor type shared by pure-Python and upb protobuf runtimes."""

    @property
    def fields(self) -> Sequence[FieldDescriptor]:
        """Return fields declared by this message descriptor."""


class _NamedConfigType(Protocol):
    """Protocol for config section objects exposing a `name` attribute."""

    name: str


def _is_secret_pref(name: str) -> bool:
    """Return whether a preference path is classified as secret-bearing."""
    normalized = _normalize_pref_name(name)
    field_name = normalized.rsplit(".", maxsplit=1)[-1]
    return normalized in _SECRET_PREF_PATHS or field_name in _SECRET_PREF_FIELDS


def _redact_pref_value(name: str, value: str) -> str:
    """Return a redacted placeholder for secret-bearing preference paths."""
    return _REDACTED_PREF_VALUE if _is_secret_pref(name) else value


def getPref(node: Any, comp_name: str, *, allow_secrets: bool = False) -> bool:
    """Retrieve and display a node configuration preference or populated section fields.

    Parameters
    ----------
    node : Any
        Node exposing local and module configuration data and configuration requests.
    comp_name : str
        Preference path or configuration section name to retrieve.
    allow_secrets : bool
        Whether sensitive preference values may be displayed without redaction.

    Returns
    -------
    bool
        ``True`` if the preference exists and values were displayed or requested,
        ``False`` if it was not found.
    """

    def _print_setting(
        config_type: _NamedConfigType,
        uni_name: str,
        pref_value: str | list[str],
        *,
        repeated: bool,
        secret_name: str,
    ) -> None:
        """Print a configuration preference and its value to stdout and the debug log.

        When `repeated` is True, `pref_value` is treated as an iterable and
            each element is converted to a string; otherwise the single value is
            converted to a string. Output is formatted as "<section>.<name>: <value>".

        Parameters
        ----------
        config_type : _NamedConfigType
            Object with a `name` attribute identifying the configuration section.
        uni_name : str
            The preference name within the configuration section.
        pref_value : str | list[str]
            The preference value to print; an iterable when `repeated` is True.
        repeated : bool
            If True, treat `pref_value` as a sequence and print the list of stringified values.
        secret_name : str
            Canonical snake_case field name used to determine whether to redact.
        """
        if repeated:
            display_value: str | list[str] = [
                (
                    meshtastic.util.toStr(v)
                    if allow_secrets
                    else _redact_pref_value(secret_name, meshtastic.util.toStr(v))
                )
                for v in pref_value
            ]
            log_value: str | list[str] = [
                _redact_pref_value(secret_name, meshtastic.util.toStr(v))
                for v in pref_value
            ]
        else:
            raw_display = meshtastic.util.toStr(pref_value)
            display_value = (
                raw_display
                if allow_secrets
                else _redact_pref_value(secret_name, raw_display)
            )
            log_value = _redact_pref_value(secret_name, raw_display)
        print(f"{str(config_type.name)}.{uni_name}: {str(display_value)}")
        logger.debug("%s.%s: %s", config_type.name, uni_name, log_value)

    comp_name = _normalize_pref_name(comp_name)
    name = splitCompoundName(comp_name)
    wholeField = name[0] == name[1]  # We want the whole field

    camel_name = meshtastic.util.snake_to_camel(name[1])
    # Note: protobufs has the keys in snake_case, so snake internally
    snake_name = meshtastic.util.camel_to_snake(name[1])
    uni_name = camel_name if mt_config.camel_case else snake_name
    logger.debug("snake_name:%s camel_name:%s", snake_name, camel_name)
    logger.debug("use camel:%s", mt_config.camel_case)

    # First validate the input
    localConfig = node.localConfig
    moduleConfig = node.moduleConfig
    found: bool = False
    config = localConfig
    config_type = None
    pref = None
    for config in [localConfig, moduleConfig]:
        objDesc = config.DESCRIPTOR
        config_type = objDesc.fields_by_name.get(name[0])
        pref = None
        if config_type:
            pref = config_type.message_type.fields_by_name.get(snake_name)
            if pref is not None or wholeField:
                found = True
                break

    if not found:
        print(
            f"{localConfig.__class__.__name__} and {moduleConfig.__class__.__name__} do not have an attribute {uni_name}."
        )
        print("Choices are...")
        printConfig(localConfig)
        printConfig(moduleConfig)
        return False

    # Check if we need to request the config
    if config_type is None:
        return False

    if len(config.ListFields()) != 0 and (pref is not None or wholeField):
        # read the value
        config_values = getattr(config, config_type.name)
        if not wholeField:
            if pref is None:
                return False
            pref_value = getattr(config_values, pref.name)
            repeated = _is_repeated_field(pref)
            _print_setting(
                config_type,
                uni_name,
                pref_value,
                repeated=repeated,
                secret_name=f"{config_type.name}.{snake_name}",
            )
        else:
            for field in config_values.ListFields():
                repeated = _is_repeated_field(field[0])
                _print_setting(
                    config_type,
                    field[0].name,
                    field[1],
                    repeated=repeated,
                    secret_name=f"{config_type.name}.{field[0].name}",
                )
    else:
        # Always show whole field for remote node
        node.requestConfig(config_type)

    return True


def splitCompoundName(comp_name: str) -> list[str]:
    """Split a dotted preference name into segments, guaranteeing at least two elements.

    If `comp_name` contains one or more dots, returns the list produced by splitting on
    '.'. If it contains no dot, returns a two-element list with `comp_name` repeated.

    Parameters
    ----------
    comp_name : str
        The dotted preference name to split.

    Returns
    -------
    list[str]
        Segments from splitting `comp_name` on '.', or `[comp_name, comp_name]` when no dot is present.
    """
    name: list[str] = comp_name.split(".")
    if len(name) < 2:
        name.append(comp_name)
    return name


def traverseConfig(
    config_root: str,
    config: dict[str, Any],
    interface_config: Any,
    failed_fields: list[str] | None = None,
) -> bool:
    """Recursively apply values from a nested mapping onto a target configuration by walking dot-separated paths.

    Parameters
    ----------
    config_root : str
        Dot-separated prefix for the current configuration path (e.g., "channel.0").
    config : dict[str, Any]
        Nested mapping where keys are field names and values are either sub-mappings or leaf values to set.
    interface_config : Any
        Target configuration object that will receive the applied leaf values.
    failed_fields : list[str] | None
        Optional mutable list to collect failing fully-qualified field paths
        when a leaf assignment fails. (Default value = None)

    Returns
    -------
    bool
        `True` when traversal completes and all leaf values are successfully
        applied, `False` if any leaf assignment fails validation.
    """
    skipped_by_section: dict[str, list[str]] = {}

    def _traverse(root: str, cfg: dict[str, Any], icfg: Any) -> bool:
        s_name = meshtastic.util.camel_to_snake(root)
        for pref in cfg:
            pref_name = f"{s_name}.{pref}"
            if isinstance(cfg[pref], dict):
                if not _traverse(pref_name, cfg[pref], icfg):
                    return False
            else:
                if not _resolve_pref(icfg, pref_name):
                    parts = pref_name.split(".")
                    section = parts[0]
                    relative = ".".join(parts[1:]) if len(parts) > 1 else pref_name
                    skipped_by_section.setdefault(section, []).append(relative)
                    continue
                try:
                    ok = setPref(icfg, pref_name, cfg[pref])
                except (ValueError, TypeError, OverflowError, binascii.Error):
                    if failed_fields is not None:
                        failed_fields.append(pref_name)
                    return False
                if not ok:
                    if failed_fields is not None:
                        failed_fields.append(pref_name)
                    return False
        return True

    success = _traverse(config_root, config, interface_config)

    if not _CONFIGURE_PREFLIGHT_MODE.get():
        for section, fields in skipped_by_section.items():
            field_list = ", ".join(fields)
            logger.warning(
                "Skipping %d unknown field(s) from %s: %s",
                len(fields),
                section,
                field_list,
            )

    return success


def _walk_config_path(config: Any, name_parts: list[str]) -> tuple[Any, Any | None]:
    """Return the parent message and descriptor for a dotted config path."""
    if not name_parts:
        return config, None

    normalized_parts = [
        meshtastic.util.camel_to_snake(name_part) for name_part in name_parts
    ]
    config_part = config
    config_type = config.DESCRIPTOR.fields_by_name.get(normalized_parts[0])
    for name_part in normalized_parts[1:-1]:
        if config_type is None or config_type.message_type is None:
            return config_part, None
        config_part = getattr(config_part, config_type.name)
        config_type = config_type.message_type.fields_by_name.get(name_part)
    if (
        len(normalized_parts) > 2
        and config_type is not None
        and config_type.message_type is None
    ):
        return config_part, None
    return config_part, config_type


def _resolve_pref(config: Any, comp_name: str) -> bool:
    """Check whether a dotted field path resolves to a valid protobuf field."""
    comp_name = _normalize_pref_name(comp_name)
    name = splitCompoundName(comp_name)
    snake_name = meshtastic.util.camel_to_snake(name[-1])
    _config_part, config_type = _walk_config_path(config, name)
    if config_type and config_type.message_type is not None:
        return config_type.message_type.fields_by_name.get(snake_name) is not None
    return config_type is not None


def _protobuf_field_type_label(field: FieldDescriptor) -> str:
    """Return a concise user-facing type label for a protobuf field.

    Parameters
    ----------
    field : FieldDescriptor
        Protobuf field whose expected CLI value type should be described.

    Returns
    -------
    str
        Concise user-facing label for the field type.
    """
    integer_types = {
        FieldDescriptor.TYPE_INT32,
        FieldDescriptor.TYPE_INT64,
        FieldDescriptor.TYPE_UINT32,
        FieldDescriptor.TYPE_UINT64,
        FieldDescriptor.TYPE_SINT32,
        FieldDescriptor.TYPE_SINT64,
        FieldDescriptor.TYPE_FIXED32,
        FieldDescriptor.TYPE_FIXED64,
        FieldDescriptor.TYPE_SFIXED32,
        FieldDescriptor.TYPE_SFIXED64,
    }
    if field.type in integer_types:
        return "integer"

    type_labels = {
        FieldDescriptor.TYPE_ENUM: "enum name or integer",
        FieldDescriptor.TYPE_FLOAT: "number",
        FieldDescriptor.TYPE_DOUBLE: "number",
        FieldDescriptor.TYPE_BOOL: "boolean",
        FieldDescriptor.TYPE_BYTES: "bytes (hex/base64)",
        FieldDescriptor.TYPE_STRING: "string",
    }
    return type_labels.get(field.type, "compatible value")


def _print_channel_field_choices(settings: Any, pref_name: str) -> None:
    """Print available channel-setting fields after an unknown --ch-set name.

    Parameters
    ----------
    settings : Any
        Channel settings protobuf whose fields should be listed.
    pref_name : str
        Unknown preference name supplied to ``--ch-set``.
    """
    print(f"{settings.__class__.__name__} does not have an attribute {pref_name}.")
    print("Choices are...")
    for field in settings.DESCRIPTOR.fields:
        if field.name != "module_settings":
            print(field.name)
            continue

        print(f"{field.name}:")
        if field.message_type is None:
            continue
        for sub_field in sorted(field.message_type.fields, key=lambda item: item.name):
            print(f"    {field.name}.{sub_field.name}")


def _reject_pref_value(
    field: FieldDescriptor,
    *,
    field_path: str,
    raw_value: Any,
) -> bool:
    """Report one invalid preference value without exposing secret input.

    Parameters
    ----------
    field : FieldDescriptor
        Descriptor for the field whose value was rejected.
    field_path : str
        Canonical dotted preference path used in diagnostics and redaction.
    raw_value : Any
        Original caller-supplied value used for safe diagnostic rendering.

    Returns
    -------
    bool
        Always ``False`` for convenient use from validation branches.

    Raises
    ------
    _PreferenceValueError
        If fatal preference-value handling is active.
    """
    display_value = _redact_pref_value(field_path, repr(raw_value))
    message = (
        f"Invalid value {display_value} for {field_path}; "
        f"expected {_protobuf_field_type_label(field)}."
    )
    if _SET_PREF_VALUE_ERRORS_FATAL.get():
        raise _PreferenceValueError(message)
    _report_pref_validation(message)
    return False


def _assign_scalar_pref_value(
    target: Any,
    field: FieldDescriptor,
    value: Any,
    *,
    field_path: str,
    raw_value: Any,
) -> bool:
    """Assign one non-repeated protobuf field without leaking conversion errors.

    Parameters
    ----------
    target : Any
        Protobuf message instance that owns ``field``.
    field : FieldDescriptor
        Descriptor for the scalar field being assigned.
    value : Any
        Converted value to assign to the protobuf field.
    field_path : str
        Canonical dotted preference path used in diagnostics and redaction.
    raw_value : Any
        Original caller-supplied value used for safe diagnostic rendering.

    Returns
    -------
    bool
        ``True`` when assignment succeeds; ``False`` when a non-fatal
        validation failure is reported.

    Raises
    ------
    _PreferenceValueError
        If assignment fails while fatal preference-value handling is active.
    """
    try:
        setattr(target, field.name, value)
        return True
    except (TypeError, ValueError, OverflowError):
        if field.type == FieldDescriptor.TYPE_STRING and not isinstance(value, str):
            try:
                setattr(target, field.name, str(value))
                return True
            except (TypeError, ValueError, OverflowError):
                pass
    return _reject_pref_value(field, field_path=field_path, raw_value=raw_value)


def setPref(config: Any, comp_name: str, raw_val: Any) -> bool:
    """Set a protobuf configuration or channel field identified by a dot-separated path.

    This updates the target field on the given protobuf-like message, converting the provided
    value to the field's expected type when possible, resolving enum names, validating
    certain fields (for example, `wifi_psk` requires length >= 8), and handling
    repeated fields (replace, append, or clear) according to the supplied value.

    Parameters
    ----------
    config : Any
        The protobuf configuration or channel message to modify.
    comp_name : str
        Dot-separated field path (e.g., "channel.security.wifi_psk" or "node.name").
    raw_val : Any
        Value to assign; may be a string, number, list (for repeated fields), or already-typed value.

    Returns
    -------
    bool
        `True` if the named field was found and successfully set or updated, `False` otherwise.
    """

    comp_name = _normalize_pref_name(comp_name)
    name = splitCompoundName(comp_name)

    snake_name = meshtastic.util.camel_to_snake(name[-1])
    camel_name = meshtastic.util.snake_to_camel(name[-1])
    uni_name = camel_name if mt_config.camel_case else snake_name
    logger.debug("snake_name:%s", snake_name)
    logger.debug("camel_name:%s", camel_name)

    config_part, config_type = _walk_config_path(config, name)
    pref = None
    if config_type and config_type.message_type is not None:
        pref = config_type.message_type.fields_by_name.get(snake_name)
    # Others like ChannelSettings are standalone
    elif config_type:
        pref = config_type

    if (not pref) or (not config_type):
        return False

    # Handle uint32 bitfields that have an associated enum of flag names.
    bitfield_enum = None
    if config_type.message_type is not None:
        bitfield_path = f"{config_type.name}.{pref.name}"
        bitfield_enum = BITFIELD_ENUMS.get(bitfield_path)

    if bitfield_enum:
        try:
            val = _parse_bitfield_value(bitfield_enum, raw_val)
        except ValueError as e:
            _report_pref_validation(f"ERROR: {e}")
            return False
    elif isinstance(raw_val, str):
        try:
            val = meshtastic.util.fromStr(raw_val)
        except (ValueError, binascii.Error):
            return _reject_pref_value(
                pref,
                field_path=comp_name,
                raw_value=raw_val,
            )
    else:
        val = raw_val
    logger.debug("val:%s", _redact_pref_value(comp_name, meshtastic.util.toStr(val)))

    if snake_name == "wifi_psk" and len(str(raw_val)) < 8:
        _report_pref_validation(
            "Warning: network.wifi_psk must be 8 or more characters."
        )
        return False

    enumType = pref.enum_type
    if enumType and isinstance(val, str):
        # We've failed so far to convert this string into an enum, try to find it by reflection
        ev = enumType.values_by_name.get(val)
        if ev:
            val = ev.number
        else:
            _report_pref_validation(
                f"{name[0]}.{uni_name} does not have an enum called {val}, so you can not set it."
            )
            _report_pref_validation("Choices in sorted order are:")
            names = []
            for f in enumType.values:
                # Note: We must use the value of the enum (regardless if camel or snake case)
                names.append(f"{f.name}")
            for temp_name in sorted(names):
                _report_pref_validation(f"    {temp_name}")
            return False

    # repeating fields need to be handled with append, not setattr
    assignment_ok = True
    print_assignment = True
    if not _is_repeated_field(pref):
        target = (
            getattr(config_part, config_type.name)
            if config_type.message_type is not None
            else config_part
        )
        assignment_ok = _assign_scalar_pref_value(
            target,
            pref,
            val,
            field_path=comp_name,
            raw_value=raw_val,
        )
    else:
        target = (
            getattr(config_part, config_type.name)
            if config_type.message_type is not None
            else config_part
        )
        candidate = type(target)()
        candidate.CopyFrom(target)
        candidate_values = getattr(candidate, pref.name)
        try:
            if isinstance(val, list):
                new_vals = [
                    meshtastic.util.fromStr(item) if isinstance(item, str) else item
                    for item in val
                ]
                candidate_values[:] = new_vals
            elif val == 0:
                del candidate_values[:]
            else:
                cur_vals = [x for x in candidate_values if x not in [0, "", b""]]
                if val not in cur_vals:
                    cur_vals.append(val)
                candidate_values[:] = cur_vals
        except (TypeError, ValueError, OverflowError, binascii.Error):
            assignment_ok = _reject_pref_value(
                pref,
                field_path=comp_name,
                raw_value=raw_val,
            )
        else:
            target.CopyFrom(candidate)
            if not isinstance(val, list):
                if val == 0:
                    if not _CONFIGURE_PREFLIGHT_MODE.get():
                        _cli_print(f"Clearing {pref.name} list")
                else:
                    display_value = _redact_pref_value(
                        comp_name, meshtastic.util.toStr(raw_val)
                    )
                    if not _CONFIGURE_PREFLIGHT_MODE.get():
                        _cli_print(f"Adding '{display_value}' to the {pref.name} list")
                print_assignment = False

    if assignment_ok and print_assignment:
        prefix = (
            f"{'.'.join(name[0:-1])}." if config_type.message_type is not None else ""
        )
        display_value = _redact_pref_value(comp_name, meshtastic.util.toStr(raw_val))
        if not _CONFIGURE_PREFLIGHT_MODE.get():
            _cli_print(f"Set {prefix}{uni_name} to {display_value}")

    return assignment_ok


def _handle_ota_update(
    interface: MeshInterface,
    args: Any,
    getNode_kwargs: dict[str, Any],
) -> None:
    """Compatibility wrapper for the canonical Wi-Fi OTA action."""
    cli_device_actions.handle_ota_update(
        interface,
        args,
        getNode_kwargs,
        cli_exit=_cli_exit,
        cli_print=_cli_print,
        is_local_destination=_is_local_destination,
    )



def _print_set_field_choices(node: Any, pref_names: Sequence[str]) -> None:
    """Print historical field-not-found guidance for one or more --set names.

    Parameters
    ----------
    node : Any
        Node whose local and module configuration schemas provide the choices.
    pref_names : Sequence[str]
        Unknown preference names to identify before printing choices.
    """
    names = list(dict.fromkeys(pref_names))
    for pref_name in names:
        print(
            f"{node.localConfig.__class__.__name__} and "
            f"{node.moduleConfig.__class__.__name__} do not have an attribute {pref_name}."
        )
    print("Choices are...")
    printConfig(node.localConfig)
    printConfig(node.moduleConfig)


def _normalize_set_entries(
    set_entries: Sequence[Sequence[Any] | None],
) -> list[tuple[str, Any]]:
    """Normalize argparse --set entries into explicit name/value pairs.

    Parameters
    ----------
    set_entries : Sequence[Sequence[Any] | None]
        Parsed ``--set`` entries, each containing at least a field name and value.
        Any additional elements are ignored for compatibility with the historical
        ``item[0]``/``item[1]`` parsing contract.

    Returns
    -------
    list[tuple[str, Any]]
        Normalized ``(field_name, value)`` pairs with incomplete entries omitted.
    """
    return [
        (str(item[0]), item[1])
        for item in set_entries
        if item is not None and len(item) >= 2
    ]


def _format_set_preflight_exception(pref_name: str, exc: Exception) -> str:
    """Format a preflight exception without exposing secret preference values.

    Parameters
    ----------
    pref_name : str
        Canonical preference path whose validation raised ``exc``.
    exc : Exception
        Validation exception raised by the protobuf/runtime conversion path.

    Returns
    -------
    str
        Safe error detail suitable for the aggregated CLI failure message.
    """
    if isinstance(exc, _PreferenceValueError):
        return str(exc)
    if _is_secret_pref(pref_name):
        return (
            f"{pref_name}: invalid value {_REDACTED_PREF_VALUE} "
            f"({type(exc).__name__})"
        )
    return f"{pref_name}: {exc}"


def _resolve_set_target(
    configs: Sequence[Any], pref_name: str
) -> tuple[Any, FieldDescriptor] | None:
    """Resolve the owning config message and root field for a normalized preference.

    Parameters
    ----------
    configs : Sequence[Any]
        Configuration wrapper messages to search in resolution order.
    pref_name : str
        Normalized dotted preference path.

    Returns
    -------
    tuple[Any, FieldDescriptor] | None
        Owning config wrapper and root field descriptor, or ``None`` when the
        root field does not exist in any supplied configuration.
    """
    root_field = splitCompoundName(pref_name)[0]
    for config in configs:
        config_type = config.DESCRIPTOR.fields_by_name.get(root_field)
        if config_type is not None:
            return config, config_type
    return None


def _ensure_set_sections_loaded(
    node: Any, set_entries: Sequence[tuple[str, Any]]
) -> None:
    """Request missing config sections before creating preflight snapshots.

    Parameters
    ----------
    node : Any
        Target node whose cached local/module configuration will be validated.
    set_entries : Sequence[tuple[str, Any]]
        Parsed ``--set`` name/value entries. Names are normalized before resolution.

    Notes
    -----
    Requests are deduplicated by config section. Protobuf message presence, not
    ``ListFields()``, distinguishes an already-loaded default-valued section from
    a section that has never been received. Unknown preference paths do not
    trigger device reads.
    """
    configs = (node.localConfig, node.moduleConfig)
    requested_sections: set[tuple[str, str]] = set()
    for raw_pref_name, _raw_value in set_entries:
        pref_name = _normalize_pref_name(raw_pref_name)
        resolved = _resolve_set_target(configs, pref_name)
        if resolved is None:
            continue
        config, config_type = resolved
        if not _resolve_pref(config, pref_name):
            continue

        section_key = (config.DESCRIPTOR.full_name, config_type.name)
        if section_key in requested_sections:
            continue
        requested_sections.add(section_key)
        if not config.HasField(config_type.name):
            node.requestConfig(config_type)


def _preflight_set_entries(node: Any, set_entries: Sequence[tuple[str, Any]]) -> bool:
    """
    Validate all --set entries against configuration copies before applying changes.

    Parameters
    ----------
    node : Any
        Node providing the current local and module configuration.
    set_entries : Sequence[tuple[str, Any]]
        Preference names and raw values to validate as one batch.

    Returns
    -------
    bool
        ``True`` if every entry is valid; ``False`` if an unknown field or semantic
        validation failure rejects the batch.
    """
    candidates: list[Any] = []
    for source in (node.localConfig, node.moduleConfig):
        candidate = type(source)()
        candidate.CopyFrom(source)
        candidates.append(candidate)

    fatal_errors: list[str] = []
    unknown_fields: list[str] = []
    value_rejections: list[tuple[str, tuple[str, ...]]] = []
    token = _CONFIGURE_PREFLIGHT_MODE.set(True)
    try:
        for raw_pref_name, raw_value in set_entries:
            pref_name = _normalize_pref_name(raw_pref_name)
            resolved = _resolve_set_target(candidates, pref_name)
            if resolved is None:
                unknown_fields.append(pref_name)
                continue
            candidate, _config_type = resolved
            if not _resolve_pref(candidate, pref_name):
                unknown_fields.append(pref_name)
                continue

            validation_messages: list[str] = []
            reporter_token = _PREF_VALIDATION_REPORTER.set(validation_messages.append)
            try:
                try:
                    with _fatal_preference_value_errors():
                        valid = setPref(candidate, pref_name, raw_value)
                finally:
                    _PREF_VALIDATION_REPORTER.reset(reporter_token)
            except (TypeError, ValueError, OverflowError, binascii.Error) as exc:
                fatal_errors.append(_format_set_preflight_exception(pref_name, exc))
                continue
            if not valid:
                value_rejections.append((pref_name, tuple(validation_messages)))
    finally:
        _CONFIGURE_PREFLIGHT_MODE.reset(token)

    if unknown_fields:
        _print_set_field_choices(node, unknown_fields)

    if fatal_errors:
        detail_lines = [f"  - {error}" for error in fatal_errors]
        for pref_name, messages in value_rejections:
            detail_lines.append(f"  - {pref_name}: {_SET_VALUE_REJECTED_MESSAGE}")
            detail_lines.extend(f"      {message}" for message in messages)
        details = "\n".join(detail_lines)
        _cli_exit(f"ERROR: --set batch rejected before applying changes:\n{details}")

    for pref_name, messages in value_rejections:
        if messages:
            for message in messages:
                _report_pref_validation(message)
        else:
            _report_pref_validation(f"{pref_name}: {_SET_VALUE_REJECTED_MESSAGE}")

    return not (unknown_fields or value_rejections)


def _handle_set_command(
    interface: MeshInterface,
    args: Any,
    getNode_kwargs: dict[str, Any],
) -> None:
    """
    Validate and apply a CLI ``--set`` batch without partial updates.

    Parameters
    ----------
    interface : MeshInterface
        Active interface used to resolve the target node.
    args : Any
        Parsed CLI arguments containing the ``--set`` entries and destination.
    getNode_kwargs : dict[str, Any]
        Additional keyword arguments forwarded to ``interface.getNode``.

    Notes
    -----
    Invalid batches are rejected before modifying the device. Valid batches write
    all affected configuration sections and use a transaction when multiple
    sections are changed.
    """
    node = interface.getNode(args.dest, False, **getNode_kwargs)
    set_entries = _normalize_set_entries(args.set)
    _ensure_set_sections_loaded(node, set_entries)
    if not _preflight_set_entries(node, set_entries):
        return

    live_configs = (node.localConfig, node.moduleConfig)
    fields: set[str] = set()
    for raw_pref_name, raw_value in set_entries:
        normalized_pref_name = _normalize_pref_name(raw_pref_name)
        resolved = _resolve_set_target(live_configs, normalized_pref_name)
        if resolved is None:
            _cli_exit(
                "ERROR: --set field no longer resolves after successful preflight: "
                f"{normalized_pref_name}."
            )
        config, config_type = resolved
        try:
            found = setPref(config, normalized_pref_name, raw_value)
        except (TypeError, ValueError, OverflowError, binascii.Error) as exc:
            detail = _format_set_preflight_exception(normalized_pref_name, exc)
            _cli_exit(
                "ERROR: --set apply diverged after successful preflight:\n"
                f"  - {detail}"
            )
        if not found:
            _cli_exit(
                "ERROR: --set apply diverged after successful preflight for "
                f"{normalized_pref_name}."
            )
        fields.add(config_type.name)

    if fields:
        _cli_print("Writing modified preferences to device")
        if len(fields) > 1:
            _cli_print("Using a configuration transaction")
            node.beginSettingsTransaction()
        for field in fields:
            _cli_print(f"Writing {field} configuration to device")
            node.writeConfig(field)
        if len(fields) > 1:
            node.commitSettingsTransaction()


def _pace_configure_write(
    remaining_writes: int,
    *,
    sleep_fn: Callable[[float], None] = time.sleep,
) -> None:
    """Yield briefly between section writes while keeping transactions short."""
    if remaining_writes > 0:
        sleep_fn(CONFIG_WRITE_PACE_SECONDS)


def _apply_configure_channel_url(
    target_node: Any,
    raw_channel_url: Any,
    *,
    config_key: str,
) -> bool:
    """Validate and apply one configured channel URL without exposing it."""
    if not isinstance(raw_channel_url, str):
        _cli_exit(f"ERROR: {config_key} must be a string.")
    requested_channel_url = raw_channel_url.strip()
    if not requested_channel_url:
        _cli_exit(f"ERROR: {config_key} must not be blank.")

    if _channel_url_matches_current_device_state(target_node, requested_channel_url):
        _cli_print("Channel url already matches device state; skipping apply.")
        logger.info("Skipping setURL apply because channel URL already matches.")
        return False

    _cli_print("Setting channel url to <redacted>")
    target_node.setURL(requested_channel_url)
    time.sleep(CONFIG_SETURL_DELAY_SECONDS)
    return True


def _handle_configure_command(
    interface: MeshInterface,
    args: Any,
    getNode_kwargs: dict[str, Any],
) -> tuple[bool, bool]:
    try:
        with open(args.configure[0], encoding="utf8") as file:
            raw_text = file.read()
        configuration = yaml.safe_load(raw_text)
    except (yaml.YAMLError, UnicodeDecodeError) as exc:
        _cli_exit(f"ERROR: Failed to parse YAML configuration: {exc}")

    if configuration is None:
        _cli_exit("ERROR: YAML configuration file is empty")
    if not isinstance(configuration, dict):
        _cli_exit(
            f"ERROR: YAML configuration must be a mapping/dictionary, got {type(configuration).__name__}"
        )
    if not configuration:
        _cli_exit("ERROR: Configuration file is empty; nothing to configure.")
    _unknown_keys = set(configuration.keys()) - _ALLOWED_CONFIGURE_KEYS
    if _unknown_keys:
        _cli_exit(
            f"ERROR: Unknown top-level key(s) in YAML: {', '.join(sorted(_unknown_keys))}"
        )

    if "channel_url" in configuration and "channelUrl" in configuration:
        _cli_exit(
            "ERROR: Cannot specify both 'channel_url' and 'channelUrl' in the same configuration file; use one."
        )
    if "owner_short" in configuration and "ownerShort" in configuration:
        _cli_exit(
            "ERROR: Cannot specify both 'owner_short' and 'ownerShort' in the same configuration file; use one."
        )

    # Pre-validate config/module_config shapes before any Phase-1 mutations.
    validated_config_sections: dict[str, dict[str, Any]] = {}
    validated_module_config_sections: dict[str, dict[str, Any]] = {}
    if "config" in configuration:
        _cfg_val = configuration["config"]
        if not isinstance(_cfg_val, dict) or not _cfg_val:
            _cli_exit(
                f"ERROR: 'config' must be a non-empty mapping, got "
                f"{type(_cfg_val).__name__}{' (empty)' if isinstance(_cfg_val, dict) else ''}"
            )
        validated_config_sections = _validate_non_empty_mapping_sections(
            top_level_key="config",
            section_mapping=_cfg_val,
        )
    if "module_config" in configuration:
        _mcfg_val = configuration["module_config"]
        if not isinstance(_mcfg_val, dict) or not _mcfg_val:
            _cli_exit(
                f"ERROR: 'module_config' must be a non-empty mapping, got "
                f"{type(_mcfg_val).__name__}{' (empty)' if isinstance(_mcfg_val, dict) else ''}"
            )
        validated_module_config_sections = _validate_non_empty_mapping_sections(
            top_level_key="module_config",
            section_mapping=_mcfg_val,
        )

    target_node = interface.getNode(args.dest, False, **getNode_kwargs)
    if validated_config_sections or validated_module_config_sections:
        _preflight_configure_sections(
            target_node,
            config_sections=validated_config_sections,
            module_config_sections=validated_module_config_sections,
        )

    phase1_started = False
    phase1_may_reconnect = False
    seturl_executed = False

    if "owner" in configuration:
        if not phase1_started:
            _cli_print(CONFIGURE_PHASE1_HEADER)
            phase1_started = True
        owner_name = str(configuration["owner"]).strip()
        if not owner_name:
            _cli_exit(
                "ERROR: Long Name cannot be empty or contain only whitespace characters"
            )
        _cli_print(f"Setting device owner to {owner_name}")
        target_node.setOwner(long_name=owner_name)
        time.sleep(CONFIG_APPLY_DELAY_SECONDS)

    if "owner_short" in configuration:
        if not phase1_started:
            _cli_print(CONFIGURE_PHASE1_HEADER)
            phase1_started = True
        owner_short_name = str(configuration["owner_short"]).strip()
        if not owner_short_name:
            _cli_exit(
                "ERROR: Short Name cannot be empty or contain only whitespace characters"
            )
        _cli_print(f"Setting device owner short to {owner_short_name}")
        target_node.setOwner(long_name=None, short_name=owner_short_name)
        time.sleep(CONFIG_APPLY_DELAY_SECONDS)

    if "ownerShort" in configuration:
        if not phase1_started:
            _cli_print(CONFIGURE_PHASE1_HEADER)
            phase1_started = True
        owner_short_name = str(configuration["ownerShort"]).strip()
        if not owner_short_name:
            _cli_exit(
                "ERROR: Short Name cannot be empty or contain only whitespace characters"
            )
        _cli_print(f"Setting device owner short to {owner_short_name}")
        target_node.setOwner(long_name=None, short_name=owner_short_name)
        time.sleep(CONFIG_APPLY_DELAY_SECONDS)

    if "location" in configuration:
        if not phase1_started:
            _cli_print(CONFIGURE_PHASE1_HEADER)
            phase1_started = True
        _loc = configuration["location"]
        if not isinstance(_loc, dict) or not _loc:
            _cli_exit(
                "location must be a non-empty mapping with lat, lon, and optional alt"
            )
        _allowed_loc_keys = {"lat", "lon", "alt"}
        _unknown_loc_keys = set(_loc.keys()) - _allowed_loc_keys
        if _unknown_loc_keys:
            _cli_exit(
                f"location contains unknown keys: {', '.join(sorted(_unknown_loc_keys))}. "
                f"Allowed: lat, lon, alt"
            )
        if "lat" not in _loc or "lon" not in _loc:
            _cli_exit("location requires both lat and lon")
        try:
            lat = float(_loc["lat"])
        except (ValueError, TypeError):
            _cli_exit(f"location.lat must be a number, got: {_loc['lat']!r}")
        try:
            lon = float(_loc["lon"])
        except (ValueError, TypeError):
            _cli_exit(f"location.lon must be a number, got: {_loc['lon']!r}")
        alt = 0
        if "alt" in _loc:
            try:
                alt = int(_loc["alt"])
            except (ValueError, TypeError):
                _cli_exit(f"location.alt must be an integer, got: {_loc['alt']!r}")
            _cli_print(f"Fixing altitude at {alt} meters")
        _cli_print(f"Fixing latitude at {lat} degrees")
        _cli_print(f"Fixing longitude at {lon} degrees")
        _cli_print("Setting device position")
        target_node.setFixedPosition(lat, lon, alt)
        time.sleep(CONFIG_APPLY_DELAY_SECONDS)

    if "canned_messages" in configuration:
        if not phase1_started:
            _cli_print(CONFIGURE_PHASE1_HEADER)
            phase1_started = True
        _cli_print(
            f"Setting canned message messages to {configuration['canned_messages']}",
        )
        target_node.set_canned_message(configuration["canned_messages"])
        time.sleep(CONFIG_APPLY_DELAY_SECONDS)

    if "ringtone" in configuration:
        if not phase1_started:
            _cli_print(CONFIGURE_PHASE1_HEADER)
            phase1_started = True
        _cli_print(f"Setting ringtone to {configuration['ringtone']}")
        target_node.set_ringtone(configuration["ringtone"])
        time.sleep(CONFIG_APPLY_DELAY_SECONDS)

    channel_url_key = "channel_url" if "channel_url" in configuration else "channelUrl"
    if channel_url_key in configuration:
        if not phase1_started:
            _cli_print(CONFIGURE_PHASE1_HEADER)
            phase1_started = True
        seturl_executed = _apply_configure_channel_url(
            target_node,
            configuration[channel_url_key],
            config_key=channel_url_key,
        )
        phase1_may_reconnect = seturl_executed

    if phase1_started:
        _cli_print("Phase 1 complete.")

    settings_transaction_started = False
    has_valid_config_section = bool(
        validated_config_sections or validated_module_config_sections
    )
    if seturl_executed and has_valid_config_section:
        if _is_local_destination(interface, args.dest):
            if not _post_seturl_stability_check(
                interface, timeout=SETURL_STABILITY_TIMEOUT_SECONDS
            ):
                _cli_exit(
                    "ERROR: channel_url applied, but transport did not stabilize "
                    "for additional configuration writes; aborting before Phase 2."
                )
        else:
            _cli_exit(
                "ERROR: Combining channel_url with additional configuration "
                "writes is not supported for remote nodes. Apply channel_url "
                "and configuration in separate operations."
            )
    if has_valid_config_section:
        _cli_print(
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
            applied = traverseConfig(
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
                _cli_exit(
                    f"Failed to apply config section {section!r} due to structural errors."
                )
            target_node.writeConfig(meshtastic.util.camel_to_snake(section))
            remaining_config_writes -= 1
            _pace_configure_write(remaining_config_writes)

    if validated_module_config_sections:
        moduleConfig = target_node.moduleConfig
        for section, section_values in validated_module_config_sections.items():
            failed_module_fields: list[str] = []
            applied = traverseConfig(
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
                _cli_exit(
                    f"Failed to apply module_config section {section!r} due to structural errors."
                )
            target_node.writeConfig(meshtastic.util.camel_to_snake(section))
            remaining_config_writes -= 1
            _pace_configure_write(remaining_config_writes)

    if settings_transaction_started:
        target_node.commitSettingsTransaction()
        time.sleep(CONFIG_COMMIT_SETTLE_SECONDS)
        _cli_print(
            "Configuration transaction committed. Device may reboot to apply changes."
        )

    if settings_transaction_started:
        _verify_channel_url = configuration.get("channel_url") or configuration.get(
            "channelUrl"
        )
        _verify_config_fields = validated_config_sections or None
        _verify_module_config_fields = validated_module_config_sections or None
        if _is_local_destination(interface, args.dest):
            _reconnect_result = _post_configure_reconnect_and_verify(
                interface,
                timeout=CONFIG_RECONNECT_WAIT_SECONDS,
                node_dest=args.dest,
                verify_channel_url=_verify_channel_url,
                verify_config_fields=_verify_config_fields,
                verify_module_config_fields=_verify_module_config_fields,
            )
            if _reconnect_result == _ConfigureReconnectResult.VERIFIED:
                _cli_print(
                    "Phase 3: Device reconnected and config reloaded. All settings verified."
                )
            elif _reconnect_result == _ConfigureReconnectResult.VERIFICATION_INCOMPLETE:
                _cli_print(
                    "Phase 3: Device reconnected and config reloaded. "
                    "Could not fully verify applied settings."
                )
            elif _reconnect_result == _ConfigureReconnectResult.CONFIG_RELOAD_FAILED:
                _cli_print(
                    "Phase 3: Device reconnected but config reload failed. "
                    "Settings may still be applying."
                )
            elif _reconnect_result == _ConfigureReconnectResult.RECONNECT_FAILED:
                _cli_print(
                    "Phase 3: Device did not reconnect within timeout. "
                    "Configuration may still be applying."
                )
        else:
            _cli_print(
                "Phase 3: Reboot/reconnect verification skipped for remote target. "
                "Local transport state does not confirm remote node reload status."
            )
    else:
        if phase1_may_reconnect:
            _cli_print(
                "Configuration applied. Channel URL updates may still trigger reconnect/reboot."
            )
        else:
            _cli_print("Configuration applied (no reboot expected).")

    return settings_transaction_started, (
        seturl_executed and _is_local_destination(interface, args.dest)
    )


def _validate_cli_show_fields(interface: MeshInterface, show_fields: list[str]) -> None:
    """Reject unavailable --show-fields values with a concrete choice list."""
    nodes_by_num = getattr(interface, "nodesByNum", None)
    observed_nodes = (
        list(nodes_by_num.values()) if isinstance(nodes_by_num, dict) else []
    )
    available = node_data.getKnownFieldPaths(observed_nodes)
    available_set = set(available)
    invalid = [field for field in show_fields if field not in available_set]
    if invalid:
        choices = textwrap.fill(
            ", ".join(available),
            width=100,
            initial_indent="  ",
            subsequent_indent="  ",
            break_long_words=False,
            break_on_hyphens=False,
        )
        _cli_exit(
            "Unknown --show-fields value(s): "
            f"{', '.join(invalid)}.\nAvailable fields:\n{choices}",
            1,
        )


def onConnected(interface: MeshInterface) -> None:
    """Execute CLI actions specified by parsed command-line arguments using the provided MeshInterface.

    Performs whichever device or network operations were requested via mt_config.args (for
    example: updating time or owner, configuring position or channels, sending
    text/telemetry/position messages, node administration, exporting/importing
    configuration, reboot/shutdown, starting tunnels or power-monitoring, and related
    read/write operations). Actions may modify remote devices, start long-running
    services, wait for acknowledgments, close the interface, or exit the process on
    fatal errors.

    Parameters
    ----------
    interface : MeshInterface
        An established mesh interface used to perform the requested device and network operations.

    Raises
    ------
    RuntimeError
        If `mt_config.args` is not set up before calling this function.
    """
    try:
        args = mt_config.args
        if args is None:
            raise RuntimeError("onConnected called without args being set up")

        # convenient place to store any keyword args we pass to getNode
        getNode_kwargs = {
            "requestChannelAttempts": args.channel_fetch_attempts,
            "timeout": args.timeout,
        }
        context = CliContext(
            interface=interface,
            args=args,
            get_node_kwargs=getNode_kwargs,
            outcome=ActionOutcome(wait_for_ack_nak=False),
        )
        outcome = context.outcome

        # do not print this line if we are exporting the config
        if not args.export_config:
            dev_path = getattr(interface, "devPath", "")
            if dev_path:
                tty_name = os.path.basename(dev_path)
                stable_path = getattr(interface, "_stable_path", None)
                if stable_path and stable_path != dev_path:
                    _cli_print(
                        f"Connected to radio on {tty_name} (stable: {stable_path})"
                    )
                else:
                    _cli_print(f"Connected to radio on {tty_name}")
            else:
                _cli_print("Connected to radio")

        device_hooks = cli_device_actions.DeviceActionHooks(
            cli_exit=_cli_exit,
            cli_print=_cli_print,
            set_pref=setPref,
            is_local_destination=_is_local_destination,
            send_local_factory_reset_and_wait=_send_local_factory_reset_and_wait,
            post_factory_reset_ready_probe=_post_factory_reset_ready_probe,
            handle_ota_update=_handle_ota_update,
            build_lockdown_auth=build_lockdown_auth,
            read_lockdown_passphrase_file=read_lockdown_passphrase_file,
            send_lockdown_auth=send_lockdown_auth,
            validate_lockdown_passphrase=validate_lockdown_passphrase,
        )
        cli_device_actions.handle_device_actions(context, device_hooks)
        if outcome.stop_processing:
            return

        if args.add_contact:
            outcome.close_now = True
            outcome.wait_for_ack_nak = True
            outcome.skip_ack_wait = True  # addContactURL() owns the remote ACK wait
            interface.getNode(args.dest, False, **getNode_kwargs).addContactURL(
                args.add_contact
            )

        if args.sendtext:
            outcome.close_now = True
            channelIndex = mt_config.channel_index or 0
            if checkChannel(interface, channelIndex):
                _cli_print(
                    f"Sending text message {args.sendtext} to {args.dest} on channelIndex:{channelIndex}"
                    f" {'using PRIVATE_APP port' if args.private else ''}"
                )
                interface.sendText(
                    args.sendtext,
                    args.dest,
                    wantAck=True,
                    channelIndex=channelIndex,
                    onResponse=interface.getNode(
                        args.dest, False, **getNode_kwargs
                    ).onAckNak,
                    portNum=(
                        portnums_pb2.PortNum.PRIVATE_APP
                        if args.private
                        else portnums_pb2.PortNum.TEXT_MESSAGE_APP
                    ),
                )
            else:
                _cli_exit(
                    f"Warning: {channelIndex} is not a valid channel. Channel must not be DISABLED."
                )

        if args.traceroute:
            loraConfig = interface.localNode.localConfig.lora
            hopLimit = loraConfig.hop_limit
            dest = str(args.traceroute)
            channelIndex = mt_config.channel_index or 0
            if checkChannel(interface, channelIndex):
                _cli_print(
                    f"Sending traceroute request to {dest} on channelIndex:{channelIndex} (this could take a while)"
                )
                interface.sendTraceRoute(dest, hopLimit, channelIndex=channelIndex)

        if args.request_telemetry:
            if args.dest == BROADCAST_ADDR:
                _cli_exit("Warning: Must use a destination node ID.")
            else:
                channelIndex = mt_config.channel_index or 0
                if checkChannel(interface, channelIndex):
                    telemMap = {
                        "device": "device_metrics",
                        "environment": "environment_metrics",
                        "air_quality": "air_quality_metrics",
                        "airquality": "air_quality_metrics",
                        "power": "power_metrics",
                        "localstats": "local_stats",
                        "local_stats": "local_stats",
                    }
                    telemType = telemMap.get(args.request_telemetry, "device_metrics")
                    _cli_print(
                        f"Sending {telemType} telemetry request to {args.dest} on channelIndex:{channelIndex} (this could take a while)"
                    )
                    interface.sendTelemetry(
                        destinationId=args.dest,
                        wantResponse=True,
                        channelIndex=channelIndex,
                        telemetryType=telemType,
                    )

        if args.request_position:
            if args.dest == BROADCAST_ADDR:
                _cli_exit("Warning: Must use a destination node ID.")
            else:
                channelIndex = mt_config.channel_index or 0
                if checkChannel(interface, channelIndex):
                    _cli_print(
                        f"Sending position request to {args.dest} on channelIndex:{channelIndex} (this could take a while)"
                    )
                    interface.sendPosition(
                        destinationId=args.dest,
                        wantResponse=True,
                        channelIndex=channelIndex,
                    )

        if args.gpio_wrb or args.gpio_rd or args.gpio_watch:
            if args.dest == BROADCAST_ADDR:
                _cli_exit("Warning: Must use a destination node ID.")
            else:
                rhc = remote_hardware.RemoteHardwareClient(interface)

                if args.gpio_wrb:
                    bitmask = 0
                    bitval = 0
                    for wrpair in args.gpio_wrb or []:
                        bitmask |= 1 << int(wrpair[0])
                        bitval |= int(wrpair[1]) << int(wrpair[0])
                    _cli_print(
                        f"Writing GPIO mask 0x{bitmask:x} with value 0x{bitval:x} to {args.dest}"
                    )
                    rhc.writeGPIOs(args.dest, bitmask, bitval)
                    outcome.close_now = True

                if args.gpio_rd:
                    bitmask = int(args.gpio_rd, 16)
                    _cli_print(f"Reading GPIO mask 0x{bitmask:x} from {args.dest}")
                    interface.mask = bitmask
                    rhc.readGPIOs(args.dest, bitmask, None)
                    # wait up to X seconds for a response
                    for _ in range(GPIO_READ_MAX_POLLS):
                        time.sleep(GPIO_READ_POLL_INTERVAL_SECONDS)
                        if interface.gotResponse:
                            break
                    logger.debug("end of gpio_rd")

                if args.gpio_watch:
                    bitmask = int(args.gpio_watch, 16)
                    _cli_print(
                        f"Watching GPIO mask 0x{bitmask:x} from {args.dest}. Press ctrl-c to exit"
                    )
                    while True:
                        rhc.watchGPIOs(args.dest, bitmask)
                        time.sleep(GPIO_WATCH_INTERVAL_SECONDS)

        # handle settings
        if args.set:
            outcome.close_now = True
            outcome.wait_for_ack_nak = True
            _handle_set_command(interface, args, getNode_kwargs)

        if args.configure:
            outcome.close_now = True
            outcome.wait_for_ack_nak = True
            _settings_transaction_started, _phase1_channel_url_applied = (
                _handle_configure_command(interface, args, getNode_kwargs)
            )
            if _settings_transaction_started or _phase1_channel_url_applied:
                outcome.wait_for_ack_nak = False
                outcome.skip_ack_wait = True

        if args.export_config:
            if args.dest != BROADCAST_ADDR:
                print("Exporting configuration of remote nodes is not supported.")
                return

            outcome.close_now = True
            config_txt = exportConfig(interface)

            if args.export_config == "-":
                # Output to stdout (preserves legacy use of `> file.yaml`)
                print(config_txt)
            else:
                try:
                    with open(args.export_config, "w", encoding="utf-8") as f:
                        f.write(config_txt)
                    _cli_print(f"Exported configuration to {args.export_config}")
                except Exception as e:
                    _cli_exit(f"ERROR: Failed to write config file: {e}")

        if args.ch_set_url:
            outcome.close_now = True
            interface.getNode(args.dest, **getNode_kwargs).setURL(
                args.ch_set_url, addOnly=False
            )

        # handle changing channels

        if args.ch_add_url:
            outcome.close_now = True
            interface.getNode(args.dest, **getNode_kwargs).setURL(
                args.ch_add_url, addOnly=True
            )

        if args.ch_add:
            ch_add_idx = mt_config.channel_index
            if ch_add_idx is not None:
                # Since we set the channel index after adding a channel, don't allow --ch-index
                _cli_exit(
                    "Warning: --ch-add chooses the next free channel index automatically; "
                    "remove --ch-index and retry. Use --ch-set, --ch-del, --ch-enable, "
                    "or --ch-disable when targeting a specific index."
                )
            outcome.close_now = True
            if len(args.ch_add) > 10:
                _cli_exit("Warning: Channel name must be shorter. Channel not added.")
            n = interface.getNode(args.dest, **getNode_kwargs)
            ch = n.getChannelByName(args.ch_add)
            if ch:
                _cli_exit(
                    f"Warning: This node already has a '{args.ch_add}' channel. No changes were made."
                )
            else:
                # get the first channel that is disabled (i.e., available)
                ch = n.getDisabledChannel()
                if not ch:
                    _cli_exit("Warning: No free channels were found")
                chs = channel_pb2.ChannelSettings()
                chs.psk = meshtastic.util.genPSK256()
                chs.name = args.ch_add
                ch.settings.CopyFrom(chs)
                ch.role = channel_pb2.Channel.Role.SECONDARY
                _cli_print("Writing modified channels to device")
                n.writeChannel(ch.index)
                _cli_print(
                    f"Setting newly-added channel's {ch.index} as '--ch-index' for further modifications"
                )
                mt_config.channel_index = ch.index

        if args.ch_del:
            outcome.close_now = True

            ch_del_idx = mt_config.channel_index
            if ch_del_idx is None:
                _cli_exit("Warning: Need to specify '--ch-index' for '--ch-del'.", 1)
            else:
                if ch_del_idx == 0:
                    _cli_exit("Warning: Cannot delete primary channel.", 1)
                else:
                    _cli_print(f"Deleting channel {ch_del_idx}")
                    interface.getNode(args.dest, **getNode_kwargs).deleteChannel(
                        ch_del_idx
                    )

        def _set_simple_config(
            modem_preset: config_pb2.Config.LoRaConfig.ModemPreset.ValueType,
        ) -> None:
            """Set and persist the LORA modem preset on the device's primary channel.

            If the configured channel is not the primary channel, the function exits
            with a warning and does not change device state. When applied, the modem
            preset is written into the node's local LORA configuration and
            persisted to the device.

            Parameters
            ----------
            modem_preset : int | EnumValue
                Modem preset identifier to apply (numeric index or enum value understood by firmware).
            """
            channelIndex = mt_config.channel_index
            if channelIndex is not None and channelIndex > 0:
                _cli_exit("Warning: Cannot set modem preset for non-primary channel", 1)
            # Overwrite modem_preset
            node = interface.getNode(args.dest, False, **getNode_kwargs)
            if len(node.localConfig.ListFields()) == 0:
                lora_descriptor = node.localConfig.DESCRIPTOR.fields_by_name.get("lora")
                if lora_descriptor is None:
                    _cli_exit(
                        "The active protobuf schema does not provide LoRa configuration",
                        1,
                    )
                node.requestConfig(lora_descriptor)
            node.localConfig.lora.modem_preset = modem_preset
            node.writeConfig("lora")

        # Resolve the final modem preset across historical shorthands (later
        # wins) and the schema-driven --ch-preset, then write exactly once.
        preset_val = None
        for _, destination, preset_name, _ in _MODEM_PRESET_SHORTHANDS:
            if getattr(args, destination, False):
                preset_val = config_pb2.Config.LoRaConfig.ModemPreset.Value(preset_name)

        generic_preset_name = getattr(args, "ch_preset", None)
        if generic_preset_name is not None:
            # Accept integer enum values from programmatic callers; CLI always
            # produces a string via _parse_modem_preset_name.
            if isinstance(generic_preset_name, int):
                # Validate by round-tripping through the name so bad integers
                # fail with a clear error instead of silently corrupting config.
                config_pb2.Config.LoRaConfig.ModemPreset.Name(
                    generic_preset_name  # type: ignore[arg-type]
                )
                preset_val = generic_preset_name  # type: ignore[assignment]
            else:
                preset_val = config_pb2.Config.LoRaConfig.ModemPreset.Value(
                    generic_preset_name
                )

        if preset_val is not None:
            _set_simple_config(preset_val)

        if args.ch_set or args.ch_enable or args.ch_disable:
            outcome.close_now = True

            _idx: int | None = mt_config.channel_index
            if _idx is None:
                _cli_exit("Warning: Need to specify '--ch-index'.", 1)
            # _idx is now narrowed to int due to NoReturn from _cli_exit
            node = interface.getNode(args.dest, **getNode_kwargs)
            channels = node.channels
            if channels is None:
                _cli_exit("Warning: Device channels are not available.", 1)
            # Reject negative indices explicitly (security fix)
            if _idx < 0:
                _cli_exit(
                    f"Warning: Channel index {_idx} is out of range.",
                    1,
                )
            # Try to access channel - IndexError catches out-of-range positive indices
            # TypeError handles case where channels is not indexable (e.g., mocked in tests)
            try:
                ch = channels[_idx]
            except (IndexError, TypeError):
                _cli_exit(
                    f"Warning: Channel index {_idx} is out of range.",
                    1,
                )

            enable: bool = True  # default to enable
            if args.ch_enable or args.ch_disable:
                _cli_print(
                    "Warning: --ch-enable and --ch-disable can produce noncontiguous channels, "
                    "which can cause errors in some clients. Whenever possible, use --ch-add and --ch-del instead."
                )
                if _idx == 0:
                    _cli_exit("Warning: Cannot enable/disable PRIMARY channel.")

                enable = True  # default to enable
                if args.ch_enable:
                    enable = True
                if args.ch_disable:
                    enable = False

            # Validate channel settings on a copy so a later invalid entry cannot
            # leave earlier values partially mutated in the local cache.
            pending_settings = type(ch.settings)()
            pending_settings.CopyFrom(ch.settings)
            channel_update_valid = True
            for pref in args.ch_set or []:
                if pref[0] == "psk":
                    try:
                        pending_settings.psk = meshtastic.util.fromPSK(pref[1])
                    except ValueError as exc:
                        _cli_exit(f"Invalid channel PSK: {exc}", 1)
                else:
                    if not _resolve_pref(pending_settings, pref[0]):
                        _print_channel_field_choices(pending_settings, pref[0])
                        channel_update_valid = False
                        continue
                    try:
                        with _fatal_preference_value_errors():
                            found = setPref(pending_settings, pref[0], pref[1])
                    except _PreferenceValueError as exc:
                        _cli_exit(str(exc), 1)
                    if not found:
                        _cli_exit(f"Invalid value for channel setting {pref[0]}.", 1)

                enable = True  # If we set any pref, assume the user wants to enable the channel

            if channel_update_valid:
                if args.ch_set:
                    ch.settings.CopyFrom(pending_settings)

                if enable:
                    ch.role = (
                        channel_pb2.Channel.Role.PRIMARY
                        if (_idx == 0)
                        else channel_pb2.Channel.Role.SECONDARY
                    )
                else:
                    ch.role = channel_pb2.Channel.Role.DISABLED

                _cli_print("Writing modified channels to device")
                node.writeChannel(_idx)
            else:
                _cli_exit(
                    "Warning: Unknown channel setting name. No changes were made.",
                    1,
                )

        if args.get_canned_message:
            outcome.close_now = True
            print("")
            messages = interface.getNode(
                args.dest, **getNode_kwargs
            ).get_canned_message()
            print(f"canned_plugin_message:{messages}")

        if args.get_ringtone:
            outcome.close_now = True
            print("")
            ringtone = interface.getNode(args.dest, **getNode_kwargs).get_ringtone()
            print(f"ringtone:{ringtone}")

        if args.show_region_presets:
            outcome.close_now = True
            if not _is_local_destination(interface, args.dest):
                print(
                    "Region/preset capabilities are available only from the local node."
                )
            elif not interface.regionPresets:
                print(
                    "This firmware did not provide usable region/preset compatibility metadata; "
                    "preset choices remain unconstrained."
                )
            else:
                for region, info in sorted(interface.regionPresets.items()):
                    try:
                        region_name = config_pb2.Config.LoRaConfig.RegionCode.Name(
                            cast(Any, region)
                        )
                    except ValueError:
                        region_name = f"REGION_{region}"
                    preset_names = []
                    for value in info.presets:
                        try:
                            preset_names.append(
                                config_pb2.Config.LoRaConfig.ModemPreset.Name(
                                    cast(Any, value)
                                )
                            )
                        except ValueError:
                            preset_names.append(f"PRESET_{value}")
                    try:
                        default_name = config_pb2.Config.LoRaConfig.ModemPreset.Name(
                            cast(Any, info.default_preset)
                        )
                    except ValueError:
                        default_name = f"PRESET_{info.default_preset}"
                    license_note = " licensed-only" if info.licensed_only else ""
                    print(
                        f"{region_name}: default={default_name}{license_note}; "
                        f"presets={','.join(preset_names)}"
                    )

        cli_device_actions.handle_lockdown_action(context, device_hooks)

        if args.info:
            print("")
            # If we aren't trying to talk to our local node, don't show it
            if args.dest == BROADCAST_ADDR:
                interface.showInfo()
                print("")
                interface.getNode(args.dest, **getNode_kwargs).showInfo()
                outcome.close_now = True
                print("")
                pypi_version = meshtastic.util.check_if_newer_version()
                if pypi_version:
                    print(
                        f"*** A newer version v{pypi_version} is available!"
                        f' Consider running "{INSTALL_UPGRADE_HINT}" ***\n'
                    )
            else:
                print("Showing info of remote node is not supported.")
                print(
                    "Use the '--get' command for a specific configuration (e.g. 'lora') instead."
                )

        if args.get:
            outcome.close_now = True
            node = interface.getNode(args.dest, False, **getNode_kwargs)
            found = False
            for pref in args.get:
                found = getPref(node, pref[0])

            if found:
                _cli_print("Completed getting preferences")

        if args.nodes:
            outcome.close_now = True
            if args.dest != BROADCAST_ADDR:
                print("Showing node list of a remote node is not supported.")
                return
            if args.show_fields:
                _validate_cli_show_fields(interface, args.show_fields)
            interface.showNodes(True, args.show_fields)

        if args.show_fields and not args.nodes:
            print("--show-fields can only be used with --nodes")
            return

        if args.qr or args.qr_all:
            outcome.close_now = True
            url = interface.getNode(args.dest, True, **getNode_kwargs).getURL(
                includeAll=args.qr_all
            )
            if args.qr_all:
                urldesc = "Complete URL (includes all channels)"
            else:
                urldesc = "Primary channel URL"
            print(f"{urldesc}: {url}")
            if pyqrcode is not None:
                qr = pyqrcode.create(url)
                print(qr.terminal())
            else:
                print("Install pyqrcode to view a QR code printed to terminal.")

        if args.contact_qr:
            outcome.close_now = True
            url = interface.localNode.getContactURL(
                args.contact_qr,
                should_ignore=args.contact_ignore,
                manually_verified=args.contact_verified,
            )
            print(f"Contact URL: {url}")
            if pyqrcode is not None:
                qr = pyqrcode.create(url)
                print(qr.terminal())
            else:
                print("Install pyqrcode to view a QR code printed to terminal.")

        log_set: Any = None
        # we need to keep a reference to the logset so it doesn't get GCed early

        if args.slog or args.power_stress:
            if have_powermon:
                global meter  # pylint: disable=global-variable-not-assigned
                if args.slog:
                    if LogSet is None:
                        _cli_exit(
                            "LogSet is required for --slog but not available. "
                            "The powermon module loaded incompletely."
                        )
                    log_set = LogSet(
                        interface, args.slog if args.slog != "default" else None, meter
                    )

                if args.power_stress:
                    if PowerStress is None:
                        _cli_exit(
                            "PowerStress is required for --power-stress but not available. "
                            "The powermon module loaded incompletely."
                        )
                    stress = PowerStress(interface)
                    stress.run()
                    outcome.close_now = True  # exit immediately after stress test
            else:
                _cli_exit(
                    "The powermon module could not be loaded. "
                    "You may need to run `poetry install --with powermon`. "
                    f"Import Error was: {powermon_exception}"
                )

        if args.listen:
            outcome.close_now = False

        have_tunnel = platform.system() == "Linux"
        if have_tunnel and args.tunnel:
            if args.dest != BROADCAST_ADDR:
                _cli_exit("A tunnel can only be created using the local node.", 1)
            # Even if others said we could close, stay open if the user asked for a tunnel
            outcome.close_now = False
            if interface.noProto:
                logger.warning("Not starting Tunnel - disabled by noProto")
            else:
                from . import tunnel  # pylint: disable=C0415

                if args.tunnel_net:
                    tunnel.Tunnel(interface, subnet=args.tunnel_net)
                else:
                    tunnel.Tunnel(interface)

        if not outcome.skip_ack_wait and (
            args.ack or (args.dest != BROADCAST_ADDR and outcome.wait_for_ack_nak)
        ):
            _cli_print(
                "Waiting for an acknowledgment from remote node (this could take a while)"
            )
            interface.getNode(args.dest, False, **getNode_kwargs).iface.waitForAckNak()

        if args.wait_to_disconnect:
            _cli_print(
                f"Waiting {args.wait_to_disconnect} seconds before disconnecting"
            )
            time.sleep(int(args.wait_to_disconnect))

        # if the user didn't ask for serial debugging output, we might want to exit after we've done our operation
        if (not args.seriallog) and outcome.close_now:
            try:
                interface.close()
            except Exception:
                logger.debug("Error during interface close", exc_info=True)

        # Close any structured logs after we've done all of our API operations
        if log_set:
            log_set.close()

    except Exception as ex:
        logger.exception("Unhandled exception in onConnected: %s", ex)
        _cli_exit(f"Aborting due to: {ex}", 1)


def printConfig(config: Any) -> None:
    """COMPAT_STABLE_SHIM: print config fields through the CLI config I/O runtime."""
    cli_config_io.print_config(config, camel_case=mt_config.camel_case)


def printAvailableConfigFields() -> None:
    """COMPAT_STABLE_SHIM: print config fields and aliases through the runtime."""
    cli_config_io.print_available_config_fields(
        camel_case=mt_config.camel_case,
        aliases=_PREFERENCE_FIELD_ALIASES,
        display_pref_name=_display_pref_name,
        local_config_factory=localonly_pb2.LocalConfig,
        module_config_factory=localonly_pb2.LocalModuleConfig,
    )


def onNode(node: Any) -> None:
    """Notify about a node database change by printing the changed node.

    Parameters
    ----------
    node : Any
        The node object or identifier that changed; printed to standard output.
    """
    _cli_print(f"Node changed: {node}")


def subscribe() -> None:
    """Register the default pub-sub handlers needed to receive incoming mesh messages.

    Subscribes the local receive callback to the "meshtastic.receive" topic so incoming packets
    are delivered to the onReceive handler. Other topic subscriptions are intentionally left
    commented out.
    """
    pub.subscribe(onReceive, "meshtastic.receive")
    # pub.subscribe(onConnection, "meshtastic.connection")

    # We now call onConnected from main
    # pub.subscribe(onConnected, "meshtastic.connection.established")

    # pub.subscribe(onNode, "meshtastic.node")


# COMPAT_STABLE_SHIM: preserve historical private helper imports.
_is_repeated_field = cli_config_io.is_repeated_field
_set_missing_flags_false = cli_config_io.set_missing_flags_false
_prefix_base64_bytes_fields = cli_config_io.prefix_base64_bytes_fields
_prefix_base64_key = cli_config_io.prefix_base64_key
CONFIG_TRUE_DEFAULTS = cli_config_io.CONFIG_TRUE_DEFAULTS
MODULE_TRUE_DEFAULTS = cli_config_io.MODULE_TRUE_DEFAULTS


def exportConfig(interface: MeshInterface) -> str:
    """COMPAT_STABLE_SHIM: export configuration through the CLI config I/O runtime."""
    return cli_config_io.export_config(
        interface,
        camel_case=mt_config.camel_case,
        message_to_dict=MessageToDict,
        prefix_base64_bytes_fields_fn=_prefix_base64_bytes_fields,
        set_missing_flags_false_fn=_set_missing_flags_false,
        config_true_defaults=CONFIG_TRUE_DEFAULTS,
        module_true_defaults=MODULE_TRUE_DEFAULTS,
    )


# COMPAT_STABLE_SHIM: snake_case alias for exportConfig
export_config = exportConfig


def _create_power_meter() -> None:
    """Initialize and configure the global power meter from parsed CLI arguments.

    Validates an optional voltage (must be between MIN_SUPPLY_VOLTAGE_V and MAX_SUPPLY_VOLTAGE_V), instantiates the
    selected power meter implementation based on power-related CLI flags, assigns it to the
    module-global `meter`, and, if a voltage is provided, sets the meter voltage and
    powers it on. When powering on, optionally waits for user confirmation or sleeps
    briefly depending on the CLI power-wait flag.

    Raises
    ------
    RuntimeError
        if mt_config.args is not initialized.
    """

    global meter  # pylint: disable=global-statement
    args = mt_config.args
    if args is None:
        raise RuntimeError(
            "mt_config.args must be initialized before calling _create_power_meter()"
        )

    if not have_powermon:
        _cli_exit(
            "The powermon module could not be loaded. "
            "You may need to run `poetry install --with powermon`. "
            f"Import Error was: {powermon_exception}"
        )
    if RidenPowerSupply is None or PPK2PowerSupply is None or SimPowerSupply is None:
        _cli_exit(
            "The powermon module loaded incompletely and required meter classes are "
            "unavailable."
        )

    # If the user specified a voltage, make sure it is valid AND a backend is selected
    v = 0.0
    if args.power_voltage is not None:
        if not any(
            (
                args.power_riden,
                args.power_ppk2_meter,
                args.power_ppk2_supply,
                args.power_sim,
            )
        ):
            _cli_exit(
                "--power-voltage requires one of --power-riden, --power-ppk2-meter, --power-ppk2-supply, or --power-sim"
            )
        v = float(args.power_voltage)
        if v < MIN_SUPPLY_VOLTAGE_V or v > MAX_SUPPLY_VOLTAGE_V:
            _cli_exit(
                f"Voltage must be between {MIN_SUPPLY_VOLTAGE_V}V and {MAX_SUPPLY_VOLTAGE_V}V"
            )
    if args.power_riden:
        meter = RidenPowerSupply(args.power_riden)
    elif args.power_ppk2_supply or args.power_ppk2_meter:
        meter = PPK2PowerSupply()
        if v <= 0:
            _cli_exit("Voltage must be specified for PPK2")
        meter.setVoltage(
            v
        )  # PPK2 requires setting voltage before selecting supply mode
        meter.setIsSupply(args.power_ppk2_supply)
    elif args.power_sim:
        meter = SimPowerSupply()

    if meter and v:
        logger.info("Setting power supply to %s volts", v)
        meter.setVoltage(v)
        meter.powerOn()
        if args.power_wait:
            input("Powered on, press enter to continue...")
        else:
            logger.info("Powered-on, waiting for device to boot")
            time.sleep(POWER_ON_BOOT_DELAY_SECONDS)


def _power_meter_requested(args: argparse.Namespace) -> bool:
    """Return whether parsed CLI arguments require powermon meter setup."""
    return any(
        (
            args.power_riden,
            args.power_ppk2_meter,
            args.power_ppk2_supply,
            args.power_sim,
            args.power_voltage is not None,
        )
    )


# COMPAT_STABLE_SHIM: legacy snake_case helper for callers importing this module.
create_power_meter = _create_power_meter


def _parse_host_port(host_str: str, default_port: int) -> tuple[str, int]:
    """Compatibility wrapper for shared host/port parsing in CLI code paths.

    Delegates parsing to `parseHostAndPort()` and preserves historical CLI
    behavior by converting validation failures into `_cli_exit(..., 1)`.

    Parameters
    ----------
    host_str : str
        Raw host string from CLI (`--host`).
    default_port : int
        Port to use when no explicit valid port is provided.

    Returns
    -------
    tuple[str, int]
        Parsed hostname/address and resolved TCP port.
    """
    try:
        return parseHostAndPort(
            host_str,
            default_port=default_port,
            env_var="--host",
        )
    except ValueError as exc:
        _cli_exit(f"Error: {exc}", 1)


def common() -> None:
    """Configure logging, validate CLI arguments, establish the selected transport.

    interface, invoke onConnected, and optionally enter the main event loop.

    Performs argument validation, initializes optional subsystems (power meter, serial logging),
    subscribes to message topics, opens the requested transport (BLE, TCP, or serial),
    calls onConnected with the established MeshInterface, and blocks until interrupted when
    a persistent session mode (listen, tunnel, noproto, or reply) is requested. On
    fatal errors the CLI exits via _cli_exit with an explanatory message.

    Raises
    ------
    RuntimeError
        If `mt_config.args` is not initialized before calling this function.
    RuntimeError
        If `mt_config.parser` is not initialized before calling this function.
    """
    logfile = None
    args = mt_config.args
    parser = mt_config.parser
    if args is None:
        raise RuntimeError("mt_config.args must be initialized before calling common()")
    if parser is None:
        raise RuntimeError(
            "mt_config.parser must be initialized before calling common()"
        )

    # Validate that --quiet is not used with --debug, --listen, or --debuglib
    if args.quiet and (args.debug or args.listen or args.debuglib):
        parser.error("--quiet cannot be used with --debug, --listen, or --debuglib")

    # Contact modifier flags require --contact-qr
    if (args.contact_verified or args.contact_ignore) and not args.contact_qr:
        parser.error("--contact-verified and --contact-ignore require --contact-qr")

    if args.quiet:
        log_level = logging.WARNING
    elif args.debug or args.listen:
        log_level = logging.DEBUG
    else:
        log_level = logging.INFO

    logging.basicConfig(
        level=log_level,
        format="%(levelname)s file:%(filename)s %(funcName)s line:%(lineno)s %(message)s",
    )

    if not (args.debug or args.listen or args.quiet) and args.debuglib:
        logging.getLogger("meshtastic").setLevel(logging.DEBUG)

    if len(sys.argv) == 1:
        parser.print_help(sys.stderr)
        _cli_exit("", 1)
    else:
        if args.support:
            supportInfo()
            _cli_exit("", 0)

        if args.list_fields:
            printAvailableConfigFields()
            return

        # Early validation for owner names before attempting device connection
        if args.set_owner is not None:
            stripped_long_name = args.set_owner.strip()
            if not stripped_long_name:
                _cli_exit(
                    "ERROR: Long Name cannot be empty or contain only whitespace characters"
                )

        if args.set_owner_short is not None:
            stripped_short_name = args.set_owner_short.strip()
            if not stripped_short_name:
                _cli_exit(
                    "ERROR: Short Name cannot be empty or contain only whitespace characters"
                )

        if args.set_ham is not None:
            stripped_ham_name = args.set_ham.strip()
            if not stripped_ham_name:
                _cli_exit(
                    "ERROR: Ham radio callsign cannot be empty or contain only whitespace characters"
                )

        # Fail fast before connecting if the OTA firmware file does not exist.
        if args.ota_update is not None and not os.path.isfile(args.ota_update):
            _cli_exit(f"Error: OTA firmware file not found: {args.ota_update}")

        if _power_meter_requested(args):
            _create_power_meter()

        if args.ch_index is not None:
            channelIndex = int(args.ch_index)
            mt_config.channel_index = channelIndex

        if not args.dest:
            args.dest = BROADCAST_ADDR

        if not args.seriallog:
            if args.noproto:
                args.seriallog = "stdout"
            else:
                args.seriallog = "none"  # assume no debug output in this case

        if args.deprecated is not None:
            logger.error(
                "This option has been deprecated, see help below for the correct replacement..."
            )
            parser.print_help(sys.stderr)
            _cli_exit("", 1)
        elif args.test:
            if meshtastic_test is None:
                _cli_exit(
                    "Test module could not be imported. Ensure you have the 'dotmap' module installed."
                )
            else:
                result = meshtastic_test.testAll()
                if not result:
                    _cli_exit("Warning: Test was not successful.")
                else:
                    _cli_exit("Test was a success.", 0)
        else:
            # Use ExitStack to guarantee cleanup on early exits or exceptions
            with contextlib.ExitStack() as stack:
                if args.seriallog == "stdout":
                    logfile = sys.stdout
                elif args.seriallog == "none":
                    args.seriallog = None
                    logger.debug("Not logging serial output")
                    logfile = None
                else:
                    logger.info("Logging serial output to %s", args.seriallog)
                    # Note: using line buffering.
                    logfile = stack.enter_context(
                        open(args.seriallog, "w+", buffering=1, encoding="utf8")
                    )
                    mt_config.logfile = logfile

                subscribe()
                if args.ble_scan:
                    logger.debug("BLE scan starting")
                    for x in BLEInterface.scan():
                        print(f"Found: name='{x.name}' address='{x.address}'")
                    _cli_exit("BLE scan finished", 0)

                client: MeshInterface | None = None
                if args.ble:
                    try:
                        client = stack.enter_context(
                            BLEInterface(
                                args.ble if args.ble != "any" else None,
                                debugOut=logfile,
                                noProto=args.noproto,
                                noNodes=args.no_nodes,
                                timeout=args.timeout,
                                auto_reconnect=args.ble_auto_reconnect,
                            )
                        )
                    except BLEInterface.BLEError as e:
                        _cli_exit(f"[BLE] {e}", 1)
                    except MeshInterface.MeshInterfaceError as e:
                        _cli_exit(f"[BLE] {e}", 1)
                elif args.host:
                    tcp_hostname: str = args.host
                    tcp_port: int = meshtastic.tcp_interface.DEFAULT_TCP_PORT
                    try:
                        tcp_hostname, tcp_port = _parse_host_port(
                            args.host,
                            meshtastic.tcp_interface.DEFAULT_TCP_PORT,
                        )
                        client = stack.enter_context(
                            meshtastic.tcp_interface.TCPInterface(
                                tcp_hostname,
                                portNumber=tcp_port,
                                debugOut=logfile,
                                noProto=args.noproto,
                                noNodes=args.no_nodes,
                                timeout=args.timeout,
                            )
                        )
                    except MeshInterface.MeshInterfaceError as ex:
                        _cli_exit(
                            f"Error connecting to {tcp_hostname}:{tcp_port}: {ex}", 1
                        )
                    except OSError as ex:
                        _cli_exit(
                            f"Error connecting to {tcp_hostname}:{tcp_port}: {ex}", 1
                        )
                else:
                    try:
                        client = stack.enter_context(
                            meshtastic.serial_interface.SerialInterface(
                                args.port,
                                debugOut=logfile,
                                noProto=args.noproto,
                                noNodes=args.no_nodes,
                                timeout=args.timeout,
                            )
                        )
                    except FileNotFoundError:
                        # Handle the case where the serial device is not found
                        message = "File Not Found Error:\n"
                        message += (
                            f"  The serial device at '{args.port}' was not found.\n"
                        )
                        message += "  Please check the following:\n"
                        message += "    1. Is the device connected properly?\n"
                        message += "    2. Is the correct serial port specified?\n"
                        message += "    3. Are the necessary drivers installed?\n"
                        message += "    4. Are you using a **power-only USB cable**? A power-only cable cannot transmit data.\n"
                        message += "       Ensure you are using a **data-capable USB cable**.\n"
                        _cli_exit(message, 1)
                    except PermissionError as ex:
                        try:
                            username = os.getlogin()
                        except OSError:
                            username = getpass.getuser()
                        message = "Permission Error:\n"
                        message += "  Need to add yourself to the 'dialout' group by running:\n"
                        message += f"     sudo usermod -a -G dialout {username}\n"
                        message += "  After running that command, log out and re-login for it to take effect.\n"
                        message += f"Error was: {ex}"
                        _cli_exit(message)
                    except MeshInterface.MeshInterfaceError as ex:
                        _cli_exit(f"[Serial] {ex}", 1)
                    except OSError as ex:
                        message = "OS Error:\n"
                        message += "  The serial device couldn't be opened, it might be in use by another process.\n"
                        message += "  Please close any applications or webpages that may be using the device and try again.\n"
                        message += f"\nOriginal error: {ex}"
                        _cli_exit(message)
                    if client is None or client.devPath is None:
                        logger.info(
                            "Serial device unavailable after initialization; falling back to localhost TCP interface."
                        )
                        try:
                            client = stack.enter_context(
                                meshtastic.tcp_interface.TCPInterface(
                                    "localhost",
                                    debugOut=logfile,
                                    noProto=args.noproto,
                                    noNodes=args.no_nodes,
                                    timeout=args.timeout,
                                )
                            )
                        except MeshInterface.MeshInterfaceError as ex:
                            _cli_exit(f"[TCP localhost] {ex}", 1)
                        except OSError as ex:
                            _cli_exit(
                                f"No Meshtastic device detected and no TCP listener on localhost: {ex}",
                                1,
                            )

                if client is None:
                    _cli_exit(
                        "Error: No interface was established. "
                        "Check connection parameters (BLE address, TCP host, or serial port).",
                        1,
                    )
                # We assume client is fully connected now
                onConnected(client)

                have_tunnel = platform.system() == "Linux"
                if (
                    args.noproto
                    or args.reply
                    or (have_tunnel and args.tunnel)
                    or args.listen
                ):  # loop until someone presses ctrlc
                    try:
                        while True:
                            if cli_runtime._listen_loop_poll_once(client):
                                continue
                    except KeyboardInterrupt:
                        logger.info("Exiting due to keyboard interrupt")

        # don't call exit, background threads might be running still
        # sys.exit(0)


# ---------------------------------------------------------------------------
# End of reconnect helpers
# ---------------------------------------------------------------------------


def initParser() -> None:
    """Parse CLI arguments and update the legacy global ``mt_config`` state."""
    parser = mt_config.parser
    if parser is None:
        raise RuntimeError(
            "mt_config.parser must be initialized before calling initParser()"
        )
    mt_config.args = parse_cli_args(
        parser,
        version=get_active_version(),
        argcomplete_module=argcomplete,
    )
    mt_config.parser = parser


def main() -> None:
    """
    Run the Meshtastic command-line entry point: initialize the argument parser, process CLI actions, and perform cleanup.

    This function initializes the global parser via initParser(), executes the shared CLI flow in
    common(), and closes the configured logfile if one was opened.
    """
    parser = argparse.ArgumentParser(
        add_help=False,
        epilog="If no connection arguments are specified, we search for a compatible serial device, "
        "and if none is found, then attempt a TCP connection to localhost.",
    )
    mt_config.parser = parser
    try:
        initParser()
        common()
    except KeyboardInterrupt:
        _cli_exit("Interrupted.", 130)
    except MeshInterface.MeshInterfaceError as exc:
        _cli_exit(f"ERROR: {exc}", 1)


def tunnelMain() -> None:
    """Start the Meshtastic CLI in IP-tunnel mode.

    Set tunnel mode on the parsed CLI arguments and run the shared CLI initialization and execution flow.

    Raises
    ------
    RuntimeError
        If CLI arguments could not be parsed or initialization failed.
    """
    parser = argparse.ArgumentParser(add_help=False)
    mt_config.parser = parser
    initParser()
    args = mt_config.args
    if args is None:
        raise RuntimeError("initParser() did not set mt_config.args")
    args.tunnel = True
    mt_config.args = args
    common()


if __name__ == "__main__":
    main()
