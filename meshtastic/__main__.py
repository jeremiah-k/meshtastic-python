"""Main Meshtastic."""

# We just hit the 1600 line limit for main.py, but I currently have a huge set of powermon/structured logging changes
# later we can have a separate changelist to refactor main.py into smaller files
# pylint: disable=R0917,C0302

import argparse
import binascii
import contextlib
import enum
import getpass
import importlib
import logging
import os
import platform
import sys
import threading
import time
from types import ModuleType
from typing import Any, NoReturn, Protocol, cast

import yaml
from google.protobuf.json_format import MessageToDict
from pubsub import pub

import meshtastic.ota
import meshtastic.serial_interface
import meshtastic.tcp_interface
import meshtastic.util
from meshtastic import BROADCAST_ADDR, LOCAL_ADDR, mt_config, remote_hardware
from meshtastic.cli.values import (
    is_local_destination as _is_local_destination,
    looks_like_integer_literal as _looks_like_integer_literal,  # noqa: F401 - legacy __main__ compatibility export
    parse_bitfield_value as _parse_bitfield_value,
    parse_integer_literal as _parse_integer_literal,  # noqa: F401 - legacy __main__ compatibility export
    parse_modem_preset_name as _parse_modem_preset_name,  # noqa: F401 - legacy __main__ compatibility export
)
# COMPAT_STABLE_SHIM: Preserve legacy imports from meshtastic.cli.parser.
from meshtastic.cli.parser import (
    _MODEM_PRESET_SHORTHANDS,
    addChannelConfigArgs,  # noqa: F401 - legacy __main__ compatibility export
    addConfigArgs,  # noqa: F401 - legacy __main__ compatibility export
    addConnectionArgs,  # noqa: F401 - legacy __main__ compatibility export
    addImportExportArgs,  # noqa: F401 - legacy __main__ compatibility export
    addLocalActionArgs,  # noqa: F401 - legacy __main__ compatibility export
    addPositionConfigArgs,  # noqa: F401 - legacy __main__ compatibility export
    addRemoteActionArgs,  # noqa: F401 - legacy __main__ compatibility export
    addRemoteAdminArgs,  # noqa: F401 - legacy __main__ compatibility export
    addSelectionArgs,  # noqa: F401 - legacy __main__ compatibility export
)
from meshtastic.configure_verify import (
    _verify_channel_url_against_state,
    _verify_requested_fields,
)
from meshtastic.host_port import parseHostAndPort
from meshtastic.interfaces.ble import BLEInterface
from meshtastic.mesh_interface import MeshInterface
from meshtastic.lockdown import (
    build_lockdown_auth,
    read_lockdown_passphrase_file,
    send_lockdown_auth,
    validate_lockdown_passphrase,
)
from meshtastic.protobuf import (
    admin_pb2,
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

FACTORY_RESET_READY_PROBE_TIMEOUT_SECONDS = 20.0
"""Timeout for post-reset reconnect probe inside factory-reset command."""

FACTORY_RESET_ACCEPTANCE_TIMEOUT_SECONDS = 20.0
"""Maximum time to observe a local reset ACK/NAK or reboot disconnect."""

FACTORY_RESET_ACCEPTANCE_POLL_SECONDS = 0.05
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
OTA_REBOOT_WAIT_SECONDS: float = 5.0
OTA_RETRY_DELAY_SECONDS: float = 2.0
OTA_MAX_RETRIES: int = 5

# Keep-alive sleep interval for main loop (effectively infinite wait)
MAIN_LOOP_IDLE_SLEEP_SECONDS = 1000
"""Sleep duration for the CLI main loop when listening on non-serial interfaces."""

SERIAL_RECONNECT_RETRY_SECONDS = 0.5
"""Polling interval for serial reconnect attempts after device reboot/disconnect."""

SERIAL_LISTEN_CONNECTED_SLEEP_SECONDS = 2.0
"""Sleep duration when a serial listen session is connected and healthy."""

SERIAL_RX_THREAD_JOIN_TIMEOUT_SECONDS = 5.0
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


def _cli_print(message: str) -> None:
    """Print a message to stdout unless --quiet is active.

    This helper gates non-essential informational output so that --quiet
    suppresses ordinary print() calls in addition to lowering the logging
    level.
    """
    args = mt_config.args
    if args and getattr(args, "quiet", False):
        return
    print(message)


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
    """Send a local factory reset and wait for ACK/NAK or reboot transport loss.

    Firmware 2.7 can execute the reset and schedule its reboot without forwarding
    the resulting routing ACK to PhoneAPI after the reset clears the channel
    table.  Firmware 2.8 normally forwards the ACK.  For this destructive local
    operation, a transport interruption observed *after* the request was queued
    is therefore also evidence that firmware accepted the command.

    Explicit routing errors and a deadline with neither signal remain hard
    failures.  The caller still reconnects and verifies the post-reset state.
    """
    reset_interface = reset_node.iface
    disconnect_observed = threading.Event()
    request_queued = threading.Event()

    def _on_connection_lost(interface: MeshInterface) -> None:
        if request_queued.is_set() and interface is reset_interface:
            disconnect_observed.set()

    acknowledgment = getattr(reset_interface, "_acknowledgment", None)
    reset_acknowledgment = getattr(acknowledgment, "reset", None)
    if callable(reset_acknowledgment):
        # A stale compatibility flag from an earlier command must never satisfy
        # this destructive request's acceptance wait.
        reset_acknowledgment()

    pub.subscribe(_on_connection_lost, "meshtastic.connection.lost")
    request: mesh_pb2.MeshPacket | None = None
    request_id: int | None = None
    try:
        request = reset_node.factoryReset(full=full)
        if request is None:
            return None

        raw_request_id = getattr(request, "id", None)
        if isinstance(raw_request_id, int) and not isinstance(raw_request_id, bool):
            request_id = raw_request_id if raw_request_id > 0 else None
        request_queued.set()

        # Snapshot the active transport only after sendData returned a concrete
        # packet.  A stale disconnect or reconnect that happened before the send
        # completed cannot prove this reset was accepted.
        missing_transport = object()
        socket_after_send = getattr(reset_interface, "socket", missing_transport)
        stream_after_send = getattr(reset_interface, "stream", missing_transport)

        acceptance_timeout = (
            FACTORY_RESET_ACCEPTANCE_TIMEOUT_SECONDS
            if timeout is None
            else float(timeout)
        )
        if acceptance_timeout <= 0:
            raise ValueError("factory reset acceptance timeout must be positive")

        wait_for_request_ack = getattr(reset_interface, "_wait_for_request_ack", None)
        raise_wait_error = getattr(
            reset_interface, "_raise_wait_error_if_present", None
        )
        scoped_wait_available = (
            request_id is not None
            and callable(wait_for_request_ack)
            and callable(raise_wait_error)
        )

        _cli_print("Waiting for factory reset acknowledgment or reboot disconnect")
        deadline = time.monotonic() + acceptance_timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break

            if scoped_wait_available and callable(wait_for_request_ack) and callable(raise_wait_error):
                completed = wait_for_request_ack(
                    "receivedNak",
                    request_id,
                    timeout_seconds=min(
                        FACTORY_RESET_ACCEPTANCE_POLL_SECONDS,
                        remaining,
                    ),
                )
                if completed:
                    # Request-scoped wait state distinguishes a successful ACK
                    # from an explicit routing error.  The legacy receivedNak
                    # attribute is only a completion latch and is not itself
                    # evidence of rejection.
                    raise_wait_error("receivedNak", request_id=request_id)
                    return request
            else:
                # Compatibility fallback for minimal/legacy interfaces without
                # request-scoped wait helpers.  Check actual ACK kinds before the
                # shared receivedNak completion flag because implicit ACK handling
                # can set both in older runtimes.
                if callable(raise_wait_error):
                    raise_wait_error("receivedNak", request_id=request_id)
                received_ack = bool(getattr(acknowledgment, "receivedAck", False))
                received_implicit_ack = bool(
                    getattr(acknowledgment, "receivedImplAck", False)
                )
                if received_ack or received_implicit_ack:
                    return request
                if bool(getattr(acknowledgment, "receivedNak", False)):
                    raise reset_interface.MeshInterfaceError(
                        "Factory reset request was rejected by the device"
                    )
                time.sleep(
                    min(
                        FACTORY_RESET_ACCEPTANCE_POLL_SECONDS,
                        remaining,
                    )
                )

            connected_event = getattr(reset_interface, "isConnected", None)
            connected = True
            is_set = getattr(connected_event, "is_set", None)
            if callable(is_set):
                connected = bool(is_set())

            current_socket = getattr(reset_interface, "socket", missing_transport)
            current_stream = getattr(reset_interface, "stream", missing_transport)
            socket_replaced = (
                socket_after_send is not missing_transport
                and current_socket is not socket_after_send
            )
            stream_replaced = (
                stream_after_send is not missing_transport
                and current_stream is not stream_after_send
            )
            if (
                disconnect_observed.is_set()
                or not connected
                or socket_replaced
                or stream_replaced
            ):
                # Check request-scoped errors one final time so an explicit NAK
                # wins over a nearly simultaneous transport loss.
                if callable(raise_wait_error):
                    raise_wait_error("receivedNak", request_id=request_id)
                logger.info(
                    "Device transport changed after local factory reset request; "
                    "treating reboot as command acceptance."
                )
                return request

        if callable(raise_wait_error):
            raise_wait_error("receivedNak", request_id=request_id)
        raise reset_interface.MeshInterfaceError(
            "Timed out waiting for a factory reset acknowledgment or reboot disconnect"
        )
    finally:
        retire_wait = getattr(reset_interface, "_retire_wait_request", None)
        if callable(retire_wait) and request_id is not None:
            retire_wait("receivedNak", request_id=request_id)
        if callable(reset_acknowledgment):
            reset_acknowledgment()
        try:
            pub.unsubscribe(_on_connection_lost, "meshtastic.connection.lost")
        except Exception:
            logger.debug(
                "Factory reset: failed to remove connection-loss observer.",
                exc_info=True,
            )

    return None


def _post_factory_reset_ready_probe(interface: MeshInterface) -> None:
    """Close, probe transport reconnect readiness, and close again for a clean next command."""
    if not isinstance(interface, meshtastic.serial_interface.SerialInterface):
        return

    logger.debug("Factory reset: closing serial interface to release port.")
    try:
        interface.close()
    except Exception:
        logger.debug(
            "Factory reset: initial serial close failed.",
            exc_info=True,
        )

    logger.debug(
        "Factory reset: probing reconnect readiness (timeout=%.1fs)...",
        FACTORY_RESET_READY_PROBE_TIMEOUT_SECONDS,
    )
    probe_start = time.monotonic()
    try:
        interface.connect()
        logger.debug(
            "Factory reset: reconnect probe succeeded in %.2fs.",
            time.monotonic() - probe_start,
        )
    except Exception:
        logger.warning(
            "Factory reset: reconnect probe did not complete before timeout.",
            exc_info=True,
        )
    finally:
        try:
            interface.close()
        except Exception:
            logger.debug(
                "Factory reset: final serial close failed.",
                exc_info=True,
            )


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


_SECRET_PREF_NAMES: frozenset[str] = frozenset(
    {
        "wifi_psk",
        "psk",
        "channel_psk",
        "private_key",
        "public_key",
        "admin_key",
        "secret",
        "api_key",
        "auth_token",
    }
)


class _NamedConfigType(Protocol):
    """Protocol for config section objects exposing a `name` attribute."""

    name: str


def _redact_pref_value(name: str, value: str) -> str:
    """Return a redacted placeholder for secret-bearing pref names."""
    return "<redacted>" if name in _SECRET_PREF_NAMES else value


def getPref(node: Any, comp_name: str, *, allow_secrets: bool = False) -> bool:
    """Retrieve and display a configuration preference or channel field for a node.

    Given a dot-separated preference name (section.field) or a single name (used for
    both section and field resolution), print any populated local values for that
    preference; if the field exists but is not populated locally, request the
    remote node's configuration so the value can be fetched. When a message/section
    name is provided (e.g., "channel" or "channel.label"), populated
    subfields are printed.

    Parameters
    ----------
    node : Any
        Node object exposing `localConfig` and `moduleConfig`.
    comp_name : str
        Dot-separated preference path (e.g., "channel.label" or "label").
        A single name is used for both section and field resolution.

    Returns
    -------
    bool
        `True` if the preference exists and local values were printed or a remote
        config request was issued, `False` if the preference was not found.
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
                secret_name=snake_name,
            )
        else:
            for field in config_values.ListFields():
                repeated = _is_repeated_field(field[0])
                _print_setting(
                    config_type,
                    field[0].name,
                    field[1],
                    repeated=repeated,
                    secret_name=field[0].name,
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
                except (ValueError, binascii.Error):
                    if failed_fields is not None:
                        failed_fields.append(pref_name)
                    return False
                if not ok:
                    if failed_fields is not None:
                        failed_fields.append(pref_name)
                    return False
        return True

    success = _traverse(config_root, config, interface_config)

    for section, fields in skipped_by_section.items():
        field_list = ", ".join(fields)
        logger.warning(
            "Skipping %d unknown field(s) from %s: %s",
            len(fields),
            section,
            field_list,
        )

    return success


def _resolve_pref(config: Any, comp_name: str) -> bool:
    """Check whether a dotted field path resolves to a valid protobuf field."""
    comp_name = _normalize_pref_name(comp_name)
    name = splitCompoundName(comp_name)
    snake_name = meshtastic.util.camel_to_snake(name[-1])
    objDesc = config.DESCRIPTOR
    config_type = objDesc.fields_by_name.get(name[0])
    if config_type and config_type.message_type is not None:
        config_part = config
        for name_part in name[1:-1]:
            part_snake_name = meshtastic.util.camel_to_snake(name_part)
            config_part = getattr(config_part, config_type.name)
            config_type = config_type.message_type.fields_by_name.get(part_snake_name)
        if config_type and config_type.message_type is not None:
            return config_type.message_type.fields_by_name.get(snake_name) is not None
    return config_type is not None


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

    objDesc = config.DESCRIPTOR
    config_part = config
    config_type = objDesc.fields_by_name.get(name[0])
    if config_type and config_type.message_type is not None:
        for name_part in name[1:-1]:
            part_snake_name = meshtastic.util.camel_to_snake((name_part))
            config_part = getattr(config, config_type.name)
            config_type = config_type.message_type.fields_by_name.get(part_snake_name)
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
            print(f"ERROR: {e}")
            return False
    elif isinstance(raw_val, str):
        val = meshtastic.util.fromStr(raw_val)
    else:
        val = raw_val
    logger.debug("val:%s", _redact_pref_value(snake_name, meshtastic.util.toStr(val)))

    if snake_name == "wifi_psk" and len(str(raw_val)) < 8:
        print("Warning: network.wifi_psk must be 8 or more characters.")
        return False

    enumType = pref.enum_type
    if enumType and isinstance(val, str):
        # We've failed so far to convert this string into an enum, try to find it by reflection
        ev = enumType.values_by_name.get(val)
        if ev:
            val = ev.number
        else:
            print(
                f"{name[0]}.{uni_name} does not have an enum called {val}, so you can not set it."
            )
            print("Choices in sorted order are:")
            names = []
            for f in enumType.values:
                # Note: We must use the value of the enum (regardless if camel or snake case)
                names.append(f"{f.name}")
            for temp_name in sorted(names):
                print(f"    {temp_name}")
            return False

    # repeating fields need to be handled with append, not setattr
    if not _is_repeated_field(pref):
        try:
            if config_type.message_type is not None:
                config_values = getattr(config_part, config_type.name)
                setattr(config_values, pref.name, val)
            else:
                setattr(config_part, snake_name, val)
        except TypeError:
            # The setter didn't like our arg type guess try again as a string
            config_values = getattr(config_part, config_type.name)
            setattr(config_values, pref.name, str(val))
    elif isinstance(val, list):
        new_vals = [meshtastic.util.fromStr(x) for x in val]
        config_values = getattr(config, config_type.name)
        getattr(config_values, pref.name)[:] = new_vals
    else:
        config_values = getattr(config, config_type.name)
        if val == 0:
            # clear values
            _cli_print(f"Clearing {pref.name} list")
            del getattr(config_values, pref.name)[:]
        else:
            display_value = _redact_pref_value(
                snake_name, meshtastic.util.toStr(raw_val)
            )
            _cli_print(f"Adding '{display_value}' to the {pref.name} list")
            cur_vals = [
                x for x in getattr(config_values, pref.name) if x not in [0, "", b""]
            ]
            if val not in cur_vals:
                cur_vals.append(val)
            getattr(config_values, pref.name)[:] = cur_vals
        return True

    prefix = f"{'.'.join(name[0:-1])}." if config_type.message_type is not None else ""
    display_value = _redact_pref_value(snake_name, meshtastic.util.toStr(raw_val))
    _cli_print(f"Set {prefix}{uni_name} to {display_value}")

    return True


def _handle_ota_update(
    interface: MeshInterface,
    args: Any,
    getNode_kwargs: dict[str, Any],
) -> None:
    if not isinstance(interface, meshtastic.tcp_interface.TCPInterface):
        _cli_exit(
            "Error: OTA update currently requires a TCP connection to the node (use --host)."
        )
    if not _is_local_destination(interface, args.dest):
        _cli_exit(
            "Error: OTA update only supports the directly connected local node; omit --dest or use --dest ^local."
        )
    ota_dest = LOCAL_ADDR

    try:
        ota = meshtastic.ota.ESP32WiFiOTA(args.ota_update, interface.hostname)
    except meshtastic.ota.OTAError as e:
        _cli_exit(f"OTA update failed: {e}")

    _cli_print(f"Triggering OTA update on {interface.hostname}...")
    interface.getNode(ota_dest, requestChannels=False, **getNode_kwargs).startOTA(
        mode=admin_pb2.OTAMode.OTA_WIFI, ota_file_hash=ota.hash_bytes()
    )

    _cli_print("Waiting for device to reboot into OTA mode...")
    time.sleep(OTA_REBOOT_WAIT_SECONDS)

    retries = OTA_MAX_RETRIES
    while retries > 0:
        try:
            ota.update()
            break

        except meshtastic.ota.OTATransportError as e:
            retries -= 1
            if retries == 0:
                _cli_exit(f"OTA update failed: {e}")

            time.sleep(OTA_RETRY_DELAY_SECONDS)
        except meshtastic.ota.OTAError as e:
            _cli_exit(f"OTA update failed: {e}")

    _cli_print("\nOTA update completed successfully!")


def _handle_set_command(
    interface: MeshInterface,
    args: Any,
    getNode_kwargs: dict[str, Any],
) -> None:
    node = interface.getNode(args.dest, False, **getNode_kwargs)

    last_pref: list[str] | None = None
    fields: set[str] = set()
    any_found = False
    for pref_item in args.set:
        if pref_item is None or len(pref_item) < 2:
            continue
        last_pref = pref_item
        found = False
        normalized_pref_name = _normalize_pref_name(pref_item[0])
        field = splitCompoundName(normalized_pref_name)[0]
        for config in [node.localConfig, node.moduleConfig]:
            config_type = config.DESCRIPTOR.fields_by_name.get(field)
            if config_type is not None:
                if len(config.ListFields()) == 0:
                    node.requestConfig(config_type)
                found = setPref(config, normalized_pref_name, pref_item[1])
                if found:
                    any_found = True
                    fields.add(field)
                    break

    if any_found:
        _cli_print("Writing modified preferences to device")
        if len(fields) > 1:
            _cli_print("Using a configuration transaction")
            node.beginSettingsTransaction()
        for field in fields:
            _cli_print(f"Writing {field} configuration to device")
            node.writeConfig(field)
        if len(fields) > 1:
            node.commitSettingsTransaction()
    elif last_pref is not None:
        print(
            f"{node.localConfig.__class__.__name__} and {node.moduleConfig.__class__.__name__} do not have an attribute {last_pref[0]}."
        )
        print("Choices are...")
        printConfig(node.localConfig)
        printConfig(node.moduleConfig)


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
        interface.getNode(args.dest, False, **getNode_kwargs).setOwner(
            long_name=owner_name
        )
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
        interface.getNode(args.dest, False, **getNode_kwargs).setOwner(
            long_name=None, short_name=owner_short_name
        )
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
        interface.getNode(args.dest, False, **getNode_kwargs).setOwner(
            long_name=None, short_name=owner_short_name
        )
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
        interface.getNode(args.dest, False, **getNode_kwargs).setFixedPosition(
            lat, lon, alt
        )
        time.sleep(CONFIG_APPLY_DELAY_SECONDS)

    if "canned_messages" in configuration:
        if not phase1_started:
            _cli_print(CONFIGURE_PHASE1_HEADER)
            phase1_started = True
        _cli_print(
            f"Setting canned message messages to {configuration['canned_messages']}",
        )
        interface.getNode(args.dest, **getNode_kwargs).set_canned_message(
            configuration["canned_messages"]
        )
        time.sleep(CONFIG_APPLY_DELAY_SECONDS)

    if "ringtone" in configuration:
        if not phase1_started:
            _cli_print(CONFIGURE_PHASE1_HEADER)
            phase1_started = True
        _cli_print(f"Setting ringtone to {configuration['ringtone']}")
        interface.getNode(args.dest, **getNode_kwargs).set_ringtone(
            configuration["ringtone"]
        )
        time.sleep(CONFIG_APPLY_DELAY_SECONDS)

    if "channel_url" in configuration:
        if not phase1_started:
            _cli_print(CONFIGURE_PHASE1_HEADER)
            phase1_started = True
        raw_channel_url = configuration["channel_url"]
        if not isinstance(raw_channel_url, str):
            _cli_exit("ERROR: channel_url must be a string.")
        requested_channel_url = raw_channel_url.strip()
        if not requested_channel_url:
            _cli_exit("ERROR: channel_url must not be blank.")
        target_node = interface.getNode(args.dest, **getNode_kwargs)
        if _channel_url_matches_current_device_state(
            target_node, requested_channel_url
        ):
            _cli_print("Channel url already matches device state; skipping apply.")
            logger.info("Skipping setURL apply because channel URL already matches.")
        else:
            phase1_may_reconnect = True
            seturl_executed = True
            _cli_print(f"Setting channel url to {requested_channel_url}")
            target_node.setURL(requested_channel_url)
            time.sleep(CONFIG_SETURL_DELAY_SECONDS)

    if "channelUrl" in configuration:
        if not phase1_started:
            _cli_print(CONFIGURE_PHASE1_HEADER)
            phase1_started = True
        raw_channel_url = configuration["channelUrl"]
        if not isinstance(raw_channel_url, str):
            _cli_exit("ERROR: channelUrl must be a string.")
        requested_channel_url = raw_channel_url.strip()
        if not requested_channel_url:
            _cli_exit("ERROR: channelUrl must not be blank.")
        target_node = interface.getNode(args.dest, **getNode_kwargs)
        if _channel_url_matches_current_device_state(
            target_node, requested_channel_url
        ):
            _cli_print("Channel url already matches device state; skipping apply.")
            logger.info("Skipping setURL apply because channel URL already matches.")
        else:
            phase1_may_reconnect = True
            seturl_executed = True
            _cli_print(f"Setting channel url to {requested_channel_url}")
            target_node.setURL(requested_channel_url)
            time.sleep(CONFIG_SETURL_DELAY_SECONDS)

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
        interface.getNode(args.dest, False, **getNode_kwargs).beginSettingsTransaction()
        settings_transaction_started = True

    if validated_config_sections:
        localConfig = interface.getNode(args.dest, **getNode_kwargs).localConfig
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
            interface.getNode(args.dest, **getNode_kwargs).writeConfig(
                meshtastic.util.camel_to_snake(section)
            )
        time.sleep(CONFIG_APPLY_DELAY_SECONDS)

    if validated_module_config_sections:
        moduleConfig = interface.getNode(args.dest, **getNode_kwargs).moduleConfig
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
            interface.getNode(args.dest, **getNode_kwargs).writeConfig(
                meshtastic.util.camel_to_snake(section)
            )
        time.sleep(CONFIG_APPLY_DELAY_SECONDS)

    if settings_transaction_started:
        interface.getNode(
            args.dest, False, **getNode_kwargs
        ).commitSettingsTransaction()
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
    closeNow = False  # Should we drop the connection after we finish?
    waitForAckNak = (
        False  # Should we wait for an acknowledgment if we send to a remote node?
    )
    skip_ack_wait = False  # OTA reboots the node before an ACK can be observed.
    try:
        args = mt_config.args
        if args is None:
            raise RuntimeError("onConnected called without args being set up")

        # convenient place to store any keyword args we pass to getNode
        getNode_kwargs = {
            "requestChannelAttempts": args.channel_fetch_attempts,
            "timeout": args.timeout,
        }

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

        if args.set_time is not None:
            interface.getNode(args.dest, False, **getNode_kwargs).setTime(args.set_time)

        if args.remove_position:
            closeNow = True
            waitForAckNak = True

            _cli_print("Removing fixed position and disabling fixed position setting")
            interface.getNode(args.dest, False, **getNode_kwargs).removeFixedPosition()
        elif args.setlat or args.setlon or args.setalt:
            closeNow = True
            waitForAckNak = True

            alt = 0
            lat = 0.0
            lon = 0.0
            if args.setalt:
                alt = int(args.setalt)
                _cli_print(f"Fixing altitude at {alt} meters")
            if args.setlat:
                try:
                    lat = int(args.setlat)
                except ValueError:
                    lat = float(args.setlat)
                _cli_print(f"Fixing latitude at {lat} degrees")
            if args.setlon:
                try:
                    lon = int(args.setlon)
                except ValueError:
                    lon = float(args.setlon)
                _cli_print(f"Fixing longitude at {lon} degrees")

            _cli_print("Setting device position and enabling fixed position setting")
            # can include lat/long/alt etc: latitude = 37.5, longitude = -122.1
            interface.getNode(args.dest, False, **getNode_kwargs).setFixedPosition(
                lat, lon, alt
            )

        if args.set_owner or args.set_owner_short or args.set_is_unmessageable:
            closeNow = True
            waitForAckNak = True

            long_name = args.set_owner.strip() if args.set_owner else None
            short_name = args.set_owner_short.strip() if args.set_owner_short else None

            if long_name is not None and not long_name:
                _cli_exit(
                    "ERROR: Long Name cannot be empty or contain only whitespace characters"
                )

            if short_name is not None and not short_name:
                _cli_exit(
                    "ERROR: Short Name cannot be empty or contain only whitespace characters"
                )

            if long_name and short_name:
                _cli_print(
                    f"Setting device owner to {long_name} and short name to {short_name}"
                )
            elif long_name:
                _cli_print(f"Setting device owner to {long_name}")
            elif short_name:
                _cli_print(f"Setting device owner short to {short_name}")

            unmessagable = None
            if args.set_is_unmessageable is not None:
                unmessagable = (
                    meshtastic.util.fromStr(args.set_is_unmessageable)
                    if isinstance(args.set_is_unmessageable, str)
                    else args.set_is_unmessageable
                )
                _cli_print(f"Setting device owner is_unmessageable to {unmessagable}")

            interface.getNode(args.dest, False, **getNode_kwargs).setOwner(
                long_name=long_name, short_name=short_name, is_unmessagable=unmessagable
            )

        if args.set_canned_message:
            closeNow = True
            waitForAckNak = True
            node = interface.getNode(args.dest, False, **getNode_kwargs)
            if node.module_available(mesh_pb2.CANNEDMSG_CONFIG):
                _cli_print(
                    f"Setting canned plugin message to {args.set_canned_message}"
                )
                node.set_canned_message(args.set_canned_message)
            else:
                logger.warning(
                    "Canned Message module is excluded by firmware; skipping set."
                )

        if args.set_ringtone:
            closeNow = True
            waitForAckNak = True
            node = interface.getNode(args.dest, False, **getNode_kwargs)
            if node.module_available(mesh_pb2.EXTNOTIF_CONFIG):
                _cli_print(f"Setting ringtone to {args.set_ringtone}")
                node.set_ringtone(args.set_ringtone)
            else:
                logger.warning(
                    "External Notification is excluded by firmware; skipping ringtone set."
                )

        if args.pos_fields:
            # If --pos-fields invoked with args, set position fields
            closeNow = True
            positionConfig = interface.getNode(
                args.dest, **getNode_kwargs
            ).localConfig.position
            allFields = 0

            try:
                for field in args.pos_fields:
                    v_field = positionConfig.PositionFlags.Value(field)
                    allFields |= v_field

            except ValueError:
                print("ERROR: supported position fields are:")
                print(positionConfig.PositionFlags.keys())
                print(
                    "If no fields are specified, will read and display current value."
                )

            else:
                _cli_print(f"Setting position fields to {allFields}")
                setPref(positionConfig, "position_flags", f"{allFields:d}")
                _cli_print("Writing modified preferences to device")
                interface.getNode(args.dest, **getNode_kwargs).writeConfig("position")

        elif args.pos_fields is not None:
            # If --pos-fields invoked without args, read and display current value
            closeNow = True
            positionConfig = interface.getNode(
                args.dest, **getNode_kwargs
            ).localConfig.position

            fieldNames = []
            for bit in positionConfig.PositionFlags.values():
                if positionConfig.position_flags & bit:
                    fieldNames.append(positionConfig.PositionFlags.Name(bit))
            print(" ".join(fieldNames))

        if args.set_ham:
            ham_id = args.set_ham.strip()
            if not ham_id:
                _cli_exit(
                    "ERROR: Ham radio callsign cannot be empty or contain only whitespace characters"
                )
            closeNow = True
            _cli_print(f"Setting Ham ID to {ham_id} and turning off encryption")
            interface.getNode(args.dest, **getNode_kwargs).setOwner(
                ham_id, is_licensed=True
            )
            # Must turn off encryption on primary channel
            interface.getNode(
                args.dest, **getNode_kwargs
            ).turnOffEncryptionOnPrimaryChannel()

        if args.reboot:
            closeNow = True
            waitForAckNak = True
            skip_ack_wait = True
            interface.getNode(args.dest, False, **getNode_kwargs).reboot()

        if args.reboot_ota:
            closeNow = True
            waitForAckNak = True
            skip_ack_wait = True
            interface.getNode(args.dest, False, **getNode_kwargs).rebootOTA()

        if args.ota_update:
            closeNow = True
            skip_ack_wait = True
            _handle_ota_update(interface, args, getNode_kwargs)
            return

        if args.enter_dfu:
            closeNow = True
            waitForAckNak = True
            skip_ack_wait = True
            interface.getNode(args.dest, False, **getNode_kwargs).enterDFUMode()

        if args.shutdown:
            closeNow = True
            waitForAckNak = True
            skip_ack_wait = True
            interface.getNode(args.dest, False, **getNode_kwargs).shutdown()

        if args.device_metadata:
            closeNow = True
            interface.getNode(args.dest, False, **getNode_kwargs).getMetadata()

        if args.begin_edit:
            closeNow = True
            interface.getNode(
                args.dest, False, **getNode_kwargs
            ).beginSettingsTransaction()

        if args.commit_edit:
            closeNow = True
            interface.getNode(
                args.dest, False, **getNode_kwargs
            ).commitSettingsTransaction()

        if args.factory_reset or args.factory_reset_device:
            closeNow = True
            waitForAckNak = True
            skip_ack_wait = True

            full = bool(args.factory_reset_device)
            reset_node = interface.getNode(args.dest, False, **getNode_kwargs)
            is_local_reset = _is_local_destination(interface, args.dest)
            if is_local_reset:
                _send_local_factory_reset_and_wait(
                    reset_node,
                    full=full,
                )
            else:
                # Remote Node.factoryReset() owns its ACK wait.
                reset_node.factoryReset(full=full)
            # Guard the isinstance check: SerialInterface may be a mock or not resolve in tests.
            _serial_interface_cls = getattr(
                meshtastic.serial_interface, "SerialInterface", None
            )
            if (
                full
                and _is_local_destination(interface, args.dest)
                and isinstance(_serial_interface_cls, type)
                and isinstance(interface, _serial_interface_cls)
            ):
                _post_factory_reset_ready_probe(interface)

        if args.remove_node:
            closeNow = True
            waitForAckNak = True
            interface.getNode(args.dest, False, **getNode_kwargs).removeNode(
                args.remove_node
            )

        if args.set_favorite_node:
            closeNow = True
            waitForAckNak = True
            interface.getNode(args.dest, False, **getNode_kwargs).setFavorite(
                args.set_favorite_node
            )

        if args.remove_favorite_node:
            closeNow = True
            waitForAckNak = True
            interface.getNode(args.dest, False, **getNode_kwargs).removeFavorite(
                args.remove_favorite_node
            )

        if args.set_ignored_node:
            closeNow = True
            waitForAckNak = True
            interface.getNode(args.dest, False, **getNode_kwargs).setIgnored(
                args.set_ignored_node
            )

        if args.remove_ignored_node:
            closeNow = True
            waitForAckNak = True
            interface.getNode(args.dest, False, **getNode_kwargs).removeIgnored(
                args.remove_ignored_node
            )

        if args.reset_nodedb:
            closeNow = True
            waitForAckNak = True
            interface.getNode(args.dest, False, **getNode_kwargs).resetNodeDb()

        if args.add_contact:
            closeNow = True
            waitForAckNak = True
            skip_ack_wait = True  # addContactURL() owns the remote ACK wait
            interface.getNode(args.dest, False, **getNode_kwargs).addContactURL(
                args.add_contact
            )

        if args.sendtext:
            closeNow = True
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
                    closeNow = True

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
            closeNow = True
            waitForAckNak = True
            _handle_set_command(interface, args, getNode_kwargs)

        if args.configure:
            closeNow = True
            waitForAckNak = True
            _settings_transaction_started, _phase1_channel_url_applied = (
                _handle_configure_command(interface, args, getNode_kwargs)
            )
            if _settings_transaction_started or _phase1_channel_url_applied:
                waitForAckNak = False
                skip_ack_wait = True

        if args.export_config:
            if args.dest != BROADCAST_ADDR:
                print("Exporting configuration of remote nodes is not supported.")
                return

            closeNow = True
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
            closeNow = True
            interface.getNode(args.dest, **getNode_kwargs).setURL(
                args.ch_set_url, addOnly=False
            )

        # handle changing channels

        if args.ch_add_url:
            closeNow = True
            interface.getNode(args.dest, **getNode_kwargs).setURL(
                args.ch_add_url, addOnly=True
            )

        if args.ch_add:
            ch_add_idx = mt_config.channel_index
            if ch_add_idx is not None:
                # Since we set the channel index after adding a channel, don't allow --ch-index
                _cli_exit(
                    "Warning: '--ch-add' and '--ch-index' are incompatible. Channel not added."
                )
            closeNow = True
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
            closeNow = True

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
            _set_simple_config(preset_val)  # type: ignore[arg-type]

        if args.ch_set or args.ch_enable or args.ch_disable:
            closeNow = True

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

            # Handle the channel settings
            for pref in args.ch_set or []:
                if pref[0] == "psk":
                    found = True
                    ch.settings.psk = meshtastic.util.fromPSK(pref[1])
                else:
                    found = setPref(ch.settings, pref[0], pref[1])
                if not found:
                    category_settings = ["module_settings"]
                    print(
                        f"{ch.settings.__class__.__name__} does not have an attribute {pref[0]}."
                    )
                    print("Choices are...")
                    for field in ch.settings.DESCRIPTOR.fields:
                        if field.name not in category_settings:
                            print(f"{field.name}")
                        else:
                            print(f"{field.name}:")
                            config = ch.settings.DESCRIPTOR.fields_by_name.get(
                                field.name
                            )
                            names = []
                            if config is not None and config.message_type is not None:
                                for sub_field in config.message_type.fields:
                                    tmp_name = f"{field.name}.{sub_field.name}"
                                    names.append(tmp_name)
                            for temp_name in sorted(names):
                                print(f"    {temp_name}")

                enable = True  # If we set any pref, assume the user wants to enable the channel

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

        if args.get_canned_message:
            closeNow = True
            print("")
            messages = interface.getNode(
                args.dest, **getNode_kwargs
            ).get_canned_message()
            print(f"canned_plugin_message:{messages}")

        if args.get_ringtone:
            closeNow = True
            print("")
            ringtone = interface.getNode(args.dest, **getNode_kwargs).get_ringtone()
            print(f"ringtone:{ringtone}")

        if args.show_region_presets:
            closeNow = True
            if not _is_local_destination(interface, args.dest):
                print("Region/preset capabilities are available only from the local node.")
            elif not interface.regionPresets:
                print(
                    "This firmware did not provide usable region/preset compatibility metadata; "
                    "preset choices remain unconstrained."
                )
            else:
                for region, info in sorted(interface.regionPresets.items()):
                    try:
                        region_name = config_pb2.Config.LoRaConfig.RegionCode.Name(cast(Any, region))
                    except ValueError:
                        region_name = f"REGION_{region}"
                    preset_names = []
                    for value in info.presets:
                        try:
                            preset_names.append(
                                config_pb2.Config.LoRaConfig.ModemPreset.Name(cast(Any, value))
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

        lockdown_action = next(
            (
                name
                for name, enabled in (
                    ("provision", args.lockdown_provision),
                    ("unlock", args.lockdown_unlock),
                    ("lock-now", args.lockdown_lock_now),
                    ("disable", args.lockdown_disable),
                )
                if enabled
            ),
            None,
        )
        if lockdown_action is not None:
            closeNow = True
            if not _is_local_destination(interface, args.dest):
                _cli_exit("Lockdown commands apply only to the directly connected local node.")
            if lockdown_action in {"provision", "lock-now", "disable"} and not args.lockdown_yes:
                confirmation = input(
                    f"Type 'yes' to confirm lockdown {lockdown_action}: "
                ).strip().casefold()
                if confirmation != "yes":
                    _cli_exit("Aborted.")
            try:
                passphrase = b""
                if lockdown_action != "lock-now":
                    if args.lockdown_passphrase_file:
                        passphrase = read_lockdown_passphrase_file(
                            args.lockdown_passphrase_file
                        )
                    elif args.lockdown_passphrase is not None:
                        if not args.insecure_lockdown_passphrase_on_command_line:
                            _cli_exit(
                                "--lockdown-passphrase requires "
                                "--insecure-lockdown-passphrase-on-command-line; "
                                "prefer an operator-only file or interactive entry."
                            )
                        passphrase = validate_lockdown_passphrase(
                            args.lockdown_passphrase.encode("utf-8")
                        )
                    else:
                        entered = getpass.getpass("Lockdown passphrase: ")
                        if lockdown_action == "provision":
                            confirmed = getpass.getpass(
                                "Lockdown passphrase (confirm): "
                            )
                            if entered != confirmed:
                                _cli_exit("Lockdown passphrases do not match.")
                        passphrase = validate_lockdown_passphrase(entered.encode("utf-8"))
                auth = build_lockdown_auth(
                    passphrase,
                    boots_remaining=args.lockdown_boots,
                    valid_until_epoch=args.lockdown_valid_until,
                    max_session_seconds=args.lockdown_max_session_seconds,
                    lock_now=lockdown_action == "lock-now",
                    disable=lockdown_action == "disable",
                )
            except (OSError, ValueError) as exc:
                _cli_exit(f"Invalid lockdown options: {exc}")
            try:
                status = send_lockdown_auth(
                    interface,
                    auth,
                    timeout=args.lockdown_wait,
                    allow_reboot_without_status=lockdown_action == "lock-now",
                )
            except (TimeoutError, ValueError, RuntimeError) as exc:
                _cli_exit(f"Lockdown command failed: {exc}")
            if status is None:
                print("Lockdown command accepted; device may already be rebooting.")
            else:
                try:
                    state_name = mesh_pb2.LockdownStatus.State.Name(status.state)
                except ValueError:
                    state_name = f"STATE_{status.state}"
                print(f"Lockdown status: {state_name}")
                if status.backoff_seconds:
                    print(f"Retry backoff: {status.backoff_seconds}s")
                if status.state == mesh_pb2.LockdownStatus.UNLOCK_FAILED:
                    _cli_exit("Lockdown authentication failed.")

        if args.info:
            print("")
            # If we aren't trying to talk to our local node, don't show it
            if args.dest == BROADCAST_ADDR:
                interface.showInfo()
                print("")
                interface.getNode(args.dest, **getNode_kwargs).showInfo()
                closeNow = True
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
            closeNow = True
            node = interface.getNode(args.dest, False, **getNode_kwargs)
            found = False
            for pref in args.get:
                found = getPref(node, pref[0])

            if found:
                _cli_print("Completed getting preferences")

        if args.nodes:
            closeNow = True
            if args.dest != BROADCAST_ADDR:
                print("Showing node list of a remote node is not supported.")
                return
            interface.showNodes(True, args.show_fields)

        if args.show_fields and not args.nodes:
            print("--show-fields can only be used with --nodes")
            return

        if args.qr or args.qr_all:
            closeNow = True
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
            closeNow = True
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
                    closeNow = True  # exit immediately after stress test
            else:
                _cli_exit(
                    "The powermon module could not be loaded. "
                    "You may need to run `poetry install --with powermon`. "
                    f"Import Error was: {powermon_exception}"
                )

        if args.listen:
            closeNow = False

        have_tunnel = platform.system() == "Linux"
        if have_tunnel and args.tunnel:
            if args.dest != BROADCAST_ADDR:
                _cli_exit("A tunnel can only be created using the local node.", 1)
            # Even if others said we could close, stay open if the user asked for a tunnel
            closeNow = False
            if interface.noProto:
                logger.warning("Not starting Tunnel - disabled by noProto")
            else:
                from . import tunnel  # pylint: disable=C0415

                if args.tunnel_net:
                    tunnel.Tunnel(interface, subnet=args.tunnel_net)
                else:
                    tunnel.Tunnel(interface)

        if not skip_ack_wait and (
            args.ack or (args.dest != BROADCAST_ADDR and waitForAckNak)
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
        if (not args.seriallog) and closeNow:
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
    """Print the top-level configuration sections and their fields.

    Skips the "version" section. For each other top-level section, prints the section name
    followed by its fields in the form "section.field"; field names are converted to
    camelCase when mt_config.camel_case is true.

    Parameters
    ----------
    config : Any
        A protobuf-like configuration message exposing a DESCRIPTOR with top-level fields.
    """
    objDesc = config.DESCRIPTOR
    for config_section in objDesc.fields:
        if config_section.name != "version":
            section_field = objDesc.fields_by_name.get(config_section.name)
            if section_field is None or section_field.message_type is None:
                continue
            print(f"{config_section.name}:")
            names = []
            for field in section_field.message_type.fields:
                tmp_name = f"{config_section.name}.{field.name}"
                if mt_config.camel_case:
                    tmp_name = meshtastic.util.snake_to_camel(tmp_name)
                names.append(tmp_name)
            for temp_name in sorted(names):
                print(f"    {temp_name}")


def printAvailableConfigFields() -> None:
    """Print all current config fields from protobuf descriptors plus aliases."""
    print("Local config fields:")
    printConfig(localonly_pb2.LocalConfig())
    print("")
    print("Module config fields:")
    printConfig(localonly_pb2.LocalModuleConfig())
    if _PREFERENCE_FIELD_ALIASES:
        print("")
        print("Compatibility aliases:")
        for alias_name, canonical_name in sorted(_PREFERENCE_FIELD_ALIASES.items()):
            print(
                f"    {_display_pref_name(alias_name)} -> {_display_pref_name(canonical_name)}"
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


def _is_repeated_field(field_desc: Any) -> bool:
    """Return True if the protobuf field is repeated.

    Newer protobuf runtimes expose a boolean ``is_repeated`` property, while
    older generated descriptors require comparing ``label`` to
    ``LABEL_REPEATED``.
    """
    is_repeated = getattr(field_desc, "is_repeated", None)
    if isinstance(is_repeated, bool):
        return is_repeated

    label = getattr(field_desc, "label", None)
    label_repeated = getattr(field_desc, "LABEL_REPEATED", None)
    return label is not None and label == label_repeated


def _set_missing_flags_false(
    config_dict: dict[str, Any], true_defaults: set[tuple[str, ...]]
) -> None:
    """Ensure specific boolean flags exist in a nested configuration dictionary by creating any.

    missing path components and setting missing final keys to False.

    Parameters
    ----------
    config_dict : dict[str, Any]
        Nested configuration dictionary to modify in place.
    true_defaults : set[tuple[str, ...]]
        Set of key paths (tuples of keys) whose final key should exist;
        if a path is missing, intermediate dictionaries are created and the final key is added with value False.
    """
    for path in true_defaults:
        d = config_dict
        for key in path[:-1]:
            if key not in d or not isinstance(d[key], dict):
                d[key] = {}
            d = d[key]
        if path[-1] not in d:
            d[path[-1]] = False


def _prefix_base64_key(
    security: dict[str, Any], normalized_key_map: dict[str, str], camel_name: str
) -> None:
    """Prefix a security key value with 'base64:' if it is a string or list of strings.

    This helper normalizes base64-encoded security keys (privateKey, publicKey, adminKey)
    so they are clearly marked as base64-encoded in exported configuration.

    Parameters
    ----------
    security : dict[str, Any]
        The security configuration dictionary to modify in-place.
    normalized_key_map : dict[str, str]
        Mapping from canonical camelCase names to actual keys in security dict.
    camel_name : str
        The canonical camelCase name of the key to process (e.g., "privateKey").
    """
    key = normalized_key_map.get(camel_name)
    if not key:
        return
    val = security.get(key)
    if isinstance(val, str):
        if not val.startswith("base64:"):
            security[key] = "base64:" + val
    elif isinstance(val, list):
        security[key] = [
            ("base64:" + v if isinstance(v, str) and not v.startswith("base64:") else v)
            for v in val
        ]


# Boolean flags that default to True in firmware but may be absent from
# MessageToDict output; set missing values to False to preserve round-trip intent.
CONFIG_TRUE_DEFAULTS: set[tuple[str, ...]] = {
    ("bluetooth", "enabled"),
    ("lora", "sx126xRxBoostedGain"),
    ("lora", "txEnabled"),
    ("lora", "usePreset"),
    ("position", "positionBroadcastSmartEnabled"),
    ("security", "serialEnabled"),
}

MODULE_TRUE_DEFAULTS: set[tuple[str, ...]] = {
    ("mqtt", "encryptionEnabled"),
}


def exportConfig(interface: MeshInterface) -> str:
    """Export local node and module configuration as a YAML-formatted Meshtastic configuration string.

    Produces a YAML document containing selected top-level metadata (owner, owner_short, channel
    URL, canned messages, ringtone, and location) plus `config` and `module_config` sections
    derived from node's protobuf-backed settings. Key casing in the exported `config` and
    `module_config` follows mt_config.camel_case. Certain boolean flags are explicitly set to
    false if missing, and security key fields are normalized to include a "base64:" prefix when
    appropriate.

    Parameters
    ----------
    interface : MeshInterface
        The connected interface whose local node and module configuration will be exported.

    Returns
    -------
    str
        A YAML string (prefixed with a header comment) representing exported configuration.
    """
    configObj: dict[str, Any] = {}

    owner = interface.getLongName()
    owner_short = interface.getShortName()
    channel_url = interface.localNode.getURL()
    myinfo = interface.getMyNodeInfo()
    canned_messages = interface.getCannedMessage()
    ringtone = interface.getRingtone()
    pos = myinfo.get("position") if myinfo else None
    lat = None
    lon = None
    alt = None
    if pos:
        lat = pos.get("latitude")
        lon = pos.get("longitude")
        alt = pos.get("altitude")

    if owner:
        configObj["owner"] = owner
    if owner_short:
        configObj["owner_short"] = owner_short
    if channel_url:
        if mt_config.camel_case:
            configObj["channelUrl"] = channel_url
        else:
            configObj["channel_url"] = channel_url
    if canned_messages:
        configObj["canned_messages"] = canned_messages
    if ringtone:
        configObj["ringtone"] = ringtone
    # lat and lon don't make much sense without the other (so fill with 0s), and alt isn't meaningful without both
    if lat is not None or lon is not None:
        configObj["location"] = {
            "lat": lat if lat is not None else 0.0,
            "lon": lon if lon is not None else 0.0,
        }
        if alt is not None:
            configObj["location"]["alt"] = alt

    config = MessageToDict(interface.localNode.localConfig)
    if config:
        # Ensure explicit false values are present before key conversion.
        _set_missing_flags_false(config, CONFIG_TRUE_DEFAULTS)

        # Convert inner keys to correct snake/camelCase.
        prefs = {}
        for pref, value in config.items():
            pref_key = (
                meshtastic.util.snake_to_camel(pref)
                if mt_config.camel_case
                else meshtastic.util.camel_to_snake(pref)
            )
            prefs[pref_key] = value
            # mark base64 encoded fields as such
            if pref == "security" and isinstance(prefs[pref_key], dict):
                security = prefs[pref_key]
                # Normalize keys to canonical camelCase for reliable lookup,
                # since MessageToDict may produce inconsistent casing
                normalized_key_map = {
                    meshtastic.util.snake_to_camel(
                        meshtastic.util.camel_to_snake(key)
                    ): key
                    for key in security
                    if isinstance(key, str)
                }

                _prefix_base64_key(security, normalized_key_map, "privateKey")
                _prefix_base64_key(security, normalized_key_map, "publicKey")
                _prefix_base64_key(security, normalized_key_map, "adminKey")
        configObj["config"] = prefs

    module_config = MessageToDict(interface.localNode.moduleConfig)
    if module_config:
        # Ensure explicit false values are present before key conversion.
        _set_missing_flags_false(module_config, MODULE_TRUE_DEFAULTS)

        # Convert inner keys to correct snake/camelCase.
        prefs = {}
        for pref, value in module_config.items():
            pref_key = (
                meshtastic.util.snake_to_camel(pref)
                if mt_config.camel_case
                else meshtastic.util.camel_to_snake(pref)
            )
            prefs[pref_key] = value
        configObj["module_config"] = prefs

    config_txt = "# start of Meshtastic configure yaml\n"
    # was used as a string here and a Dictionary above
    config_txt += yaml.dump(configObj)
    return config_txt


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
    if RidenPowerSupply is None or PPK2PowerSupply is None or SimPowerSupply is None:
        _cli_exit(
            "The powermon module loaded incompletely and required meter classes are "
            "unavailable."
        )

    # If the user specified a voltage, make sure it is valid
    v = 0.0
    if args.power_voltage:
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
                            if _listen_loop_poll_once(client):
                                continue
                    except KeyboardInterrupt:
                        logger.info("Exiting due to keyboard interrupt")

        # don't call exit, background threads might be running still
        # sys.exit(0)




















def _is_serial_reconnect_client(client: MeshInterface) -> bool:
    """Return True if *client* is a real SerialInterface that supports reconnect.

    Uses a guarded isinstance check that is safe even when tests patch
    ``SerialInterface`` with a MagicMock. Does **not** rely on
    ``devPath`` because auto-detected devices intentionally reset
    ``devPath = None`` during reconnect so port detection can re-run.
    """
    serial_cls = getattr(meshtastic.serial_interface, "SerialInterface", None)
    return isinstance(serial_cls, type) and isinstance(client, serial_cls)


def _serial_transport_is_live(client: MeshInterface) -> bool:
    """Check if a serial interface has a live transport (stream open, reader alive).

    In noProto mode, ``isConnected`` is never set because the protocol handshake
    is skipped. This helper checks transport-level liveness instead: the stream
    exists and is open, and the reader thread is alive.
    """
    stream = getattr(client, "stream", None)
    if stream is None or not getattr(stream, "is_open", True):
        return False
    rx_thread = getattr(client, "_rxThread", None)
    if rx_thread is None or not rx_thread.is_alive():
        return False
    return True


def _serial_should_reconnect(client: MeshInterface) -> bool:
    """Return True if a serial client needs reconnect attention.

    For protocol mode: reconnect when ``isConnected`` is cleared.
    For noProto mode: reconnect when the transport itself is dead.
    """
    if getattr(client, "_wantExit", False):
        return False
    if getattr(client, "noProto", False):
        return not _serial_transport_is_live(client)
    return not client.isConnected.is_set()


def _poll_serial_reconnect(client: MeshInterface) -> None:
    """Attempt one round of serial reconnection, sleeping on failure.

    Catches connection-related errors and retryable MeshInterfaceError.
    Waits for the old reader thread to exit before reconnecting.
    """
    logger.debug("Serial reconnect poll: transport is down, attempting connect...")

    # Wait for the old reader thread to exit before reconnecting
    rx_thread = getattr(client, "_rxThread", None)
    if rx_thread is not None and rx_thread.is_alive():
        rx_thread.join(timeout=SERIAL_RX_THREAD_JOIN_TIMEOUT_SECONDS)
        if rx_thread.is_alive():
            logger.warning(
                "Reader thread is still alive after join timeout; delaying reconnect."
            )
            time.sleep(SERIAL_RECONNECT_RETRY_SECONDS)
            return

    try:
        client.connect()  # type: ignore[attr-defined]
        if client.isConnected.is_set() or _serial_transport_is_live(client):
            logger.info("Serial reconnected.")
        else:
            logger.debug("Reconnect returned but interface is not live yet.")
            time.sleep(SERIAL_RECONNECT_RETRY_SECONDS)
    except Exception as exc:
        # Route all exceptions through the serial retry classifier when available.
        # Unconditionally retry OS/connection errors; conditionally retry
        # MeshInterfaceError based on _is_retryable_connect_error().
        if isinstance(exc, (ConnectionError, OSError, TimeoutError)):
            retryable = True
        elif isinstance(exc, MeshInterface.MeshInterfaceError):
            retryable = bool(
                hasattr(client, "_is_retryable_connect_error")
                and client._is_retryable_connect_error(exc)
            )
        else:
            retryable = False

        if retryable:
            logger.debug("Reconnect attempt failed (retryable): %s", exc)
            time.sleep(SERIAL_RECONNECT_RETRY_SECONDS)
        else:
            raise


def _listen_loop_poll_once(client: MeshInterface) -> bool:
    """Execute one iteration of the persistent listen loop.

    Returns True if the caller should ``continue`` immediately (reconnect
    was attempted, timing is self-contained), False if the caller should
    proceed to the next iteration normally (sleep already handled).
    """
    if _is_serial_reconnect_client(client):
        if _serial_should_reconnect(client):
            _poll_serial_reconnect(client)
            return True  # reconnect timing is self-contained, skip main sleep
        time.sleep(SERIAL_LISTEN_CONNECTED_SLEEP_SECONDS)
    else:
        time.sleep(MAIN_LOOP_IDLE_SLEEP_SECONDS)
    return False


# ---------------------------------------------------------------------------
# End of reconnect helpers
# ---------------------------------------------------------------------------


def initParser() -> None:
    """Configure the global CLI ArgumentParser by registering all Meshtastic command.

    groups, enable shell autocompletion if available, parse command-line
    arguments, and store the parser and parsed arguments on mt_config.

    Raises
    ------
    RuntimeError
        if mt_config.parser is not initialized before calling.
    """
    parser = mt_config.parser
    if parser is None:
        raise RuntimeError(
            "mt_config.parser must be initialized before calling initParser()"
        )

    # The "Help" group includes the help option and other informational stuff about the CLI itself
    outerHelpGroup = parser.add_argument_group("Help")
    helpGroup = outerHelpGroup.add_mutually_exclusive_group()
    helpGroup.add_argument(
        "-h", "--help", action="help", help="show this help message and exit"
    )

    the_version = get_active_version()
    helpGroup.add_argument("--version", action="version", version=f"{the_version}")

    helpGroup.add_argument(
        "--support",
        action="store_true",
        help="Show support info (useful when troubleshooting an issue)",
    )

    # Connection arguments to indicate a device to connect to
    parser = addConnectionArgs(parser)

    # Selection arguments to denote nodes and channels to use
    parser = addSelectionArgs(parser)

    # Arguments concerning viewing and setting configuration
    parser = addImportExportArgs(parser)
    parser = addConfigArgs(parser)
    parser = addPositionConfigArgs(parser)
    parser = addChannelConfigArgs(parser)

    # Arguments for sending or requesting things from the local device
    parser = addLocalActionArgs(parser)

    # Arguments for sending or requesting things from the mesh
    parser = addRemoteActionArgs(parser)
    parser = addRemoteAdminArgs(parser)

    # All the rest of the arguments
    group = parser.add_argument_group("Miscellaneous arguments")

    group.add_argument(
        "--seriallog",
        help="Log device serial output to either 'none' or a filename to append to.  Defaults to '%(const)s' if no filename specified.",
        nargs="?",
        const="stdout",
        default=None,
        metavar="LOG_DESTINATION",
    )

    group.add_argument(
        "--ack",
        help="Use in combination with compatible actions (e.g. --sendtext) to wait for an acknowledgment.",
        action="store_true",
    )

    group.add_argument(
        "--timeout",
        help="How long to wait for replies. Default %(default)ss.",
        default=300.0,
        type=float,
        metavar="SECONDS",
    )

    group.add_argument(
        "--no-nodes",
        help="Request that the node not send node info to the client. "
        "Will break things that depend on the nodedb, but will speed up startup. Requires 2.3.11+ firmware.",
        action="store_true",
    )

    group.add_argument(
        "--debug",
        help="Show detailed debug log messages (connection diagnostics, config streaming, retries)",
        action="store_true",
    )

    group.add_argument(
        "--debuglib",
        help="Show debug log messages for the meshtastic library only (not dependencies)",
        action="store_true",
    )

    group.add_argument(
        "--quiet",
        help="Suppress non-essential output; show only warnings and errors",
        action="store_true",
    )

    group.add_argument(
        "--test",
        help="Run stress test against all connected Meshtastic devices",
        action="store_true",
    )

    group.add_argument(
        "--wait-to-disconnect",
        help="How many seconds to wait before disconnecting from the device.",
        const="5",
        nargs="?",
        action="store",
        metavar="SECONDS",
    )

    group.add_argument(
        "--noproto",
        help="Don't start the API, just function as a dumb serial terminal.",
        action="store_true",
    )

    group.add_argument(
        "--listen",
        help="Just stay open and listen to the protobuf stream. Enables debug logging.",
        action="store_true",
    )

    group.add_argument(
        "--no-time",
        help="Deprecated. Retained for backwards compatibility in scripts, but is a no-op.",
        action="store_true",
    )

    power_group = parser.add_argument_group(
        "Power Testing", "Options for power testing/logging."
    )

    power_supply_group = power_group.add_mutually_exclusive_group()

    power_supply_group.add_argument(
        "--power-riden",
        help="Talk to a Riden power-supply. You must specify the device path, i.e. /dev/ttyUSBxxx",
    )

    power_supply_group.add_argument(
        "--power-ppk2-meter",
        help="Talk to a Nordic Power Profiler Kit 2 (in meter mode)",
        action="store_true",
    )

    power_supply_group.add_argument(
        "--power-ppk2-supply",
        help="Talk to a Nordic Power Profiler Kit 2 (in supply mode)",
        action="store_true",
    )

    power_supply_group.add_argument(
        "--power-sim",
        help="Use a simulated power meter (for development)",
        action="store_true",
    )

    power_group.add_argument(
        "--power-voltage",
        help="Set the specified voltage on the power-supply. Be VERY careful, you can burn things up.",
    )

    power_group.add_argument(
        "--power-stress",
        help="Perform power monitor stress testing, to capture a power consumption profile for the device (also requires --power-mon)",
        action="store_true",
    )

    power_group.add_argument(
        "--power-wait",
        help="Prompt the user to wait for device reset before looking for device serial ports (some boards kill power to USB serial port)",
        action="store_true",
    )

    power_group.add_argument(
        "--slog",
        help="Store structured-logs (slogs) for this run, optionally you can specify a destination directory",
        nargs="?",
        default=None,
        const="default",
    )

    remoteHardwareArgs = parser.add_argument_group(
        "Remote Hardware", "Arguments related to the Remote Hardware module"
    )

    remoteHardwareArgs.add_argument(
        "--gpio-wrb", nargs=2, help="Set a particular GPIO # to 1 or 0", action="append"
    )

    remoteHardwareArgs.add_argument(
        "--gpio-rd", help="Read from a GPIO mask (ex: '0x10')"
    )

    remoteHardwareArgs.add_argument(
        "--gpio-watch", help="Start watching a GPIO mask for changes (ex: '0x10')"
    )

    have_tunnel = platform.system() == "Linux"
    if have_tunnel:
        tunnelArgs = parser.add_argument_group(
            "Tunnel", "Arguments related to establishing a tunnel device over the mesh."
        )
        tunnelArgs.add_argument(
            "--tunnel",
            action="store_true",
            help="Create a TUN tunnel device for forwarding IP packets over the mesh",
        )
        tunnelArgs.add_argument(
            "--subnet",
            dest="tunnel_net",
            help="Sets the local-end subnet address for the TUN IP bridge. (ex: 10.115' which is the default)",
            default=None,
        )

    parser.set_defaults(deprecated=None)

    if argcomplete is not None:
        argcomplete.autocomplete(parser)
    args = parser.parse_args()
    mt_config.args = args
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
    initParser()
    common()


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
