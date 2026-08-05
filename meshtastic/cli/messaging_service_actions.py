"""Connected CLI actions for messaging, reads, and long-running services."""

from __future__ import annotations

import logging
import platform
import time
from dataclasses import dataclass
from typing import Any, Callable, NoReturn

from meshtastic._core_constants import BROADCAST_ADDR
from meshtastic.cli.context import CliContext
from meshtastic.protobuf import portnums_pb2

logger = logging.getLogger(__name__)

GPIO_WATCH_INTERVAL_SECONDS = 1.0
GPIO_READ_POLL_INTERVAL_SECONDS = 1.0
GPIO_READ_MAX_POLLS = 10


@dataclass(frozen=True, slots=True)
class MessagingServiceHooks:
    """Compatibility and optional-subsystem seams for service actions."""

    cli_exit: Callable[..., NoReturn]
    cli_print: Callable[[str], None]
    get_channel_index: Callable[[], int | None]
    check_channel: Callable[[Any, int], bool]
    remote_hardware_client: Callable[[Any], Any]
    get_pref: Callable[[Any, str], bool]
    validate_cli_show_fields: Callable[[Any, list[str]], None]
    newer_version: Callable[[], str | None]
    install_upgrade_hint: str
    powermon_available: Callable[[], bool]
    powermon_error: Callable[[], BaseException | None]
    log_set_factory: Callable[[Any, str | None, Any], Any] | None
    power_stress_factory: Callable[[Any], Any] | None
    get_meter: Callable[[], Any]
    platform_system: Callable[[], str] = platform.system


def _selected_channel(hooks: MessagingServiceHooks) -> int:
    return hooks.get_channel_index() or 0


def handle_messaging_actions(
    context: CliContext, hooks: MessagingServiceHooks
) -> None:
    """Send messages, requests, and remote-hardware operations in CLI order."""
    args = context.args
    interface = context.interface
    get_node_kwargs = context.get_node_kwargs

    if args.sendtext:
        context.outcome.close_now = True
        channel_index = _selected_channel(hooks)
        if not hooks.check_channel(interface, channel_index):
            hooks.cli_exit(
                f"Warning: {channel_index} is not a valid channel. "
                "Channel must not be DISABLED."
            )
        hooks.cli_print(
            f"Sending text message {args.sendtext} to {args.dest} "
            f"on channelIndex:{channel_index}"
            f" {'using PRIVATE_APP port' if args.private else ''}"
        )
        interface.sendText(
            args.sendtext,
            args.dest,
            wantAck=True,
            channelIndex=channel_index,
            onResponse=interface.getNode(args.dest, False, **get_node_kwargs).onAckNak,
            portNum=(
                portnums_pb2.PortNum.PRIVATE_APP
                if args.private
                else portnums_pb2.PortNum.TEXT_MESSAGE_APP
            ),
        )

    if args.traceroute:
        channel_index = _selected_channel(hooks)
        if hooks.check_channel(interface, channel_index):
            hop_limit = interface.localNode.localConfig.lora.hop_limit
            destination = str(args.traceroute)
            hooks.cli_print(
                f"Sending traceroute request to {destination} on "
                f"channelIndex:{channel_index} (this could take a while)"
            )
            interface.sendTraceRoute(
                destination, hop_limit, channelIndex=channel_index
            )

    if args.request_telemetry:
        if args.dest == BROADCAST_ADDR:
            hooks.cli_exit("Warning: Must use a destination node ID.")
        channel_index = _selected_channel(hooks)
        if hooks.check_channel(interface, channel_index):
            telemetry_map = {
                "device": "device_metrics",
                "environment": "environment_metrics",
                "air_quality": "air_quality_metrics",
                "airquality": "air_quality_metrics",
                "power": "power_metrics",
                "localstats": "local_stats",
                "local_stats": "local_stats",
            }
            telemetry_type = telemetry_map.get(
                args.request_telemetry, "device_metrics"
            )
            hooks.cli_print(
                f"Sending {telemetry_type} telemetry request to {args.dest} on "
                f"channelIndex:{channel_index} (this could take a while)"
            )
            interface.sendTelemetry(
                destinationId=args.dest,
                wantResponse=True,
                channelIndex=channel_index,
                telemetryType=telemetry_type,
            )

    if args.request_position:
        if args.dest == BROADCAST_ADDR:
            hooks.cli_exit("Warning: Must use a destination node ID.")
        channel_index = _selected_channel(hooks)
        if hooks.check_channel(interface, channel_index):
            hooks.cli_print(
                f"Sending position request to {args.dest} on "
                f"channelIndex:{channel_index} (this could take a while)"
            )
            interface.sendPosition(
                destinationId=args.dest,
                wantResponse=True,
                channelIndex=channel_index,
            )

    if args.gpio_wrb or args.gpio_rd or args.gpio_watch:
        if args.dest == BROADCAST_ADDR:
            hooks.cli_exit("Warning: Must use a destination node ID.")
        client = hooks.remote_hardware_client(interface)

        if args.gpio_wrb:
            bitmask = 0
            bitval = 0
            for bit, value in args.gpio_wrb or []:
                bitmask |= 1 << int(bit)
                bitval |= int(value) << int(bit)
            hooks.cli_print(
                f"Writing GPIO mask 0x{bitmask:x} with value 0x{bitval:x} "
                f"to {args.dest}"
            )
            client.writeGPIOs(args.dest, bitmask, bitval)
            context.outcome.close_now = True

        if args.gpio_rd:
            bitmask = int(args.gpio_rd, 16)
            hooks.cli_print(f"Reading GPIO mask 0x{bitmask:x} from {args.dest}")
            interface.mask = bitmask
            client.readGPIOs(args.dest, bitmask, None)
            for _ in range(GPIO_READ_MAX_POLLS):
                time.sleep(GPIO_READ_POLL_INTERVAL_SECONDS)
                if interface.gotResponse:
                    break
            logger.debug("end of gpio_rd")

        if args.gpio_watch:
            bitmask = int(args.gpio_watch, 16)
            hooks.cli_print(
                f"Watching GPIO mask 0x{bitmask:x} from {args.dest}. "
                "Press ctrl-c to exit"
            )
            while True:
                client.watchGPIOs(args.dest, bitmask)
                time.sleep(GPIO_WATCH_INTERVAL_SECONDS)


