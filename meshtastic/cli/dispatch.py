"""Connected CLI action dispatcher and lifecycle finalization."""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from typing import Callable

import meshtastic.cli.channel_contact_actions as channel_contact_actions
import meshtastic.cli.configure_actions as configure_actions
import meshtastic.cli.device_actions as device_actions
import meshtastic.cli.messaging_service_actions as messaging_service_actions
from meshtastic._core_constants import BROADCAST_ADDR
from meshtastic.cli.context import CliContext

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class DispatchHooks:
    """Action hooks and compatibility seams used by connected dispatch."""

    cli_print: Callable[[str], None]
    device: device_actions.DeviceActionHooks
    channel_contact: channel_contact_actions.ChannelContactHooks
    configure: configure_actions.ConfigureActionHooks
    services: messaging_service_actions.MessagingServiceHooks
    sleep: Callable[[float], None] = time.sleep


def _print_connection(context: CliContext, hooks: DispatchHooks) -> None:
    """Print the historical connection banner unless config export is active."""
    if context.args.export_config:
        return

    dev_path = getattr(context.interface, "devPath", "")
    if not dev_path:
        hooks.cli_print("Connected to radio")
        return

    tty_name = os.path.basename(dev_path)
    stable_path = getattr(context.interface, "_stable_path", None)
    if stable_path and stable_path != dev_path:
        hooks.cli_print(f"Connected to radio on {tty_name} (stable: {stable_path})")
    else:
        hooks.cli_print(f"Connected to radio on {tty_name}")


def _finalize_connected_actions(context: CliContext, hooks: DispatchHooks) -> None:
    """Perform final ACK wait, delayed disconnect, and interface-close decisions."""
    args = context.args
    outcome = context.outcome
    interface = context.interface

    if not outcome.skip_ack_wait and (
        args.ack or (args.dest != BROADCAST_ADDR and outcome.wait_for_ack_nak)
    ):
        hooks.cli_print(
            "Waiting for an acknowledgment from remote node (this could take a while)"
        )
        interface.getNode(
            args.dest, False, **context.get_node_kwargs
        ).iface.waitForAckNak()

    if args.wait_to_disconnect:
        hooks.cli_print(
            f"Waiting {args.wait_to_disconnect} seconds before disconnecting"
        )
        hooks.sleep(int(args.wait_to_disconnect))

    if not args.seriallog and outcome.close_now:
        try:
            interface.close()
        except Exception:
            logger.debug("Error during interface close", exc_info=True)


def _cleanup_connected_resources(context: CliContext) -> BaseException | None:
    """Release retained action resources and return the first cleanup failure."""
    first_error: BaseException | None = None
    for cleanup in reversed(context.outcome.cleanup_callbacks):
        try:
            cleanup()
        except BaseException as exc:  # noqa: BLE001 - preserve primary action failure
            if first_error is None:
                first_error = exc
            else:
                logger.warning("Additional connected-action cleanup failed", exc_info=True)
    context.outcome.cleanup_callbacks.clear()
    return first_error


def dispatch_connected(context: CliContext, hooks: DispatchHooks) -> None:
    """Execute all connected CLI action groups in their historical order."""
    outcome = context.outcome
    action_error: BaseException | None = None
    try:
        _print_connection(context, hooks)

        device_actions.handle_device_actions(context, hooks.device)
        if outcome.stop_processing:
            return

        channel_contact_actions.handle_contact_import(context)
        messaging_service_actions.handle_messaging_actions(context, hooks.services)

        configure_actions.handle_configure_actions(context, hooks.configure)
        if outcome.stop_processing:
            return

        channel_contact_actions.handle_channel_mutations(context, hooks.channel_contact)
        messaging_service_actions.handle_content_reads(context)
        channel_contact_actions.handle_region_preset_display(
            context, hooks.channel_contact
        )
        device_actions.handle_lockdown_action(context, hooks.device)
        messaging_service_actions.handle_information_actions(context, hooks.services)
        if outcome.stop_processing:
            return

        channel_contact_actions.handle_channel_contact_display(
            context, hooks.channel_contact
        )
        messaging_service_actions.handle_long_running_services(context, hooks.services)
        _finalize_connected_actions(context, hooks)
    except BaseException as exc:
        action_error = exc
        raise
    finally:
        cleanup_error = _cleanup_connected_resources(context)
        if cleanup_error is not None:
            if action_error is None or not isinstance(cleanup_error, Exception):
                raise cleanup_error
            logger.warning(
                "Connected-action cleanup failed while unwinding another error",
                exc_info=(
                    type(cleanup_error),
                    cleanup_error,
                    cleanup_error.__traceback__,
                ),
            )
