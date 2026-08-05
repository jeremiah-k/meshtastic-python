"""Connected CLI actions that mutate or administer a node/device.

The public CLI entrypoint remains ``meshtastic.__main__``. This module owns the
internal execution policy for device-oriented actions while dependencies that
are established compatibility/test seams are supplied explicitly by the
entrypoint.
"""

from __future__ import annotations

import contextlib
import getpass
import logging
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Any, NoReturn

from pubsub import pub

import meshtastic.ota
import meshtastic.serial_interface
import meshtastic.tcp_interface
import meshtastic.util
from meshtastic._core_constants import LOCAL_ADDR
from meshtastic.cli.context import CliContext
from meshtastic.mesh_interface import MeshInterface
from meshtastic.protobuf import admin_pb2, mesh_pb2

logger = logging.getLogger(__name__)

FACTORY_RESET_READY_PROBE_TIMEOUT_SECONDS = 20.0
FACTORY_RESET_ACCEPTANCE_TIMEOUT_SECONDS = 20.0
FACTORY_RESET_ACCEPTANCE_POLL_SECONDS = 0.05
OTA_REBOOT_WAIT_SECONDS: float = 5.0
OTA_RETRY_DELAY_SECONDS: float = 2.0
OTA_MAX_RETRIES: int = 5


@dataclass(frozen=True, slots=True)
class DeviceActionHooks:
    """Entrypoint-owned dependencies used by device action execution.

    Parameters
    ----------
    cli_exit : Callable[[str, int], NoReturn]
        User-facing CLI exit function.
    cli_print : Callable[[str], None]
        Quiet-aware CLI reporter.
    set_pref : Callable[[Any, str, Any], bool]
        Preference assignment compatibility seam.
    is_local_destination : Callable[[Any, str], bool]
        Destination classifier.
    send_local_factory_reset_and_wait : Callable[..., Any]
        Local destructive-reset acceptance helper.
    post_factory_reset_ready_probe : Callable[[MeshInterface], None]
        Best-effort serial readiness probe used after a full local reset.
    handle_ota_update : Callable[[MeshInterface, Any, dict[str, Any]], None]
        Wi-Fi OTA execution helper.
    build_lockdown_auth, read_lockdown_passphrase_file, send_lockdown_auth,
    validate_lockdown_passphrase : Callable[..., Any]
        Lockdown compatibility seams retained by the entrypoint.
    """

    cli_exit: Callable[[str, int], NoReturn]
    cli_print: Callable[[str], None]
    set_pref: Callable[[Any, str, Any], bool]
    is_local_destination: Callable[[Any, str], bool]
    send_local_factory_reset_and_wait: Callable[..., Any]
    post_factory_reset_ready_probe: Callable[[MeshInterface], None]
    handle_ota_update: Callable[[MeshInterface, Any, dict[str, Any]], None]
    build_lockdown_auth: Callable[..., Any]
    read_lockdown_passphrase_file: Callable[[str], bytes]
    send_lockdown_auth: Callable[..., Any]
    validate_lockdown_passphrase: Callable[[bytes], bytes]