def handle_content_reads(context: CliContext) -> None:
    """Read canned-message and ringtone content in their historical position."""
    args = context.args
    interface = context.interface

    if args.get_canned_message:
        context.outcome.close_now = True
        print("")
        messages = interface.getNode(
            args.dest, **context.get_node_kwargs
        ).get_canned_message()
        print(f"canned_plugin_message:{messages}")

    if args.get_ringtone:
        context.outcome.close_now = True
        print("")
        ringtone = interface.getNode(
            args.dest, **context.get_node_kwargs
        ).get_ringtone()
        print(f"ringtone:{ringtone}")


def handle_information_actions(
    context: CliContext, hooks: MessagingServiceHooks
) -> None:
    """Handle info, preference reads, node listing, and show-field validation."""
    args = context.args
    interface = context.interface

    if args.info:
        print("")
        if args.dest == BROADCAST_ADDR:
            interface.showInfo()
            print("")
            interface.getNode(args.dest, **context.get_node_kwargs).showInfo()
            context.outcome.close_now = True
            print("")
            pypi_version = hooks.newer_version()
            if pypi_version:
                print(
                    f"*** A newer version v{pypi_version} is available!"
                    f' Consider running "{hooks.install_upgrade_hint}" ***\n'
                )
        else:
            print("Showing info of remote node is not supported.")
            print(
                "Use the '--get' command for a specific configuration "
                "(e.g. 'lora') instead."
            )

    if args.get:
        context.outcome.close_now = True
        node = interface.getNode(args.dest, False, **context.get_node_kwargs)
        found = False
        for pref in args.get:
            found = hooks.get_pref(node, pref[0])
        if found:
            hooks.cli_print("Completed getting preferences")

    if args.nodes:
        context.outcome.close_now = True
        if args.dest != BROADCAST_ADDR:
            print("Showing node list of a remote node is not supported.")
            context.outcome.stop_processing = True
            return
        if args.show_fields:
            hooks.validate_cli_show_fields(interface, args.show_fields)
        interface.showNodes(True, args.show_fields)

    if args.show_fields and not args.nodes:
        print("--show-fields can only be used with --nodes")
        context.outcome.stop_processing = True


def _start_tunnel(context: CliContext, hooks: MessagingServiceHooks) -> None:
    args = context.args
    if hooks.platform_system() != "Linux" or not args.tunnel:
        return
    if args.dest != BROADCAST_ADDR:
        hooks.cli_exit("A tunnel can only be created using the local node.", 1)

    context.outcome.close_now = False
    if context.interface.noProto:
        logger.warning("Not starting Tunnel - disabled by noProto")
        return

    from meshtastic import tunnel  # pylint: disable=import-outside-toplevel

    if args.tunnel_net:
        tunnel.Tunnel(context.interface, subnet=args.tunnel_net)
    else:
        tunnel.Tunnel(context.interface)


def handle_long_running_services(
    context: CliContext, hooks: MessagingServiceHooks
) -> None:
    """Start structured logging, power stress, listening, and tunnel services."""
    args = context.args
    interface = context.interface

    if args.slog or args.power_stress:
        if not hooks.powermon_available():
            hooks.cli_exit(
                "The powermon module could not be loaded. "
                "You may need to run `poetry install --with powermon`. "
                f"Import Error was: {hooks.powermon_error()}"
            )

        if args.slog:
            if hooks.log_set_factory is None:
                hooks.cli_exit(
                    "LogSet is required for --slog but not available. "
                    "The powermon module loaded incompletely."
                )
            log_set = hooks.log_set_factory(
                interface,
                args.slog if args.slog != "default" else None,
                hooks.get_meter(),
            )
            context.outcome.cleanup_callbacks.append(log_set.close)

        if args.power_stress:
            if hooks.power_stress_factory is None:
                hooks.cli_exit(
                    "PowerStress is required for --power-stress but not available. "
                    "The powermon module loaded incompletely."
                )
            hooks.power_stress_factory(interface).run()
            context.outcome.close_now = True

    if args.listen:
        context.outcome.close_now = False

    _start_tunnel(context, hooks)