def send_local_factory_reset_and_wait(
    reset_node: Any,
    *,
    full: bool,
    cli_print: Callable[[str], None],
    timeout: float | None = None,
) -> mesh_pb2.MeshPacket | None:
    """Send a local factory reset and wait for ACK/NAK or reboot transport loss.

    Parameters
    ----------
    reset_node : Any
        Local node whose interface transports the destructive reset request.
    full : bool
        Whether to request a complete device factory reset.
    cli_print : Callable[[str], None]
        Quiet-aware CLI reporter used for the acceptance wait message.
    timeout : float | None
        Optional acceptance deadline override.

    Returns
    -------
    mesh_pb2.MeshPacket | None
        Sent request packet, or ``None`` when the node returns no packet.

    Raises
    ------
    ValueError
        If the supplied timeout is not positive.
    MeshInterface.MeshInterfaceError
        If the request is rejected or no acceptance/reboot signal arrives.
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
        raise_wait_error = getattr(reset_interface, "_raise_wait_error_if_present", None)
        scoped_wait_available = (
            request_id is not None
            and callable(wait_for_request_ack)
            and callable(raise_wait_error)
        )

        cli_print("Waiting for factory reset acknowledgment or reboot disconnect")
        deadline = time.monotonic() + acceptance_timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break

            if (
                scoped_wait_available
                and callable(wait_for_request_ack)
                and callable(raise_wait_error)
            ):
                completed = wait_for_request_ack(
                    "receivedNak",
                    request_id,
                    timeout_seconds=min(FACTORY_RESET_ACCEPTANCE_POLL_SECONDS, remaining),
                )
                if completed:
                    raise_wait_error("receivedNak", request_id=request_id)
                    return request
            else:
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
                time.sleep(min(FACTORY_RESET_ACCEPTANCE_POLL_SECONDS, remaining))

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


@contextlib.contextmanager
def temporary_instance_attributes(
    instance: Any, overrides: dict[str, Any]
) -> Iterator[None]:
    """Temporarily override instance attributes and restore exact prior state.

    Parameters
    ----------
    instance : Any
        Object whose instance dictionary is temporarily modified.
    overrides : dict[str, Any]
        Attribute/value overrides to install for the duration of the context.

    Yields
    ------
    None
        Control while the overrides are active.
    """
    missing = object()
    instance_values = vars(instance)
    previous_values = {name: instance_values.get(name, missing) for name in overrides}
    try:
        for name, value in overrides.items():
            setattr(instance, name, value)
        yield
    finally:
        for name, previous in previous_values.items():
            if previous is missing:
                with contextlib.suppress(AttributeError):
                    delattr(instance, name)
            else:
                setattr(instance, name, previous)


def post_factory_reset_ready_probe(interface: MeshInterface) -> None:
    """Close, briefly probe serial readiness, then release the serial port.

    Parameters
    ----------
    interface : MeshInterface
        Connected interface; non-serial transports are ignored.
    """
    if not isinstance(interface, meshtastic.serial_interface.SerialInterface):
        return

    logger.debug("Factory reset: closing serial interface to release port.")
    try:
        interface.close()
    except Exception:
        logger.debug("Factory reset: initial serial close failed.", exc_info=True)

    logger.debug(
        "Factory reset: probing reconnect readiness (timeout=%.1fs)...",
        FACTORY_RESET_READY_PROBE_TIMEOUT_SECONDS,
    )
    probe_overrides = {
        "_connect_wait_timeout_seconds": FACTORY_RESET_READY_PROBE_TIMEOUT_SECONDS,
        "_connect_retry_budget_seconds": FACTORY_RESET_READY_PROBE_TIMEOUT_SECONDS,
        "_suppress_connect_failure_logging": True,
    }
    probe_start = time.monotonic()
    with temporary_instance_attributes(interface, probe_overrides):
        try:
            interface.connect()
            logger.debug(
                "Factory reset: reconnect probe succeeded in %.2fs.",
                time.monotonic() - probe_start,
            )
        except Exception as exc:
            logger.info(
                "Factory reset accepted; device is still rebooting after %.1fs "
                "and the next command will reconnect normally (%s).",
                time.monotonic() - probe_start,
                exc,
            )
    try:
        interface.close()
    except Exception:
        logger.debug("Factory reset: final serial close failed.", exc_info=True)


def handle_ota_update(
    interface: MeshInterface,
    args: Any,
    get_node_kwargs: dict[str, Any],
    *,
    cli_exit: Callable[[str, int], NoReturn],
    cli_print: Callable[[str], None],
    is_local_destination: Callable[[Any, str], bool],
) -> None:
    """Initiate a Wi-Fi OTA update for the directly connected local node.

    Parameters
    ----------
    interface : MeshInterface
        TCP interface connected to the target node.
    args : Any
        CLI arguments containing the OTA update path and destination.
    get_node_kwargs : dict[str, Any]
        Additional arguments for retrieving the local node.
    cli_exit : Callable[[str, int], NoReturn]
        User-facing exit handler.
    cli_print : Callable[[str], None]
        Quiet-aware reporter.
    is_local_destination : Callable[[Any, str], bool]
        Destination classifier.
    """
    if not isinstance(interface, meshtastic.tcp_interface.TCPInterface):
        cli_exit(
            "Error: OTA update currently requires a TCP connection to the node (use --host).",
            1,
        )
    if not is_local_destination(interface, args.dest):
        cli_exit(
            "Error: OTA update only supports the directly connected local node; "
            "omit --dest or use --dest ^local.",
            1,
        )

    try:
        ota = meshtastic.ota.ESP32WiFiOTA(args.ota_update, interface.hostname)
    except meshtastic.ota.OTAError as exc:
        cli_exit(f"OTA update failed: {exc}", 1)

    cli_print(f"Triggering OTA update on {interface.hostname}...")
    interface.getNode(LOCAL_ADDR, requestChannels=False, **get_node_kwargs).startOTA(
        mode=admin_pb2.OTAMode.OTA_WIFI, ota_file_hash=ota.hash_bytes()
    )
    cli_print("Waiting for device to reboot into OTA mode...")
    time.sleep(OTA_REBOOT_WAIT_SECONDS)

    retries = OTA_MAX_RETRIES
    while retries > 0:
        try:
            ota.update()
            break
        except meshtastic.ota.OTATransportError as exc:
            retries -= 1
            if retries == 0:
                cli_exit(f"OTA update failed: {exc}", 1)
            time.sleep(OTA_RETRY_DELAY_SECONDS)
        except meshtastic.ota.OTAError as exc:
            cli_exit(f"OTA update failed: {exc}", 1)

    cli_print("\nOTA update completed successfully!")


def handle_device_actions(context: CliContext, hooks: DeviceActionHooks) -> None:
    """Execute device- and node-administration actions in historical CLI order.

    Parameters
    ----------
    context : CliContext
        Connected invocation state and lifecycle outcome.
    hooks : DeviceActionHooks
        Entrypoint-owned compatibility dependencies.
    """
    interface = context.interface
    args = context.args
    get_node_kwargs = context.get_node_kwargs
    outcome = context.outcome

    if args.set_time is not None:
        interface.getNode(args.dest, False, **get_node_kwargs).setTime(args.set_time)

    if args.remove_position:
        outcome.close_now = True
        outcome.wait_for_ack_nak = True
        hooks.cli_print("Removing fixed position and disabling fixed position setting")
        interface.getNode(args.dest, False, **get_node_kwargs).removeFixedPosition()
    elif args.setlat or args.setlon or args.setalt:
        outcome.close_now = True
        outcome.wait_for_ack_nak = True
        alt = int(args.setalt) if args.setalt else 0
        lat = _parse_coordinate(args.setlat, "latitude", hooks.cli_print)
        lon = _parse_coordinate(args.setlon, "longitude", hooks.cli_print)
        if args.setalt:
            hooks.cli_print(f"Fixing altitude at {alt} meters")
        hooks.cli_print("Setting device position and enabling fixed position setting")
        interface.getNode(args.dest, False, **get_node_kwargs).setFixedPosition(
            lat, lon, alt
        )

    if args.set_owner or args.set_owner_short or args.set_is_unmessageable:
        outcome.close_now = True
        outcome.wait_for_ack_nak = True
        long_name = args.set_owner.strip() if args.set_owner else None
        short_name = args.set_owner_short.strip() if args.set_owner_short else None
        if long_name is not None and not long_name:
            hooks.cli_exit(
                "ERROR: Long Name cannot be empty or contain only whitespace characters",
                1,
            )
        if short_name is not None and not short_name:
            hooks.cli_exit(
                "ERROR: Short Name cannot be empty or contain only whitespace characters",
                1,
            )
        if long_name and short_name:
            hooks.cli_print(
                f"Setting device owner to {long_name} and short name to {short_name}"
            )
        elif long_name:
            hooks.cli_print(f"Setting device owner to {long_name}")
        elif short_name:
            hooks.cli_print(f"Setting device owner short to {short_name}")

        unmessagable = None
        if args.set_is_unmessageable is not None:
            unmessagable = (
                meshtastic.util.fromStr(args.set_is_unmessageable)
                if isinstance(args.set_is_unmessageable, str)
                else args.set_is_unmessageable
            )
            hooks.cli_print(f"Setting device owner is_unmessageable to {unmessagable}")
        interface.getNode(args.dest, False, **get_node_kwargs).setOwner(
            long_name=long_name,
            short_name=short_name,
            is_unmessagable=unmessagable,
        )

    _handle_content_updates(context, hooks)
    _handle_position_fields(context, hooks)

    if args.set_ham:
        ham_id = args.set_ham.strip()
        if not ham_id:
            hooks.cli_exit(
                "ERROR: Ham radio callsign cannot be empty or contain only whitespace characters",
                1,
            )
        outcome.close_now = True
        hooks.cli_print(f"Setting Ham ID to {ham_id} and turning off encryption")
        interface.getNode(args.dest, **get_node_kwargs).setOwner(
            ham_id, is_licensed=True
        )
        interface.getNode(args.dest, **get_node_kwargs).turnOffEncryptionOnPrimaryChannel()

    _handle_reboot_and_reset_actions(context, hooks)
    if outcome.stop_processing:
        return
    _handle_node_database_actions(context)


def _parse_coordinate(
    raw_value: Any,
    coordinate_name: str,
    cli_print: Callable[[str], None],
) -> float:
    if raw_value is None:
        return 0.0
    try:
        value: float = int(raw_value)
    except ValueError:
        value = float(raw_value)
    cli_print(f"Fixing {coordinate_name} at {value} degrees")
    return value


def _handle_content_updates(context: CliContext, hooks: DeviceActionHooks) -> None:
    interface, args, kwargs, outcome = (
        context.interface,
        context.args,
        context.get_node_kwargs,
        context.outcome,
    )
    if args.set_canned_message:
        outcome.close_now = True
        outcome.wait_for_ack_nak = True
        node = interface.getNode(args.dest, False, **kwargs)
        if node.module_available(mesh_pb2.CANNEDMSG_CONFIG):
            hooks.cli_print(f"Setting canned plugin message to {args.set_canned_message}")
            node.set_canned_message(args.set_canned_message)
        else:
            logger.warning("Canned Message module is excluded by firmware; skipping set.")
    if args.set_ringtone:
        outcome.close_now = True
        outcome.wait_for_ack_nak = True
        node = interface.getNode(args.dest, False, **kwargs)
        if node.module_available(mesh_pb2.EXTNOTIF_CONFIG):
            hooks.cli_print(f"Setting ringtone to {args.set_ringtone}")
            node.set_ringtone(args.set_ringtone)
        else:
            logger.warning(
                "External Notification is excluded by firmware; skipping ringtone set."
            )


def _handle_position_fields(context: CliContext, hooks: DeviceActionHooks) -> None:
    interface, args, kwargs, outcome = (
        context.interface,
        context.args,
        context.get_node_kwargs,
        context.outcome,
    )
    if args.pos_fields:
        outcome.close_now = True
        position_config = interface.getNode(args.dest, **kwargs).localConfig.position
        all_fields = 0
        try:
            for field in args.pos_fields:
                all_fields |= position_config.PositionFlags.Value(field)
        except ValueError:
            print("ERROR: supported position fields are:")
            print(position_config.PositionFlags.keys())
            print("If no fields are specified, will read and display current value.")
        else:
            hooks.cli_print(f"Setting position fields to {all_fields}")
            hooks.set_pref(position_config, "position_flags", f"{all_fields:d}")
            hooks.cli_print("Writing modified preferences to device")
            interface.getNode(args.dest, **kwargs).writeConfig("position")
    elif args.pos_fields is not None:
        outcome.close_now = True
        position_config = interface.getNode(args.dest, **kwargs).localConfig.position
        field_names = [
            position_config.PositionFlags.Name(bit)
            for bit in position_config.PositionFlags.values()
            if position_config.position_flags & bit
        ]
        print(" ".join(field_names))


def _handle_reboot_and_reset_actions(
    context: CliContext, hooks: DeviceActionHooks
) -> None:
    interface, args, kwargs, outcome = (
        context.interface,
        context.args,
        context.get_node_kwargs,
        context.outcome,
    )
    reboot_actions = (
        (args.reboot, "reboot"),
        (args.reboot_ota, "rebootOTA"),
        (args.enter_dfu, "enterDFUMode"),
        (args.shutdown, "shutdown"),
    )
    for enabled, method_name in reboot_actions:
        if not enabled:
            continue
        outcome.close_now = True
        outcome.wait_for_ack_nak = True
        outcome.skip_ack_wait = True
        getattr(interface.getNode(args.dest, False, **kwargs), method_name)()

    if args.ota_update:
        outcome.close_now = True
        outcome.skip_ack_wait = True
        hooks.handle_ota_update(interface, args, kwargs)
        outcome.stop_processing = True
        return

    if args.device_metadata:
        outcome.close_now = True
        interface.getNode(args.dest, False, **kwargs).getMetadata()
    if args.begin_edit:
        outcome.close_now = True
        interface.getNode(args.dest, False, **kwargs).beginSettingsTransaction()
    if args.commit_edit:
        outcome.close_now = True
        interface.getNode(args.dest, False, **kwargs).commitSettingsTransaction()

    if args.factory_reset or args.factory_reset_device:
        outcome.close_now = True
        outcome.wait_for_ack_nak = True
        outcome.skip_ack_wait = True
        full = bool(args.factory_reset_device)
        reset_node = interface.getNode(args.dest, False, **kwargs)
        is_local_reset = hooks.is_local_destination(interface, args.dest)
        if is_local_reset:
            hooks.send_local_factory_reset_and_wait(reset_node, full=full)
        else:
            reset_node.factoryReset(full=full)
        serial_interface_cls = getattr(
            meshtastic.serial_interface, "SerialInterface", None
        )
        if (
            full
            and is_local_reset
            and isinstance(serial_interface_cls, type)
            and isinstance(interface, serial_interface_cls)
        ):
            hooks.post_factory_reset_ready_probe(interface)


def _handle_node_database_actions(context: CliContext) -> None:
    interface, args, kwargs, outcome = (
        context.interface,
        context.args,
        context.get_node_kwargs,
        context.outcome,
    )
    actions = (
        (args.remove_node, "removeNode"),
        (args.set_favorite_node, "setFavorite"),
        (args.remove_favorite_node, "removeFavorite"),
        (args.set_ignored_node, "setIgnored"),
        (args.remove_ignored_node, "removeIgnored"),
    )
    for value, method_name in actions:
        if value:
            outcome.close_now = True
            outcome.wait_for_ack_nak = True
            getattr(interface.getNode(args.dest, False, **kwargs), method_name)(value)
    if args.reset_nodedb:
        outcome.close_now = True
        outcome.wait_for_ack_nak = True
        interface.getNode(args.dest, False, **kwargs).resetNodeDb()


def handle_lockdown_action(context: CliContext, hooks: DeviceActionHooks) -> None:
    """Execute the mutually exclusive firmware-lockdown action, if requested.

    Parameters
    ----------
    context : CliContext
        Connected invocation state.
    hooks : DeviceActionHooks
        Entrypoint-owned lockdown and reporting dependencies.
    """
    args = context.args
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
    if lockdown_action is None:
        return

    context.outcome.close_now = True
    if not hooks.is_local_destination(context.interface, args.dest):
        hooks.cli_exit(
            "Lockdown commands apply only to the directly connected local node.", 1
        )
    if lockdown_action in {"provision", "lock-now", "disable"} and not args.lockdown_yes:
        confirmation = (
            input(f"Type 'yes' to confirm lockdown {lockdown_action}: ")
            .strip()
            .casefold()
        )
        if confirmation != "yes":
            hooks.cli_exit("Aborted.", 1)

    try:
        passphrase = _read_lockdown_passphrase(args, lockdown_action, hooks)
        auth = hooks.build_lockdown_auth(
            passphrase,
            boots_remaining=args.lockdown_boots,
            valid_until_epoch=args.lockdown_valid_until,
            max_session_seconds=args.lockdown_max_session_seconds,
            lock_now=lockdown_action == "lock-now",
            disable=lockdown_action == "disable",
        )
    except (OSError, ValueError) as exc:
        hooks.cli_exit(f"Invalid lockdown options: {exc}", 1)

    try:
        status = hooks.send_lockdown_auth(
            context.interface,
            auth,
            timeout=args.lockdown_wait,
            allow_reboot_without_status=lockdown_action == "lock-now",
        )
    except (TimeoutError, ValueError, RuntimeError) as exc:
        hooks.cli_exit(f"Lockdown command failed: {exc}", 1)

    if status is None:
        print("Lockdown command accepted; device may already be rebooting.")
        return
    try:
        state_name = mesh_pb2.LockdownStatus.State.Name(status.state)
    except ValueError:
        state_name = f"STATE_{status.state}"
    print(f"Lockdown status: {state_name}")
    if status.backoff_seconds:
        print(f"Retry backoff: {status.backoff_seconds}s")
    if status.state == mesh_pb2.LockdownStatus.UNLOCK_FAILED:
        hooks.cli_exit("Lockdown authentication failed.", 1)


def _read_lockdown_passphrase(
    args: Any, lockdown_action: str, hooks: DeviceActionHooks
) -> bytes:
    if lockdown_action == "lock-now":
        return b""
    if args.lockdown_passphrase_file:
        return hooks.read_lockdown_passphrase_file(args.lockdown_passphrase_file)
    if args.lockdown_passphrase is not None:
        if not args.insecure_lockdown_passphrase_on_command_line:
            hooks.cli_exit(
                "--lockdown-passphrase requires "
                "--insecure-lockdown-passphrase-on-command-line; "
                "prefer an operator-only file or interactive entry.",
                1,
            )
        return hooks.validate_lockdown_passphrase(
            args.lockdown_passphrase.encode("utf-8")
        )

    entered = getpass.getpass("Lockdown passphrase: ")
    if lockdown_action == "provision":
        confirmed = getpass.getpass("Lockdown passphrase (confirm): ")
        if entered != confirmed:
            hooks.cli_exit("Lockdown passphrases do not match.", 1)
    return hooks.validate_lockdown_passphrase(entered.encode("utf-8"))
