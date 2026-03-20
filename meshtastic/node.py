# pylint: disable=too-many-lines
"""Node class for representing and managing mesh nodes.

This module provides the Node class which represents a (local or remote) node
in the mesh, including methods for localConfig, moduleConfig, and channels management.
"""

import base64
import binascii
import logging
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import (
    TYPE_CHECKING,
    Any,
    Callable,
    NoReturn,
    Sequence,
    TypeVar,
    cast,
)

from google.protobuf.descriptor import FieldDescriptor
from google.protobuf.message import DecodeError

from meshtastic.protobuf import (
    admin_pb2,
    apponly_pb2,
    channel_pb2,
    config_pb2,
    localonly_pb2,
    mesh_pb2,
    portnums_pb2,
)
from meshtastic.util import (
    Timeout,
    camel_to_snake,
    flagsToList,
    fromPSK,
    generate_channel_hash,
    messageToJson,
    pskToString,
    stripnl,
    toNodeNum,
)

if TYPE_CHECKING:
    from meshtastic.mesh_interface import MeshInterface

logger = logging.getLogger(__name__)

# Validation error messages for setOwner
EMPTY_LONG_NAME_MSG = "Long Name cannot be empty or contain only whitespace characters"
EMPTY_SHORT_NAME_MSG = (
    "Short Name cannot be empty or contain only whitespace characters"
)
# Maximum length for long_name (per protobuf definition in mesh.options)
MAX_LONG_NAME_LEN = 40
# Maximum length for owner short_name.
MAX_SHORT_NAME_LEN = 4
# Maximum text length for ringtone messages.
MAX_RINGTONE_LENGTH = 230
# Maximum text length for canned-message payloads.
MAX_CANNED_MESSAGE_LENGTH = 200
# Maximum number of channels a node can hold.
MAX_CHANNELS = 8
# Protobuf factory-reset fields are integer-typed; use the explicit sentinel
# value instead of boolean assignment to avoid firmware-side coercion issues.
FACTORY_RESET_REQUEST_VALUE: int = 1
# Extra wait used only when getMetadata() runs under redirected stdout for
# historical callers that parse printed metadata lines.
METADATA_STDOUT_COMPAT_WAIT_SECONDS = 1.0
NAMED_ADMIN_CHANNEL_NAME = "admin"
_ResultT = TypeVar("_ResultT")


def _is_named_admin_channel_name(channel_name: str) -> bool:
    """Return whether a channel name designates the special named admin channel."""
    return channel_name.lower() == NAMED_ADMIN_CHANNEL_NAME


def _ordered_admin_indexes(*indexes: int | None) -> list[int]:
    """Return unique non-None admin channel indexes, preserving input order."""
    ordered: list[int] = []
    for index in indexes:
        if index is None or index in ordered:
            continue
        ordered.append(index)
    return ordered


@dataclass(frozen=True)
class _SetUrlParsedInput:
    """Parsed/decoded setURL input."""

    url: str
    channel_set: apponly_pb2.ChannelSet
    has_lora_update: bool


@dataclass(frozen=True)
class _SetUrlAdminContext:
    """Admin-channel write path metadata resolved before planning/execution."""

    admin_write_node: "Node"
    admin_index_for_write: int
    named_admin_index_for_write: int | None
    has_admin_write_node_named_admin: bool


@dataclass
class _SetUrlAddOnlyPlan:
    """Planning output for addOnly transactions."""

    ignored_channel_names: list[str]
    channels_to_write: list[tuple[channel_pb2.Channel, str]]
    deferred_add_only_admin_channel: tuple[channel_pb2.Channel, str] | None
    deferred_add_only_admin_index: int | None
    original_channels_by_index: dict[int, channel_pb2.Channel]
    original_lora_config: config_pb2.Config.LoRaConfig | None


@dataclass
class _SetUrlReplacePlan:
    """Planning output for replace-all transactions."""

    max_channels: int
    replace_original_channels_snapshot: list[channel_pb2.Channel]
    replace_original_channels_by_index: dict[int, channel_pb2.Channel]
    staged_channels: list[channel_pb2.Channel]
    staged_channels_by_index: dict[int, channel_pb2.Channel]
    deferred_new_named_admin_channel: channel_pb2.Channel | None
    deferred_new_named_admin_index: int | None
    deferred_previous_admin_slot_channel: channel_pb2.Channel | None
    replace_original_lora_config: config_pb2.Config.LoRaConfig | None


@dataclass
class _SetUrlAddOnlyExecutionState:
    """Execution/rollback tracking state for addOnly transactions."""

    written_indices: list[int] = field(default_factory=list)
    lora_write_started: bool = False


@dataclass
class _SetUrlReplaceExecutionState:
    """Execution/rollback tracking state for replace-all transactions."""

    written_channel_indices: list[int] = field(default_factory=list)
    lora_write_started: bool = False
    rollback_admin_index_for_write: int = 0


class _SetUrlParser:
    """Owns URL fragment decoding and ChannelSet parse/validation."""

    @staticmethod
    def parse(
        url: str,
        *,
        raise_interface_error: Callable[[str], NoReturn],
    ) -> _SetUrlParsedInput:
        """Parse URL fragment into a ChannelSet payload with setURL validations."""
        # URLs are of the form https://meshtastic.org/d/#{base64_channel_set}
        # Parse from '#' to support optional query parameters before the fragment.
        if "#" not in url:
            raise_interface_error(f"Invalid URL '{url}'")
        b64 = url.split("#")[-1]
        if not b64:
            raise_interface_error(f"Invalid URL '{url}': no channel data found")

        # We normally strip padding to make for a shorter URL, but the python parser doesn't like
        # that.  So add back any missing padding
        # per https://stackoverflow.com/a/9807138
        missing_padding = len(b64) % 4
        if missing_padding:
            b64 += "=" * (4 - missing_padding)

        try:
            decoded_url = base64.urlsafe_b64decode(b64)
        except (binascii.Error, ValueError) as ex:
            raise_interface_error(f"Invalid URL '{url}': {ex}")

        channel_set = apponly_pb2.ChannelSet()
        try:
            channel_set.ParseFromString(decoded_url)
        except (DecodeError, ValueError) as ex:
            raise_interface_error(
                f"Unable to parse channel settings from URL '{url}': {ex}"
            )

        if len(channel_set.settings) == 0:
            raise_interface_error("There were no settings.")
        return _SetUrlParsedInput(
            url=url,
            channel_set=channel_set,
            has_lora_update=channel_set.HasField("lora_config"),
        )


class _SetUrlAddOnlyPlanner:
    """Owns addOnly transaction planning and snapshot capture."""

    def __init__(
        self,
        node: "Node",
        *,
        parsed_input: _SetUrlParsedInput,
        admin_context: _SetUrlAdminContext,
    ) -> None:
        self._node = node
        self._parsed_input = parsed_input
        self._admin_context = admin_context

    def capture_original_lora_snapshot(self) -> config_pb2.Config.LoRaConfig | None:
        """Capture original LoRa config snapshot for addOnly rollback when needed."""
        if not self._parsed_input.has_lora_update:
            return None
        if not self._node.localConfig.HasField("lora"):
            self._node._raise_interface_error(  # noqa: SLF001
                "LoRa config must be loaded before setURL(addOnly=True)"
            )
        original_lora_config = config_pb2.Config.LoRaConfig()
        original_lora_config.CopyFrom(self._node.localConfig.lora)
        return original_lora_config

    def build_plan(
        self,
        *,
        original_lora_config: config_pb2.Config.LoRaConfig | None,
    ) -> _SetUrlAddOnlyPlan:
        """Build addOnly staging plan, dedupe selection, and deferred admin handling."""
        ignored_channel_names: list[str] = []
        channels_to_write: list[tuple[channel_pb2.Channel, str]] = []
        original_channels_by_index: dict[int, channel_pb2.Channel] = {}
        with self._node._channels_lock:  # noqa: SLF001
            channels = self._node.channels
            if channels is None:
                self._node._raise_interface_error(
                    "Config or channels not loaded"
                )  # noqa: SLF001
            existing_names_normalized = {
                channel.settings.name.lower()
                for channel in channels
                if (
                    channel.role != channel_pb2.Channel.Role.DISABLED
                    and channel.settings
                    and channel.settings.name
                )
            }
            disabled_channels = [
                channel
                for channel in channels
                if channel.role == channel_pb2.Channel.Role.DISABLED
            ]
            pending_new_settings: list[channel_pb2.ChannelSettings] = []
            for channel_settings in self._parsed_input.channel_set.settings:
                channel_name = channel_settings.name
                normalized_name = channel_name.lower()
                if channel_name == "" or normalized_name in existing_names_normalized:
                    ignored_channel_names.append(channel_name)
                    continue
                pending_new_settings.append(channel_settings)
                existing_names_normalized.add(normalized_name)

            if len(pending_new_settings) > len(disabled_channels):
                self._node._raise_interface_error(  # noqa: SLF001
                    "No free channels were found for all additions "
                    f"(need {len(pending_new_settings)}, available {len(disabled_channels)})"
                )

            for disabled_channel, new_settings in zip(
                disabled_channels,
                pending_new_settings,
                strict=False,
            ):
                previous_channel = channel_pb2.Channel()
                previous_channel.CopyFrom(disabled_channel)
                original_channels_by_index[disabled_channel.index] = previous_channel

                staged_channel = channel_pb2.Channel()
                staged_channel.CopyFrom(disabled_channel)
                staged_channel.settings.CopyFrom(new_settings)
                staged_channel.role = channel_pb2.Channel.Role.SECONDARY
                channels_to_write.append((staged_channel, new_settings.name))

        deferred_add_only_admin_channel: tuple[channel_pb2.Channel, str] | None = None
        deferred_add_only_admin_index: int | None = None
        if not self._admin_context.has_admin_write_node_named_admin:
            deferred_add_only_admin_channel = next(
                (
                    candidate
                    for candidate in channels_to_write
                    if _is_named_admin_channel_name(candidate[1])
                ),
                None,
            )
        if deferred_add_only_admin_channel is not None:
            deferred_add_only_admin_index = deferred_add_only_admin_channel[0].index

        return _SetUrlAddOnlyPlan(
            ignored_channel_names=ignored_channel_names,
            channels_to_write=channels_to_write,
            deferred_add_only_admin_channel=deferred_add_only_admin_channel,
            deferred_add_only_admin_index=deferred_add_only_admin_index,
            original_channels_by_index=original_channels_by_index,
            original_lora_config=original_lora_config,
        )


class _SetUrlReplacePlanner:
    """Owns replace-all transaction planning and snapshot capture."""

    def __init__(
        self,
        node: "Node",
        *,
        parsed_input: _SetUrlParsedInput,
        admin_context: _SetUrlAdminContext,
    ) -> None:
        self._node = node
        self._parsed_input = parsed_input
        self._admin_context = admin_context

    def build_plan(self) -> _SetUrlReplacePlan:
        """Build replace-all staging plan, deferred admin strategy, and snapshots."""
        with self._node._channels_lock:  # noqa: SLF001
            channels = self._node.channels
            if channels is None:
                self._node._raise_interface_error(
                    "Config or channels not loaded"
                )  # noqa: SLF001
            max_channels = len(channels)
            replace_original_channels_snapshot: list[channel_pb2.Channel] = []
            replace_original_channels_by_index: dict[int, channel_pb2.Channel] = {}
            for existing_channel in channels:
                channel_snapshot = channel_pb2.Channel()
                channel_snapshot.CopyFrom(existing_channel)
                replace_original_channels_snapshot.append(channel_snapshot)
                replace_original_channels_by_index[existing_channel.index] = (
                    channel_snapshot
                )

        replace_original_lora_config: config_pb2.Config.LoRaConfig | None = None
        if self._parsed_input.has_lora_update and self._node.localConfig.HasField(
            "lora"
        ):
            replace_original_lora_config = config_pb2.Config.LoRaConfig()
            replace_original_lora_config.CopyFrom(self._node.localConfig.lora)

        staged_channels: list[channel_pb2.Channel] = []
        for i, channel_settings in enumerate(self._parsed_input.channel_set.settings):
            if i >= max_channels:
                logger.warning(
                    "URL contains more than %d channels; extra channels are ignored.",
                    max_channels,
                )
                break
            staged_channel = channel_pb2.Channel()
            staged_channel.role = (
                channel_pb2.Channel.Role.PRIMARY
                if i == 0
                else channel_pb2.Channel.Role.SECONDARY
            )
            staged_channel.index = i
            staged_channel.settings.CopyFrom(channel_settings)
            staged_channels.append(staged_channel)

        # Full-replace semantics: any channels not present in the URL should be
        # explicitly disabled so stale secondaries/admin channels do not persist.
        for i in range(len(staged_channels), max_channels):
            disabled_channel = channel_pb2.Channel()
            disabled_channel.index = i
            disabled_channel.role = channel_pb2.Channel.Role.DISABLED
            staged_channels.append(disabled_channel)

        staged_named_admin_channels = [
            staged_channel
            for staged_channel in staged_channels
            if staged_channel.settings
            and staged_channel.settings.name
            and _is_named_admin_channel_name(staged_channel.settings.name)
        ]
        if len(staged_named_admin_channels) > 1:
            self._node._raise_interface_error(  # noqa: SLF001
                "URL contains multiple channels named 'admin'; only one is allowed"
            )
        deferred_new_named_admin_channel = (
            staged_named_admin_channels[0] if staged_named_admin_channels else None
        )
        deferred_new_named_admin_index = (
            deferred_new_named_admin_channel.index
            if deferred_new_named_admin_channel is not None
            else None
        )
        staged_channels_by_index = {
            staged_channel.index: staged_channel for staged_channel in staged_channels
        }

        deferred_previous_admin_slot_channel: channel_pb2.Channel | None = None
        if self._admin_context.named_admin_index_for_write is not None:
            previous_admin_slot_channel = staged_channels_by_index.get(
                self._admin_context.named_admin_index_for_write
            )
            if previous_admin_slot_channel is not None and (
                deferred_new_named_admin_channel is None
                or previous_admin_slot_channel.index
                != deferred_new_named_admin_channel.index
            ):
                deferred_previous_admin_slot_channel = previous_admin_slot_channel

        return _SetUrlReplacePlan(
            max_channels=max_channels,
            replace_original_channels_snapshot=replace_original_channels_snapshot,
            replace_original_channels_by_index=replace_original_channels_by_index,
            staged_channels=staged_channels,
            staged_channels_by_index=staged_channels_by_index,
            deferred_new_named_admin_channel=deferred_new_named_admin_channel,
            deferred_new_named_admin_index=deferred_new_named_admin_index,
            deferred_previous_admin_slot_channel=deferred_previous_admin_slot_channel,
            replace_original_lora_config=replace_original_lora_config,
        )


class _SetUrlCacheManager:
    """Owns local cache updates, invalidation, and restoration for setURL transactions."""

    def __init__(self, node: "Node") -> None:
        self._node = node

    def apply_add_only_success(
        self,
        channels_to_write: list[tuple[channel_pb2.Channel, str]],
    ) -> None:
        """Apply addOnly channel cache updates after remote writes succeed."""
        with self._node._channels_lock:  # noqa: SLF001
            channels = self._node.channels
            if channels is None:
                logger.warning(
                    "Channel cache unavailable after successful addOnly apply; reload channels to refresh local state."
                )
                return
            for staged_channel, _ in channels_to_write:
                if 0 <= staged_channel.index < len(channels):
                    channels[staged_channel.index].CopyFrom(staged_channel)
                else:
                    logger.warning(
                        "Channel index %s out of range during addOnly cache update; invalidating local channel cache.",
                        staged_channel.index,
                    )
                    self._node.channels = None
                    self._node.partialChannels = []
                    break

    def apply_replace_channel_write(self, staged_channel: channel_pb2.Channel) -> None:
        """Apply one replace-path channel cache update after a successful write."""
        with self._node._channels_lock:  # noqa: SLF001
            channels = self._node.channels
            if channels is None:
                self._node._raise_interface_error(
                    "Config or channels not loaded"
                )  # noqa: SLF001
            if staged_channel.index < 0 or staged_channel.index >= len(channels):
                self._node._raise_interface_error(  # noqa: SLF001
                    f"Channel index {staged_channel.index} out of range during cache update"
                )
            channels[staged_channel.index].CopyFrom(staged_channel)

    def invalidate_channel_cache(self, warning_message: str) -> None:
        """Invalidate local channel cache after incomplete rollback."""
        with self._node._channels_lock:  # noqa: SLF001
            self._node.channels = None
            self._node.partialChannels = []
        logger.warning("%s", warning_message)

    def restore_replace_channels_snapshot(
        self,
        replace_original_channels_snapshot: list[channel_pb2.Channel],
    ) -> None:
        """Restore replace-all channel cache from pre-transaction snapshot."""
        with self._node._channels_lock:  # noqa: SLF001
            restored_channels: list[channel_pb2.Channel] = []
            for original_channel in replace_original_channels_snapshot:
                restored_channel = channel_pb2.Channel()
                restored_channel.CopyFrom(original_channel)
                restored_channels.append(restored_channel)
            self._node.channels = restored_channels
            self._node.partialChannels = []

    def apply_lora_success(self, lora_config: config_pb2.Config.LoRaConfig) -> None:
        """Apply successful LoRa cache update."""
        self._node.localConfig.lora.CopyFrom(lora_config)

    def restore_lora_snapshot(
        self,
        original_lora_config: config_pb2.Config.LoRaConfig,
    ) -> None:
        """Restore LoRa cache from pre-transaction snapshot."""
        self._node.localConfig.lora.CopyFrom(original_lora_config)

    def clear_lora_cache_with_warning(self, warning_message: str) -> None:
        """Clear LoRa cache when rollback cannot restore prior value."""
        self._node.localConfig.ClearField("lora")
        logger.warning("%s", warning_message)


class _SetUrlExecutionEngine:
    """Owns remote write ordering/admin-index strategy for setURL transactions."""

    def __init__(
        self,
        node: "Node",
        *,
        cache_manager: _SetUrlCacheManager,
    ) -> None:
        self._node = node
        self._cache_manager = cache_manager

    def execute_add_only(
        self,
        *,
        parsed_input: _SetUrlParsedInput,
        admin_context: _SetUrlAdminContext,
        plan: _SetUrlAddOnlyPlan,
        state: _SetUrlAddOnlyExecutionState,
    ) -> None:
        """Execute addOnly writes in transactional order."""
        for staged_channel, channel_name in plan.channels_to_write:
            if (
                plan.deferred_add_only_admin_channel is not None
                and staged_channel.index
                == plan.deferred_add_only_admin_channel[0].index
            ):
                continue
            logger.info("Adding new channel '%s' to device", channel_name)
            state.written_indices.append(staged_channel.index)
            self._node._write_channel_snapshot(  # noqa: SLF001
                staged_channel,
                adminIndex=admin_context.admin_index_for_write,
            )

        if parsed_input.has_lora_update:
            set_lora = admin_pb2.AdminMessage()
            set_lora.set_config.lora.CopyFrom(parsed_input.channel_set.lora_config)
            self._node.ensureSessionKey(adminIndex=admin_context.admin_index_for_write)
            state.lora_write_started = True
            self._node._send_admin(  # noqa: SLF001
                set_lora,
                adminIndex=admin_context.admin_index_for_write,
            )

        if plan.deferred_add_only_admin_channel is not None:
            staged_channel, channel_name = plan.deferred_add_only_admin_channel
            logger.info("Adding new channel '%s' to device", channel_name)
            state.written_indices.append(staged_channel.index)
            self._node._write_channel_snapshot(  # noqa: SLF001
                staged_channel,
                adminIndex=admin_context.admin_index_for_write,
            )

    def execute_replace_all(
        self,
        *,
        parsed_input: _SetUrlParsedInput,
        admin_context: _SetUrlAdminContext,
        plan: _SetUrlReplacePlan,
        state: _SetUrlReplaceExecutionState,
    ) -> None:
        """Execute replace-all writes in transactional order."""
        deferred_channel_indexes = {
            channel.index
            for channel in (
                plan.deferred_new_named_admin_channel,
                plan.deferred_previous_admin_slot_channel,
            )
            if channel is not None
        }
        for staged_channel in plan.staged_channels:
            if staged_channel.index in deferred_channel_indexes:
                continue
            logger.debug("Channel i:%s ch:%s", staged_channel.index, staged_channel)
            state.written_channel_indices.append(staged_channel.index)
            self._node._write_channel_snapshot(  # noqa: SLF001
                staged_channel,
                adminIndex=admin_context.admin_index_for_write,
            )
            self._cache_manager.apply_replace_channel_write(staged_channel)

        if parsed_input.has_lora_update:
            set_lora = admin_pb2.AdminMessage()
            set_lora.set_config.lora.CopyFrom(parsed_input.channel_set.lora_config)
            self._node.ensureSessionKey(adminIndex=admin_context.admin_index_for_write)
            state.lora_write_started = True
            self._node._send_admin(  # noqa: SLF001
                set_lora,
                adminIndex=admin_context.admin_index_for_write,
            )
            self._cache_manager.apply_lora_success(parsed_input.channel_set.lora_config)

        if plan.deferred_new_named_admin_channel is not None:
            logger.debug(
                "Channel i:%s ch:%s",
                plan.deferred_new_named_admin_channel.index,
                plan.deferred_new_named_admin_channel,
            )
            state.written_channel_indices.append(
                plan.deferred_new_named_admin_channel.index
            )
            self._node._write_channel_snapshot(  # noqa: SLF001
                plan.deferred_new_named_admin_channel,
                adminIndex=admin_context.admin_index_for_write,
            )
            self._cache_manager.apply_replace_channel_write(
                plan.deferred_new_named_admin_channel
            )
            state.rollback_admin_index_for_write = (
                plan.deferred_new_named_admin_channel.index
            )

        if plan.deferred_previous_admin_slot_channel is not None:
            updated_admin_index_for_write = admin_context.admin_index_for_write
            if plan.deferred_new_named_admin_channel is not None:
                updated_admin_index_for_write = (
                    admin_context.admin_write_node._get_admin_channel_index()  # noqa: SLF001
                )
            logger.debug(
                "Rewriting deferred admin slot i:%s via admin index %s",
                plan.deferred_previous_admin_slot_channel.index,
                updated_admin_index_for_write,
            )
            state.written_channel_indices.append(
                plan.deferred_previous_admin_slot_channel.index
            )
            state.rollback_admin_index_for_write = updated_admin_index_for_write
            self._node._write_channel_snapshot(  # noqa: SLF001
                plan.deferred_previous_admin_slot_channel,
                adminIndex=updated_admin_index_for_write,
            )
            self._cache_manager.apply_replace_channel_write(
                plan.deferred_previous_admin_slot_channel
            )


class _SetUrlRollbackEngine:
    """Owns rollback ordering/admin-index fallback and cache restore/invalidate strategy."""

    def __init__(
        self,
        node: "Node",
        *,
        cache_manager: _SetUrlCacheManager,
    ) -> None:
        self._node = node
        self._cache_manager = cache_manager

    def rollback_add_only(
        self,
        *,
        admin_context: _SetUrlAdminContext,
        plan: _SetUrlAddOnlyPlan,
        state: _SetUrlAddOnlyExecutionState,
    ) -> None:
        """Run best-effort rollback for addOnly transactions."""
        logger.warning(
            "Failed while applying addOnly channel updates; attempting rollback "
            "for written channels and LoRa config.",
            exc_info=True,
        )
        rollback_failed = False
        written_index_set = set(state.written_indices)
        if plan.deferred_add_only_admin_index in written_index_set:
            rollback_admin_indexes = _ordered_admin_indexes(
                plan.deferred_add_only_admin_index,
                admin_context.admin_index_for_write,
            )
        else:
            rollback_admin_indexes = _ordered_admin_indexes(
                admin_context.admin_index_for_write,
                plan.deferred_add_only_admin_index,
            )
        channel_rollback_order: list[int] = []
        if (
            plan.deferred_add_only_admin_index is not None
            and plan.deferred_add_only_admin_index in written_index_set
        ):
            channel_rollback_order.append(plan.deferred_add_only_admin_index)
        for index in reversed(state.written_indices):
            if index not in channel_rollback_order:
                channel_rollback_order.append(index)

        for index in channel_rollback_order:
            rollback_channel = plan.original_channels_by_index.get(index)
            if rollback_channel is None:
                continue
            rollback_succeeded = False
            last_rollback_error: Exception | None = None
            for rollback_admin_index in rollback_admin_indexes:
                try:
                    self._node._write_channel_snapshot(  # noqa: SLF001
                        rollback_channel,
                        adminIndex=rollback_admin_index,
                    )
                    rollback_succeeded = True
                    break
                # Best-effort rollback path; keep attempting remaining steps.
                except (
                    Exception
                ) as rollback_error:  # noqa: BLE001 - best-effort rollback must continue on any rollback send failure
                    last_rollback_error = rollback_error
            if not rollback_succeeded:
                rollback_failed = True
                logger.warning(
                    "Rollback of channel index %s failed after addOnly partial failure.",
                    index,
                    exc_info=(
                        None
                        if last_rollback_error is None
                        else (
                            type(last_rollback_error),
                            last_rollback_error,
                            last_rollback_error.__traceback__,
                        )
                    ),
                )

        if rollback_failed:
            self._cache_manager.invalidate_channel_cache(
                "Channel rollback incomplete after addOnly failure; invalidated local channel cache."
            )

        if state.lora_write_started and plan.original_lora_config is not None:
            rollback_lora = admin_pb2.AdminMessage()
            rollback_lora.set_config.lora.CopyFrom(plan.original_lora_config)
            rollback_lora_succeeded = False
            last_rollback_lora_error: Exception | None = None
            for rollback_admin_index in rollback_admin_indexes:
                try:
                    self._node.ensureSessionKey(adminIndex=rollback_admin_index)
                    self._node._send_admin(  # noqa: SLF001
                        rollback_lora,
                        adminIndex=rollback_admin_index,
                    )
                    self._cache_manager.restore_lora_snapshot(plan.original_lora_config)
                    rollback_lora_succeeded = True
                    break
                # Best-effort rollback path; keep original failure semantics.
                # Preserve original failure while attempting best-effort LoRa rollback.
                except Exception as rollback_lora_error:  # noqa: BLE001
                    last_rollback_lora_error = rollback_lora_error
            if not rollback_lora_succeeded:
                logger.warning(
                    "Rollback of LoRa config failed after addOnly partial failure.",
                    exc_info=(
                        None
                        if last_rollback_lora_error is None
                        else (
                            type(last_rollback_lora_error),
                            last_rollback_lora_error,
                            last_rollback_lora_error.__traceback__,
                        )
                    ),
                )
                self._cache_manager.clear_lora_cache_with_warning(
                    "LoRa config cache cleared after rollback failure; reload config before using localConfig.lora."
                )

    def rollback_replace_all(
        self,
        *,
        admin_context: _SetUrlAdminContext,
        plan: _SetUrlReplacePlan,
        state: _SetUrlReplaceExecutionState,
    ) -> None:
        """Run best-effort rollback for replace-all transactions."""
        logger.warning(
            "Failed while applying replace-all channel updates; attempting rollback "
            "for written channels and LoRa config.",
            exc_info=True,
        )
        rollback_failed = False
        written_index_set = set(state.written_channel_indices)
        if plan.deferred_new_named_admin_index in written_index_set:
            rollback_admin_indexes = _ordered_admin_indexes(
                plan.deferred_new_named_admin_index,
                state.rollback_admin_index_for_write,
                admin_context.admin_index_for_write,
            )
        else:
            rollback_admin_indexes = _ordered_admin_indexes(
                state.rollback_admin_index_for_write,
                admin_context.admin_index_for_write,
                plan.deferred_new_named_admin_index,
            )
        replace_channel_rollback_order: list[int] = []
        if (
            plan.deferred_new_named_admin_index is not None
            and plan.deferred_new_named_admin_index in written_index_set
        ):
            replace_channel_rollback_order.append(plan.deferred_new_named_admin_index)
        for index in reversed(state.written_channel_indices):
            if index not in replace_channel_rollback_order:
                replace_channel_rollback_order.append(index)

        for index in replace_channel_rollback_order:
            replace_rollback_channel = plan.replace_original_channels_by_index.get(
                index
            )
            if replace_rollback_channel is None:
                continue
            rollback_succeeded = False
            replace_last_rollback_error: Exception | None = None
            for rollback_admin_index in rollback_admin_indexes:
                try:
                    self._node._write_channel_snapshot(  # noqa: SLF001
                        replace_rollback_channel,
                        adminIndex=rollback_admin_index,
                    )
                    rollback_succeeded = True
                    break
                except (
                    Exception
                ) as rollback_error:  # noqa: BLE001 - best-effort rollback must continue on any rollback send failure
                    replace_last_rollback_error = rollback_error
            if not rollback_succeeded:
                rollback_failed = True
                logger.warning(
                    "Rollback of channel index %s failed after replace-all partial failure.",
                    index,
                    exc_info=(
                        None
                        if replace_last_rollback_error is None
                        else (
                            type(replace_last_rollback_error),
                            replace_last_rollback_error,
                            replace_last_rollback_error.__traceback__,
                        )
                    ),
                )

        if state.lora_write_started:
            if plan.replace_original_lora_config is not None:
                rollback_lora = admin_pb2.AdminMessage()
                rollback_lora.set_config.lora.CopyFrom(
                    plan.replace_original_lora_config
                )
                rollback_lora_succeeded = False
                replace_last_rollback_lora_error: Exception | None = None
                for rollback_admin_index in rollback_admin_indexes:
                    try:
                        self._node.ensureSessionKey(adminIndex=rollback_admin_index)
                        self._node._send_admin(  # noqa: SLF001
                            rollback_lora,
                            adminIndex=rollback_admin_index,
                        )
                        self._cache_manager.restore_lora_snapshot(
                            plan.replace_original_lora_config
                        )
                        rollback_lora_succeeded = True
                        break
                    # Preserve original failure while attempting best-effort LoRa rollback.
                    except Exception as rollback_lora_error:  # noqa: BLE001
                        replace_last_rollback_lora_error = rollback_lora_error
                if not rollback_lora_succeeded:
                    rollback_failed = True
                    logger.warning(
                        "Rollback of LoRa config failed after replace-all partial failure.",
                        exc_info=(
                            None
                            if replace_last_rollback_lora_error is None
                            else (
                                type(replace_last_rollback_lora_error),
                                replace_last_rollback_lora_error,
                                replace_last_rollback_lora_error.__traceback__,
                            )
                        ),
                    )
                    self._cache_manager.clear_lora_cache_with_warning(
                        "LoRa config cache cleared after rollback failure; reload config before using localConfig.lora."
                    )
            else:
                self._cache_manager.clear_lora_cache_with_warning(
                    "LoRa config cache cleared after replace-all failure without "
                    "rollback snapshot; reload config before using localConfig.lora."
                )

        if rollback_failed:
            self._cache_manager.invalidate_channel_cache(
                "Replace-all rollback incomplete after failure; invalidated local channel cache."
            )
        else:
            self._cache_manager.restore_replace_channels_snapshot(
                plan.replace_original_channels_snapshot
            )


class _SetUrlTransactionCoordinator:
    """Coordinates setURL transaction planning, execution, rollback, and cache policy."""

    def __init__(self, node: "Node", *, parsed_input: _SetUrlParsedInput) -> None:
        self._node = node
        self._parsed_input = parsed_input
        self._admin_context = self._resolve_admin_context()
        self._cache_manager = _SetUrlCacheManager(node)
        self._execution_engine = _SetUrlExecutionEngine(
            node,
            cache_manager=self._cache_manager,
        )
        self._rollback_engine = _SetUrlRollbackEngine(
            node,
            cache_manager=self._cache_manager,
        )

    def _resolve_admin_context(self) -> _SetUrlAdminContext:
        """Capture admin-channel write context before staging any transaction writes."""
        admin_write_node = self._node.iface.localNode
        admin_index_for_write = admin_write_node._get_admin_channel_index()
        named_admin_index_for_write = admin_write_node._get_named_admin_channel_index()
        return _SetUrlAdminContext(
            admin_write_node=admin_write_node,
            admin_index_for_write=admin_index_for_write,
            named_admin_index_for_write=named_admin_index_for_write,
            has_admin_write_node_named_admin=(named_admin_index_for_write is not None),
        )

    def apply_add_only(self) -> None:
        """Execute the addOnly setURL transaction pipeline."""
        planner = _SetUrlAddOnlyPlanner(
            self._node,
            parsed_input=self._parsed_input,
            admin_context=self._admin_context,
        )
        original_lora_config = planner.capture_original_lora_snapshot()
        # Bootstrap admin session using the snapshotted path before staging.
        self._node.ensureSessionKey(
            adminIndex=self._admin_context.admin_index_for_write
        )
        plan = planner.build_plan(original_lora_config=original_lora_config)
        for ignored_name in plan.ignored_channel_names:
            logger.info(
                'Ignoring existing or empty channel "%s" from add URL',
                ignored_name,
            )
        execution_state = _SetUrlAddOnlyExecutionState()
        try:
            self._execution_engine.execute_add_only(
                parsed_input=self._parsed_input,
                admin_context=self._admin_context,
                plan=plan,
                state=execution_state,
            )
        # Intentionally broad: rollback should run for any send failure in this
        # transactional block. The original exception is re-raised.
        except Exception:
            self._rollback_engine.rollback_add_only(
                admin_context=self._admin_context,
                plan=plan,
                state=execution_state,
            )
            raise
        self._cache_manager.apply_add_only_success(plan.channels_to_write)
        if self._parsed_input.has_lora_update:
            self._cache_manager.apply_lora_success(
                self._parsed_input.channel_set.lora_config
            )

    def apply_replace_all(self) -> None:
        """Execute the replace-all setURL transaction pipeline."""
        planner = _SetUrlReplacePlanner(
            self._node,
            parsed_input=self._parsed_input,
            admin_context=self._admin_context,
        )
        plan = planner.build_plan()
        execution_state = _SetUrlReplaceExecutionState(
            rollback_admin_index_for_write=self._admin_context.admin_index_for_write
        )
        try:
            self._execution_engine.execute_replace_all(
                parsed_input=self._parsed_input,
                admin_context=self._admin_context,
                plan=plan,
                state=execution_state,
            )
        except Exception:
            self._rollback_engine.rollback_replace_all(
                admin_context=self._admin_context,
                plan=plan,
                state=execution_state,
            )
            raise


class _NodeContentCacheStore:
    """Owns ringtone/canned-message cache state, fragment storage, and invalidation."""

    def __init__(self, node: "Node") -> None:
        self._node = node

    def get_cached_ringtone(self) -> str | None:
        """Return cached full ringtone value when already present."""
        with self._node._ringtone_lock:  # noqa: SLF001
            if self._node.ringtone:
                logger.debug("ringtone:%s", self._node.ringtone)
                return self._node.ringtone
            return None

    def clear_ringtone_fragment(self) -> None:
        """Clear stale ringtone fragment state before issuing a read request."""
        with self._node._ringtone_lock:  # noqa: SLF001
            self._node.ringtonePart = None

    def store_ringtone_fragment(self, ringtone_fragment: str) -> None:
        """Store ringtone fragment from the latest response packet."""
        with self._node._ringtone_lock:  # noqa: SLF001
            self._node.ringtonePart = ringtone_fragment
        logger.debug("self.ringtonePart:%s", ringtone_fragment)

    def resolve_ringtone_after_read(self) -> str | None:
        """Resolve ringtone result after a read wait by preferring full cache, then fragment."""
        with self._node._ringtone_lock:  # noqa: SLF001
            if self._node.ringtone:
                logger.debug("ringtone:%s", self._node.ringtone)
                return self._node.ringtone
            if self._node.ringtonePart:
                self._node.ringtone = self._node.ringtonePart
                logger.debug("ringtone:%s", self._node.ringtone)
                return self._node.ringtone
            return None

    def invalidate_ringtone_cache(self) -> None:
        """Invalidate ringtone full/fragment cache after writes."""
        with self._node._ringtone_lock:  # noqa: SLF001
            self._node.ringtone = None
            self._node.ringtonePart = None

    def get_cached_canned_message(self) -> str | None:
        """Return cached full canned-message value when already present."""
        with self._node._canned_message_lock:  # noqa: SLF001
            if self._node.cannedPluginMessage:
                logger.debug("canned_plugin_message:%s", self._node.cannedPluginMessage)
                return self._node.cannedPluginMessage
            return None

    def clear_canned_message_fragment(self) -> None:
        """Clear stale canned-message fragment state before issuing a read request."""
        with self._node._canned_message_lock:  # noqa: SLF001
            self._node.cannedPluginMessageMessages = None

    def store_canned_message_fragment(self, canned_messages: str) -> None:
        """Store canned-message fragment payload from response packets."""
        with self._node._canned_message_lock:  # noqa: SLF001
            self._node.cannedPluginMessageMessages = canned_messages
        logger.debug("self.cannedPluginMessageMessages:%s", canned_messages)

    def resolve_canned_message_after_read(self) -> str | None:
        """Resolve canned-message result after a read wait."""
        with self._node._canned_message_lock:  # noqa: SLF001
            if self._node.cannedPluginMessage:
                logger.debug("canned_plugin_message:%s", self._node.cannedPluginMessage)
                return self._node.cannedPluginMessage
            logger.debug(
                "self.cannedPluginMessageMessages:%s",
                self._node.cannedPluginMessageMessages,
            )
            if self._node.cannedPluginMessageMessages:
                self._node.cannedPluginMessage = self._node.cannedPluginMessageMessages
                logger.debug("canned_plugin_message:%s", self._node.cannedPluginMessage)
                return self._node.cannedPluginMessage
            return None

    def invalidate_canned_message_cache(self) -> None:
        """Invalidate canned-message full/fragment cache after writes."""
        with self._node._canned_message_lock:  # noqa: SLF001
            self._node.cannedPluginMessage = None
            self._node.cannedPluginMessageMessages = None


class _NodeContentResponseRuntime:
    """Owns ringtone/canned-message response parsing and fragment cache updates."""

    def __init__(self, node: "Node", *, cache_store: _NodeContentCacheStore) -> None:
        self._node = node
        self._cache_store = cache_store

    @staticmethod
    def _has_routing_error(decoded: dict[str, Any]) -> bool:
        """Return True when decoded routing payload contains a non-NONE error reason."""
        if "routing" in decoded and decoded["routing"]["errorReason"] != "NONE":
            logger.error("Error on response: %s", decoded["routing"]["errorReason"])
            return True
        return False

    def handle_ringtone_response(self, packet: dict[str, Any]) -> None:
        """Parse ringtone response packet and store ringtone fragment when valid."""
        logger.debug("onResponseRequestRingtone() p:%s", packet)
        decoded = packet["decoded"]
        if self._has_routing_error(decoded):
            return
        if "admin" in decoded and "raw" in decoded["admin"]:
            ringtone_part = decoded["admin"]["raw"].get_ringtone_response
            self._cache_store.store_ringtone_fragment(ringtone_part)

    def handle_canned_message_response(self, packet: dict[str, Any]) -> None:
        """Parse canned-message response packet and store payload fragment when valid."""
        logger.debug(
            "onResponseRequestCannedMessagePluginMessageMessages() p:%s", packet
        )
        decoded = packet["decoded"]
        if self._has_routing_error(decoded):
            return
        if "admin" in decoded and "raw" in decoded["admin"]:
            canned_messages = decoded["admin"][
                "raw"
            ].get_canned_message_module_messages_response
            self._cache_store.store_canned_message_fragment(canned_messages)


class _NodeAdminContentRuntime:
    """Owns admin request/wait orchestration for ringtone and canned-message reads/writes."""

    def __init__(
        self,
        node: "Node",
        *,
        cache_store: _NodeContentCacheStore,
        response_runtime: _NodeContentResponseRuntime,
    ) -> None:
        self._node = node
        self._cache_store = cache_store
        self._response_runtime = response_runtime

    def _module_available_or_warn(
        self, excluded_bit: int, warning_message: str
    ) -> bool:
        """Evaluate module availability and emit the legacy warning when unavailable."""
        if not self._node.module_available(excluded_bit):
            logger.warning("%s", warning_message)
            return False
        return True

    def _send_content_read_request(
        self,
        *,
        build_request: Callable[[admin_pb2.AdminMessage], None],
        handle_response: Callable[[dict[str, Any]], None],
        skipped_send_debug_message: str,
        timeout_warning_message: str,
        resolve_result: Callable[[], str | None],
    ) -> str | None:
        """Send content read request, wait for callback, and resolve cached result."""
        response_event = threading.Event()

        def _on_response(packet: dict[str, Any]) -> None:
            try:
                handle_response(packet)
            finally:
                response_event.set()

        request_message = admin_pb2.AdminMessage()
        build_request(request_message)
        request = self._node._send_admin(
            request_message,
            wantResponse=True,
            onResponse=_on_response,
        )
        if request is None:
            logger.debug("%s", skipped_send_debug_message)
            return None
        if not response_event.wait(timeout=self._node._timeout.expireTimeout):
            logger.warning("%s", timeout_warning_message)
            return None
        return resolve_result()

    def _select_write_response_handler(
        self,
    ) -> Callable[[dict[str, Any]], Any] | None:
        """Return legacy ACK callback selection for local-vs-remote writes."""
        if self._node == self._node.iface.localNode:
            return None
        return self._node.onAckNak

    def read_ringtone(self) -> str | None:
        """Read ringtone using cached-short-circuit + request/wait orchestration."""
        logger.debug("in get_ringtone()")
        if not self._module_available_or_warn(
            mesh_pb2.EXTNOTIF_CONFIG,
            "External Notification module not present (excluded by firmware)",
        ):
            return None
        cached_ringtone = self._cache_store.get_cached_ringtone()
        if cached_ringtone is not None:
            return cached_ringtone
        self._cache_store.clear_ringtone_fragment()
        return self._send_content_read_request(
            build_request=lambda message: setattr(
                message, "get_ringtone_request", True
            ),
            handle_response=self._response_runtime.handle_ringtone_response,
            skipped_send_debug_message=(
                "Skipping ringtone wait because protocol send was not started"
            ),
            timeout_warning_message="Timed out waiting for ringtone response",
            resolve_result=self._cache_store.resolve_ringtone_after_read,
        )

    def write_ringtone(self, ringtone: str) -> mesh_pb2.MeshPacket | None:
        """Write ringtone payload and invalidate local ringtone cache."""
        if not self._module_available_or_warn(
            mesh_pb2.EXTNOTIF_CONFIG,
            "External Notification module not present (excluded by firmware)",
        ):
            return None
        if len(ringtone) > MAX_RINGTONE_LENGTH:
            self._node._raise_interface_error(  # noqa: SLF001
                f"The ringtone must be {MAX_RINGTONE_LENGTH} characters or fewer."
            )
        self._node.ensureSessionKey()
        request_message = admin_pb2.AdminMessage()
        request_message.set_ringtone_message = ringtone
        logger.debug("Setting ringtone '%s'", ringtone)
        send_result = self._node._send_admin(
            request_message,
            onResponse=self._select_write_response_handler(),
        )
        self._cache_store.invalidate_ringtone_cache()
        return send_result

    def read_canned_message(self) -> str | None:
        """Read canned-message payload using cached-short-circuit + request/wait orchestration."""
        logger.debug("in get_canned_message()")
        if not self._module_available_or_warn(
            mesh_pb2.CANNEDMSG_CONFIG,
            "Canned Message module not present (excluded by firmware)",
        ):
            return None
        cached_canned_message = self._cache_store.get_cached_canned_message()
        if cached_canned_message is not None:
            return cached_canned_message
        self._cache_store.clear_canned_message_fragment()
        return self._send_content_read_request(
            build_request=lambda message: setattr(
                message,
                "get_canned_message_module_messages_request",
                True,
            ),
            handle_response=self._response_runtime.handle_canned_message_response,
            skipped_send_debug_message=(
                "Skipping canned-message wait because protocol send was not started"
            ),
            timeout_warning_message="Timed out waiting for canned message response",
            resolve_result=self._cache_store.resolve_canned_message_after_read,
        )

    def write_canned_message(self, message: str) -> mesh_pb2.MeshPacket | None:
        """Write canned-message payload and invalidate local canned-message cache."""
        if not self._module_available_or_warn(
            mesh_pb2.CANNEDMSG_CONFIG,
            "Canned Message module not present (excluded by firmware)",
        ):
            return None
        if len(message) > MAX_CANNED_MESSAGE_LENGTH:
            self._node._raise_interface_error(  # noqa: SLF001
                f"The canned message must be {MAX_CANNED_MESSAGE_LENGTH} characters or fewer."
            )
        self._node.ensureSessionKey()
        request_message = admin_pb2.AdminMessage()
        request_message.set_canned_message_module_messages = message
        logger.debug("Setting canned message '%s'", message)
        send_result = self._node._send_admin(
            request_message,
            onResponse=self._select_write_response_handler(),
        )
        self._cache_store.invalidate_canned_message_cache()
        return send_result


class _NodeMetadataResponseRuntime:
    """Owns metadata response routing/error handling, state mutation, and signaling."""

    def __init__(self, node: "Node") -> None:
        self._node = node

    def _handle_routing_portnum(self, decoded: dict[str, Any]) -> bool:
        """Handle ROUTING_APP metadata responses and indicate whether processing is complete."""
        if decoded["portnum"] != portnums_pb2.PortNum.Name(
            portnums_pb2.PortNum.ROUTING_APP
        ):
            return False
        if decoded["routing"]["errorReason"] != "NONE":
            logger.warning(
                "Metadata request failed, error reason: %s",
                decoded["routing"]["errorReason"],
            )
            self._node.iface._acknowledgment.receivedNak = True
            self._node._timeout.expireTime = time.time()  # Do not wait any longer
            self._node._signal_metadata_stdout_event()
            return True  # Don't try to parse this routing message
        logger.debug(
            "Metadata request routed successfully; waiting for ADMIN_APP payload."
        )
        return True

    def _handle_generic_routing_error(self, decoded: dict[str, Any]) -> bool:
        """Handle non-routing-port metadata errors and indicate completion."""
        if "routing" in decoded and decoded["routing"]["errorReason"] != "NONE":
            logger.error("Error on response: %s", decoded["routing"]["errorReason"])
            self._node.iface._acknowledgment.receivedNak = True
            self._node._signal_metadata_stdout_event()
            return True
        return False

    def _emit_metadata_lines(self, metadata: mesh_pb2.DeviceMetadata) -> None:
        """Emit metadata lines with historical formatting and enum fallback behavior."""
        self._node._emit_metadata_line(
            f"\nfirmware_version: {metadata.firmware_version}"
        )
        self._node._emit_metadata_line(
            f"device_state_version: {metadata.device_state_version}"
        )
        if metadata.role in config_pb2.Config.DeviceConfig.Role.values():
            self._node._emit_metadata_line(
                f"role: {config_pb2.Config.DeviceConfig.Role.Name(metadata.role)}"
            )
        else:
            self._node._emit_metadata_line(f"role: {metadata.role}")
        self._node._emit_metadata_line(
            f"position_flags: {self._node.position_flags_list(metadata.position_flags)}"
        )
        if metadata.hw_model in mesh_pb2.HardwareModel.values():
            self._node._emit_metadata_line(
                f"hw_model: {mesh_pb2.HardwareModel.Name(metadata.hw_model)}"
            )
        else:
            self._node._emit_metadata_line(f"hw_model: {metadata.hw_model}")
        self._node._emit_metadata_line(f"hasPKC: {metadata.hasPKC}")
        if metadata.excluded_modules > 0:
            self._node._emit_metadata_line(
                f"excluded_modules: {self._node.excluded_modules_list(metadata.excluded_modules)}"
            )

    def handle_metadata_response(self, packet: dict[str, Any]) -> None:
        """Process metadata response packet and preserve historical ACK/timeout semantics."""
        logger.debug("onRequestGetMetadata() p:%s", packet)
        decoded = packet["decoded"]
        if self._handle_routing_portnum(decoded):
            return
        if self._handle_generic_routing_error(decoded):
            return

        self._node.iface._acknowledgment.receivedAck = True
        metadata_response = decoded["admin"]["raw"].get_device_metadata_response
        metadata_snapshot = mesh_pb2.DeviceMetadata()
        metadata_snapshot.CopyFrom(metadata_response)
        self._node._set_metadata_snapshot(metadata_snapshot)
        self._node._timeout.reset()  # We made forward progress
        logger.debug("Received metadata %s", stripnl(metadata_response))
        self._emit_metadata_lines(metadata_response)
        self._node._signal_metadata_stdout_event()


class _NodeChannelResponseRuntime:
    """Owns channel-response routing/error handling, sequencing, and final installation."""

    def __init__(self, node: "Node") -> None:
        self._node = node

    def _handle_routing_response(self, decoded: dict[str, Any]) -> bool:
        """Handle ROUTING_APP channel responses and indicate whether processing is complete."""
        if decoded["portnum"] != portnums_pb2.PortNum.Name(
            portnums_pb2.PortNum.ROUTING_APP
        ):
            return False
        if decoded["routing"]["errorReason"] != "NONE":
            logger.warning(
                "Channel request failed, error reason: %s",
                decoded["routing"]["errorReason"],
            )
            self._node._timeout.expireTime = time.time()  # Do not wait any longer
            return True  # Don't try to parse this routing message

        last_tried = 0
        with self._node._channels_lock:  # noqa: SLF001
            if self._node.partialChannels:
                last_tried = self._node.partialChannels[-1].index
        logger.debug("Retrying previous channel request.")
        self._node._request_channel(last_tried)  # noqa: SLF001
        return True

    def handle_channel_response(self, packet: dict[str, Any]) -> None:
        """Process channel response packet and maintain partial/final channel sequencing."""
        logger.debug("onResponseRequestChannel() p:%s", packet)
        decoded = packet["decoded"]
        if self._handle_routing_response(decoded):
            return

        response_channel = decoded["admin"]["raw"].get_channel_response
        channel_response = channel_pb2.Channel()
        channel_response.CopyFrom(response_channel)
        with self._node._channels_lock:  # noqa: SLF001
            self._node.partialChannels.append(channel_response)
        self._node._timeout.reset()  # We made forward progress
        logger.debug("Received channel %s", stripnl(channel_response))
        index = channel_response.index

        if index >= MAX_CHANNELS - 1:
            logger.debug("Finished downloading channels")
            with self._node._channels_lock:  # noqa: SLF001
                self._node.channels = list(self._node.partialChannels)
                self._node._fixup_channels_locked()  # noqa: SLF001
            return
        self._node._request_channel(index + 1)  # noqa: SLF001


class _NodeAdminSessionRuntime:
    """Owns admin-session readiness checks and session-key request behavior."""

    def __init__(self, node: "Node") -> None:
        self._node = node

    def ensure_session_key(self, *, admin_index: int | None = None) -> None:
        """Ensure session key is present, preserving historical noProto behavior."""
        if self._node.noProto:
            logger.warning(
                "Not ensuring session key, because protocol use is disabled by noProto"
            )
            return
        if (
            self._node.iface._get_or_create_by_num(self._node.nodeNum).get(
                "adminSessionPassKey"
            )
            is None
        ):
            self._node.requestConfig(
                admin_pb2.AdminMessage.SESSIONKEY_CONFIG,
                adminIndex=admin_index,
            )


class _NodeAdminTransportRuntime:
    """Owns admin transport mechanics for ADMIN_APP sends."""

    def __init__(self, node: "Node") -> None:
        self._node = node

    def _resolve_admin_index(self, admin_index: int | None) -> int:
        """Resolve None to auto-detected admin channel index; preserve explicit zero."""
        if admin_index is None:
            return self._node.iface.localNode._get_admin_channel_index()
        return admin_index

    def send_admin(
        self,
        message: admin_pb2.AdminMessage,
        *,
        want_response: bool = False,
        on_response: Callable[[dict[str, Any]], Any] | None = None,
        admin_index: int | None = None,
    ) -> mesh_pb2.MeshPacket | None:
        """Send an AdminMessage via iface.sendData with preserved transport semantics."""
        if self._node.noProto:
            logger.warning(
                "Not sending packet because protocol use is disabled by noProto"
            )
            return None

        resolved_admin_index = self._resolve_admin_index(admin_index)
        logger.debug("adminIndex:%s", resolved_admin_index)

        node_info = self._node.iface._get_or_create_by_num(self._node.nodeNum)
        passkey = node_info.get("adminSessionPassKey")
        if isinstance(passkey, bytes):
            message.session_passkey = passkey

        return self._node.iface.sendData(
            message,
            self._node.nodeNum,
            portNum=portnums_pb2.PortNum.ADMIN_APP,
            wantAck=True,
            wantResponse=want_response,
            onResponse=on_response,
            channelIndex=resolved_admin_index,
            pkiEncrypted=True,
        )


class _NodeChannelWriteRuntime:
    """Owns channel-snapshot writes and writeChannel orchestration."""

    def __init__(
        self,
        node: "Node",
        *,
        admin_session_runtime: _NodeAdminSessionRuntime,
        admin_transport_runtime: _NodeAdminTransportRuntime,
    ) -> None:
        self._node = node
        self._admin_session_runtime = admin_session_runtime
        self._admin_transport_runtime = admin_transport_runtime

    def write_channel_snapshot(
        self,
        channel_to_write: channel_pb2.Channel,
        *,
        admin_index: int | None = None,
    ) -> None:
        """Send a pre-built channel snapshot to the device."""
        # Keep compatibility for callers/tests that patch Node facade methods.
        # Runtime ownership remains here, but transport/session side effects still
        # flow through the historical Node entry points.
        self._node.ensureSessionKey(adminIndex=admin_index)
        request_message = admin_pb2.AdminMessage()
        request_message.set_channel.CopyFrom(channel_to_write)
        self._node._send_admin(
            request_message,
            adminIndex=admin_index,
        )
        logger.debug("Wrote channel %s", channel_to_write.index)

    def write_channel(self, channel_index: int, *, admin_index: int | None = None) -> None:
        """Validate and write one channel by index using snapshot semantics."""
        with self._node._channels_lock:  # noqa: SLF001
            channels = self._node.channels
            if channels is None:
                self._node._raise_interface_error("Error: No channels have been read")  # noqa: SLF001
            if channel_index < 0 or channel_index >= len(channels):
                self._node._raise_interface_error(  # noqa: SLF001
                    f"Channel index {channel_index} out of range (0-{len(channels) - 1})"
                )
            channel_snapshot = channel_pb2.Channel()
            channel_snapshot.CopyFrom(channels[channel_index])
        self.write_channel_snapshot(
            channel_snapshot,
            admin_index=admin_index,
        )


@dataclass(frozen=True)
class _DeleteChannelRewritePlan:
    """Delete-channel rewrite execution plan."""

    pre_delete_admin_index: int
    post_delete_admin_index: int
    switch_after_admin_slot_rewrite: bool
    channels_to_rewrite: list[channel_pb2.Channel]


class _NodeDeleteChannelRuntime:
    """Owns delete-channel validation, planning, and ordered rewrite execution."""

    def __init__(self, node: "Node", *, channel_write_runtime: _NodeChannelWriteRuntime):
        self._node = node
        self._channel_write_runtime = channel_write_runtime

    @staticmethod
    def _named_admin_index_from_channels(
        channel_list: list[channel_pb2.Channel],
    ) -> int:
        for channel in channel_list:
            if (
                channel.role != channel_pb2.Channel.Role.DISABLED
                and channel.settings
                and _is_named_admin_channel_name(channel.settings.name)
            ):
                return channel.index
        return 0

    def _build_rewrite_plan(self, channel_index: int) -> _DeleteChannelRewritePlan:
        """Build lock-scoped delete/rewrite plan with pre/post admin indexes."""
        with self._node._channels_lock:  # noqa: SLF001
            channels = self._node.channels
            if channels is None:
                self._node._raise_interface_error("Error: No channels have been read")  # noqa: SLF001
            if channel_index < 0 or channel_index >= len(channels):
                self._node._raise_interface_error(  # noqa: SLF001
                    f"Channel index {channel_index} out of range (0-{len(channels) - 1})"
                )

            channel_to_delete = channels[channel_index]
            if channel_to_delete.role not in (
                channel_pb2.Channel.Role.SECONDARY,
                channel_pb2.Channel.Role.DISABLED,
            ):
                self._node._raise_interface_error(  # noqa: SLF001
                    "Only SECONDARY or DISABLED channels can be deleted"
                )

            is_local_node = self._node.iface.localNode == self._node
            if is_local_node:
                pre_delete_admin_index = self._named_admin_index_from_channels(channels)
            else:
                pre_delete_admin_index = self._node.iface.localNode.getAdminChannelIndex()

            # If we move the "admin" channel, the index used for admin writes
            # will need to be recomputed as writes progress.
            channels.pop(channel_index)
            self._node._fixup_channels_locked()  # noqa: SLF001

            channels_to_rewrite: list[channel_pb2.Channel] = []
            for rewrite_index in range(channel_index, MAX_CHANNELS):
                channel_snapshot = channel_pb2.Channel()
                channel_snapshot.CopyFrom(channels[rewrite_index])
                channels_to_rewrite.append(channel_snapshot)

            if is_local_node:
                post_delete_admin_index = self._named_admin_index_from_channels(channels)
            else:
                post_delete_admin_index = self._node.iface.localNode.getAdminChannelIndex()

        return _DeleteChannelRewritePlan(
            pre_delete_admin_index=pre_delete_admin_index,
            post_delete_admin_index=post_delete_admin_index,
            switch_after_admin_slot_rewrite=(pre_delete_admin_index >= channel_index),
            channels_to_rewrite=channels_to_rewrite,
        )

    def _execute_rewrite_plan(self, plan: _DeleteChannelRewritePlan) -> None:
        """Execute channel rewrites while preserving historical admin-index switch timing."""
        admin_index_for_write = plan.pre_delete_admin_index
        for channel_snapshot in plan.channels_to_rewrite:
            self._channel_write_runtime.write_channel_snapshot(
                channel_snapshot,
                admin_index=admin_index_for_write,
            )
            if (
                plan.switch_after_admin_slot_rewrite
                and channel_snapshot.index == plan.pre_delete_admin_index
            ):
                admin_index_for_write = plan.post_delete_admin_index

    def delete_channel(self, channel_index: int) -> None:
        """Delete one channel and execute ordered rewrite plan."""
        rewrite_plan = self._build_rewrite_plan(channel_index)
        self._execute_rewrite_plan(rewrite_plan)


class _NodeAckNakRuntime:
    """Owns ACK/NAK payload classification and acknowledgment flag mutation."""

    def __init__(self, node: "Node") -> None:
        self._node = node

    def handle_ack_nak(self, packet: dict[str, Any]) -> None:
        """Classify ACK/NAK payload and update interface acknowledgment state."""
        decoded = packet.get("decoded", {})
        routing = decoded.get("routing")
        if not isinstance(routing, dict):
            logger.warning(
                "Received ACK/NAK response without routing details: %s", packet
            )
            return

        error_reason = routing.get("errorReason", "NONE")
        if error_reason != "NONE":
            logger.warning("Received a NAK, error reason: %s", error_reason)
            self._node.iface._acknowledgment.receivedNak = True
            return

        from_value = packet.get("from")
        if from_value is None:
            logger.warning("Received ACK/NAK response without sender: %s", packet)
            return
        try:
            from_num = int(from_value)
        except (TypeError, ValueError):
            logger.warning("Received ACK/NAK response with invalid sender: %s", packet)
            return

        if from_num == self._node.iface.localNode.nodeNum:
            logger.info(
                "Received an implicit ACK. Packet will likely arrive, but cannot be guaranteed."
            )
            self._node.iface._acknowledgment.receivedImplAck = True
            return

        logger.info("Received an ACK.")
        self._node.iface._acknowledgment.receivedAck = True


class _NodeSettingsMessageBuilder:
    """Owns settings request/write AdminMessage construction and field mapping."""

    def __init__(self, node: "Node") -> None:
        self._node = node

    def build_request_message(
        self, config_type: int | FieldDescriptor
    ) -> admin_pb2.AdminMessage:
        """Build request-config message from int or protobuf field descriptor."""
        message = admin_pb2.AdminMessage()
        if isinstance(config_type, int):
            message.get_config_request = config_type  # type: ignore[assignment] # pyright: ignore[reportAttributeAccessIssue]
            return message

        if config_type.containing_type.name == "LocalConfig":
            message.get_config_request = admin_pb2.AdminMessage.ConfigType.Value(
                f"{config_type.name.upper()}_CONFIG"
            )
            return message

        message.get_module_config_request = (
            config_type.index  # pyright: ignore[reportAttributeAccessIssue]
        )
        return message

    def _write_config_dispatch(self) -> dict[str, tuple[str, Any]]:
        """Return config-name mapping to (setter oneof, source config message)."""
        node = self._node
        return {
            "device": ("set_config", node.localConfig.device),
            "position": ("set_config", node.localConfig.position),
            "power": ("set_config", node.localConfig.power),
            "network": ("set_config", node.localConfig.network),
            "display": ("set_config", node.localConfig.display),
            "lora": ("set_config", node.localConfig.lora),
            "bluetooth": ("set_config", node.localConfig.bluetooth),
            "security": ("set_config", node.localConfig.security),
            "mqtt": ("set_module_config", node.moduleConfig.mqtt),
            "serial": ("set_module_config", node.moduleConfig.serial),
            "external_notification": (
                "set_module_config",
                node.moduleConfig.external_notification,
            ),
            "store_forward": ("set_module_config", node.moduleConfig.store_forward),
            "range_test": ("set_module_config", node.moduleConfig.range_test),
            "telemetry": ("set_module_config", node.moduleConfig.telemetry),
            "canned_message": ("set_module_config", node.moduleConfig.canned_message),
            "audio": ("set_module_config", node.moduleConfig.audio),
            "remote_hardware": (
                "set_module_config",
                node.moduleConfig.remote_hardware,
            ),
            "neighbor_info": ("set_module_config", node.moduleConfig.neighbor_info),
            "detection_sensor": (
                "set_module_config",
                node.moduleConfig.detection_sensor,
            ),
            "ambient_lighting": (
                "set_module_config",
                node.moduleConfig.ambient_lighting,
            ),
            "paxcounter": ("set_module_config", node.moduleConfig.paxcounter),
            "traffic_management": (
                "set_module_config",
                node.moduleConfig.traffic_management,
            ),
        }

    def build_write_message(self, config_name: str) -> admin_pb2.AdminMessage:
        """Build one set_config/set_module_config message for a config name."""
        config_entry = self._write_config_dispatch().get(config_name)
        if config_entry is None:
            self._node._raise_interface_error(  # noqa: SLF001
                f"Error: No valid config with name {config_name}"
            )

        message = admin_pb2.AdminMessage()
        setter_name, source_config = config_entry
        config_setter = getattr(message, setter_name)
        getattr(config_setter, config_name).CopyFrom(source_config)
        return message


class _NodeSettingsRuntime:
    """Owns settings request/write orchestration and callback policy."""

    def __init__(
        self,
        node: "Node",
        *,
        message_builder: _NodeSettingsMessageBuilder,
    ) -> None:
        self._node = node
        self._message_builder = message_builder

    def request_config(
        self,
        config_type: int | FieldDescriptor,
        *,
        admin_index: int | None = None,
    ) -> None:
        """Send one settings request with preserved local/remote wait semantics."""
        if self._node == self._node.iface.localNode:
            on_response: Callable[[dict[str, Any]], Any] | None = None
        else:
            on_response = self._node.onResponseRequestSettings
            logger.info(
                "Requesting current config from remote node (this can take a while)."
            )

        message = self._message_builder.build_request_message(config_type)
        self._node._send_admin(
            message,
            wantResponse=True,
            onResponse=on_response,
            adminIndex=admin_index,
        )
        if on_response is not None:
            self._node.iface.waitForAckNak()

    def _validate_write_configs_loaded(self) -> None:
        """Preserve historical loaded-state precondition for writeConfig."""
        if (
            len(self._node.localConfig.ListFields()) == 0
            and len(self._node.moduleConfig.ListFields()) == 0
        ):
            self._node._raise_interface_error(  # noqa: SLF001
                "Error: No localConfig has been read. "
                "Request config from the device before writing."
            )

    def write_config(self, config_name: str) -> None:
        """Send one settings write with preserved callback selection."""
        message = self._message_builder.build_write_message(config_name)
        self._validate_write_configs_loaded()
        logger.debug("Wrote: %s", config_name)
        on_response = None if self._node == self._node.iface.localNode else self._node.onAckNak
        self._node._send_admin(message, onResponse=on_response)


class _NodeSettingsResponseRuntime:
    """Owns onResponseRequestSettings routing, decode, mutation, and output behavior."""

    def __init__(self, node: "Node") -> None:
        self._node = node

    def _resolve_local_config_target(
        self, admin_message: dict[str, Any]
    ) -> tuple[str, str, Any] | None:
        """Resolve getConfigResponse payload target, if present and valid."""
        response = admin_message.get("getConfigResponse")
        if response is None:
            return None
        if not response:
            logger.warning("Received empty config response from node.")
            return None
        response_field = next(iter(response.keys()))
        field_name = camel_to_snake(response_field)
        config_type = self._node.localConfig.DESCRIPTOR.fields_by_name.get(field_name)
        if config_type is None:
            logger.warning(
                "Ignoring unknown LocalConfig field in getConfigResponse: %s",
                field_name,
            )
            return None
        return "get_config_response", field_name, getattr(
            self._node.localConfig, config_type.name
        )

    def _resolve_module_config_target(
        self, admin_message: dict[str, Any]
    ) -> tuple[str, str, Any] | None:
        """Resolve getModuleConfigResponse payload target, if present and valid."""
        response = admin_message.get("getModuleConfigResponse")
        if response is None:
            return None
        if not response:
            logger.warning("Received empty module config response from node.")
            return None
        response_field = next(iter(response.keys()))
        field_name = camel_to_snake(response_field)
        config_type = self._node.moduleConfig.DESCRIPTOR.fields_by_name.get(field_name)
        if config_type is None:
            logger.warning(
                "Ignoring unknown ModuleConfig field in getModuleConfigResponse: %s",
                field_name,
            )
            return None
        return "get_module_config_response", field_name, getattr(
            self._node.moduleConfig, config_type.name
        )

    def _resolve_config_target(
        self,
        admin_message: dict[str, Any],
    ) -> tuple[str, str, Any] | None:
        """Resolve response type/field to destination config submessage."""
        local_target = self._resolve_local_config_target(admin_message)
        if local_target is not None:
            return local_target

        module_target = self._resolve_module_config_target(admin_message)
        if module_target is not None:
            return module_target

        logger.warning(
            "Did not receive a valid response. Make sure to have a shared channel named 'admin'."
        )
        return None

    def handle_settings_response(self, packet: dict[str, Any]) -> None:
        """Process one settings response packet with preserved ACK/NAK semantics."""
        logger.debug("onResponseRequestSetting() p:%s", packet)
        decoded = packet["decoded"]
        if "routing" in decoded:
            if decoded["routing"]["errorReason"] != "NONE":
                logger.error(
                    "Error on response: %s",
                    decoded["routing"]["errorReason"],
                )
                self._node.iface._acknowledgment.receivedNak = True
            return

        self._node.iface._acknowledgment.receivedAck = True
        admin_message = decoded["admin"]
        target = self._resolve_config_target(admin_message)
        if target is None:
            return

        oneof, field_name, config_values = target
        raw_config = getattr(getattr(admin_message["raw"], oneof), field_name)
        config_values.CopyFrom(raw_config)
        logger.info("%s:\n%s", field_name, config_values)


class _NodeAdminCommandRuntime:
    """Owns generic admin-command session/callback/send policy for command family methods."""

    def __init__(self, node: "Node") -> None:
        self._node = node

    def _select_remote_ack_callback(self) -> Callable[[dict[str, Any]], Any] | None:
        """Return callback policy used by remote admin command sends."""
        if self._node == self._node.iface.localNode:
            return None
        return self._node.onAckNak

    def _send_command(
        self,
        message: admin_pb2.AdminMessage,
        *,
        ensure_session_key: bool,
        use_remote_ack_callback: bool,
    ) -> mesh_pb2.MeshPacket | None:
        """Apply shared session/callback/send policy for admin commands."""
        if ensure_session_key:
            self._node.ensureSessionKey()
        on_response = (
            self._select_remote_ack_callback() if use_remote_ack_callback else None
        )
        return self._node._send_admin(message, onResponse=on_response)

    def send_owner_message(
        self, message: admin_pb2.AdminMessage
    ) -> mesh_pb2.MeshPacket | None:
        """Send set_owner message with historical session and remote-ACK behavior."""
        return self._send_command(
            message,
            ensure_session_key=True,
            use_remote_ack_callback=True,
        )

    def exit_simulator(self) -> mesh_pb2.MeshPacket | None:
        """Send exit-simulator admin command."""
        message = admin_pb2.AdminMessage()
        message.exit_simulator = True
        logger.debug("in exitSimulator()")
        return self._send_command(
            message,
            ensure_session_key=True,
            use_remote_ack_callback=False,
        )

    def reboot(self, secs: int) -> mesh_pb2.MeshPacket | None:
        """Send reboot command with delayed reboot seconds."""
        message = admin_pb2.AdminMessage()
        message.reboot_seconds = secs
        logger.info("Telling node to reboot in %s seconds", secs)
        return self._send_command(
            message,
            ensure_session_key=True,
            use_remote_ack_callback=True,
        )

    def begin_settings_transaction(self) -> mesh_pb2.MeshPacket | None:
        """Send begin-edit-settings command."""
        message = admin_pb2.AdminMessage()
        message.begin_edit_settings = True
        logger.info("Telling node to open a transaction to edit settings")
        return self._send_command(
            message,
            ensure_session_key=True,
            use_remote_ack_callback=True,
        )

    def commit_settings_transaction(self) -> mesh_pb2.MeshPacket | None:
        """Send commit-edit-settings command."""
        message = admin_pb2.AdminMessage()
        message.commit_edit_settings = True
        logger.info("Telling node to commit open transaction for editing settings")
        return self._send_command(
            message,
            ensure_session_key=True,
            use_remote_ack_callback=True,
        )

    def reboot_ota(self, secs: int) -> mesh_pb2.MeshPacket | None:
        """Send reboot-to-OTA command."""
        message = admin_pb2.AdminMessage()
        message.reboot_ota_seconds = secs
        logger.info("Telling node to reboot to OTA in %s seconds", secs)
        return self._send_command(
            message,
            ensure_session_key=True,
            use_remote_ack_callback=True,
        )

    def start_ota(
        self,
        mode: admin_pb2.OTAMode.ValueType | None = None,
        ota_file_hash: bytes | None = None,
        *,
        ota_mode: admin_pb2.OTAMode.ValueType | None = None,
        ota_hash: bytes | None = None,
        extra_kwargs: dict[str, Any],
    ) -> mesh_pb2.MeshPacket | None:
        """Validate OTA args and send ota_request command."""
        if self._node != self._node.iface.localNode:
            self._node._raise_interface_error("startOTA only possible on local node")  # noqa: SLF001

        # COMPAT_STABLE_SHIM: support legacy keyword aliases used by older callers:
        # `ota_mode` -> `mode`, and `ota_hash`/`hash` -> `ota_file_hash`.
        legacy_hash = extra_kwargs.pop("hash", None)
        if extra_kwargs:
            unexpected = ", ".join(sorted(extra_kwargs))
            raise TypeError(
                f"startOTA() got unexpected keyword argument(s): {unexpected}"
            )

        if mode is not None and ota_mode is not None and mode != ota_mode:
            raise ValueError("Conflicting OTA mode arguments provided")
        resolved_mode = mode if mode is not None else ota_mode
        if resolved_mode is None:
            raise TypeError("startOTA() missing required argument: 'mode'")

        hash_values = {
            value
            for value in (ota_file_hash, ota_hash, legacy_hash)
            if value is not None
        }
        if not hash_values:
            raise TypeError("startOTA() missing required argument: 'ota_file_hash'")
        if len(hash_values) > 1:
            raise ValueError("Conflicting OTA hash arguments provided")
        resolved_hash = hash_values.pop()
        if not isinstance(resolved_hash, bytes):
            raise TypeError("ota_file_hash must be bytes")

        message = admin_pb2.AdminMessage()
        message.ota_request.reboot_ota_mode = resolved_mode
        message.ota_request.ota_hash = resolved_hash
        return self._send_command(
            message,
            ensure_session_key=True,
            use_remote_ack_callback=False,
        )

    def enter_dfu_mode(self) -> mesh_pb2.MeshPacket | None:
        """Send enter-DFU-mode command."""
        message = admin_pb2.AdminMessage()
        message.enter_dfu_mode_request = True
        logger.info("Telling node to enable DFU mode")
        return self._send_command(
            message,
            ensure_session_key=True,
            use_remote_ack_callback=True,
        )

    def shutdown(self, secs: int) -> mesh_pb2.MeshPacket | None:
        """Send shutdown command with delayed shutdown seconds."""
        message = admin_pb2.AdminMessage()
        message.shutdown_seconds = secs
        logger.info("Telling node to shutdown in %s seconds", secs)
        return self._send_command(
            message,
            ensure_session_key=True,
            use_remote_ack_callback=True,
        )

    def factory_reset(self, *, full: bool) -> mesh_pb2.MeshPacket | None:
        """Send factory-reset command, preserving full/config split behavior."""
        message = admin_pb2.AdminMessage()
        if full:
            message.factory_reset_device = FACTORY_RESET_REQUEST_VALUE
            logger.info("Telling node to factory reset (full device reset)")
        else:
            message.factory_reset_config = FACTORY_RESET_REQUEST_VALUE
            logger.info("Telling node to factory reset (config reset)")
        return self._send_command(
            message,
            ensure_session_key=True,
            use_remote_ack_callback=True,
        )

    def _send_node_id_command(
        self,
        *,
        node_id: int | str,
        set_field: Callable[[admin_pb2.AdminMessage, int], None],
    ) -> mesh_pb2.MeshPacket | None:
        """Send one node-id command after toNodeNum conversion."""
        node_num = toNodeNum(node_id)
        message = admin_pb2.AdminMessage()
        set_field(message, node_num)
        return self._send_command(
            message,
            ensure_session_key=True,
            use_remote_ack_callback=True,
        )

    def remove_node(self, node_id: int | str) -> mesh_pb2.MeshPacket | None:
        """Send remove-by-nodenum command."""
        return self._send_node_id_command(
            node_id=node_id,
            set_field=lambda message, node_num: setattr(
                message, "remove_by_nodenum", node_num
            ),
        )

    def set_favorite(self, node_id: int | str) -> mesh_pb2.MeshPacket | None:
        """Send set-favorite command."""
        return self._send_node_id_command(
            node_id=node_id,
            set_field=lambda message, node_num: setattr(
                message, "set_favorite_node", node_num
            ),
        )

    def remove_favorite(self, node_id: int | str) -> mesh_pb2.MeshPacket | None:
        """Send remove-favorite command."""
        return self._send_node_id_command(
            node_id=node_id,
            set_field=lambda message, node_num: setattr(
                message, "remove_favorite_node", node_num
            ),
        )

    def set_ignored(self, node_id: int | str) -> mesh_pb2.MeshPacket | None:
        """Send set-ignored command."""
        return self._send_node_id_command(
            node_id=node_id,
            set_field=lambda message, node_num: setattr(
                message, "set_ignored_node", node_num
            ),
        )

    def remove_ignored(self, node_id: int | str) -> mesh_pb2.MeshPacket | None:
        """Send remove-ignored command."""
        return self._send_node_id_command(
            node_id=node_id,
            set_field=lambda message, node_num: setattr(
                message, "remove_ignored_node", node_num
            ),
        )

    def reset_node_db(self) -> mesh_pb2.MeshPacket | None:
        """Send NodeDB reset command."""
        message = admin_pb2.AdminMessage()
        message.nodedb_reset = True
        logger.info("Telling node to reset the NodeDB")
        return self._send_command(
            message,
            ensure_session_key=True,
            use_remote_ack_callback=True,
        )


class _NodeOwnerProfileRuntime:
    """Owns setOwner validation/truncation/message-build and send orchestration."""

    def __init__(
        self,
        node: "Node",
        *,
        admin_command_runtime: _NodeAdminCommandRuntime,
    ) -> None:
        self._node = node
        self._admin_command_runtime = admin_command_runtime

    def set_owner(
        self,
        *,
        long_name: str | None = None,
        short_name: str | None = None,
        is_licensed: bool = False,
        is_unmessagable: bool | None = None,
    ) -> mesh_pb2.MeshPacket | None:
        """Apply setOwner validation/truncation and send policy."""
        logger.debug("in setOwner nodeNum:%s", self._node.nodeNum)
        message = admin_pb2.AdminMessage()

        if long_name is not None:
            long_name = long_name.strip()
            # Validate that long_name is not empty or whitespace-only
            if not long_name:
                self._node._raise_interface_error(EMPTY_LONG_NAME_MSG)  # noqa: SLF001
            if len(long_name) > MAX_LONG_NAME_LEN:
                long_name = long_name[:MAX_LONG_NAME_LEN]
                logger.warning(
                    "Long name is longer than %s characters, truncating to '%s'",
                    MAX_LONG_NAME_LEN,
                    long_name,
                )
            message.set_owner.long_name = long_name
            message.set_owner.is_licensed = is_licensed

        if short_name is not None:
            short_name = short_name.strip()
            # Validate that short_name is not empty or whitespace-only
            if not short_name:
                self._node._raise_interface_error(EMPTY_SHORT_NAME_MSG)  # noqa: SLF001
            if len(short_name) > MAX_SHORT_NAME_LEN:
                short_name = short_name[:MAX_SHORT_NAME_LEN]
                logger.warning(
                    "Short name is longer than %s characters, truncating to '%s'",
                    MAX_SHORT_NAME_LEN,
                    short_name,
                )
            message.set_owner.short_name = short_name

        if is_unmessagable is not None:
            message.set_owner.is_unmessagable = is_unmessagable

        # Note: These debug lines are used in unit tests
        logger.debug("p.set_owner.long_name:%s:", message.set_owner.long_name)
        logger.debug("p.set_owner.short_name:%s:", message.set_owner.short_name)
        logger.debug("p.set_owner.is_licensed:%s:", message.set_owner.is_licensed)
        logger.debug(
            "p.set_owner.is_unmessagable:%s:",
            message.set_owner.is_unmessagable,
        )
        return self._admin_command_runtime.send_owner_message(message)


class Node:  # pylint: disable=too-many-instance-attributes
    """A model of a (local or remote) node in the mesh.

    Includes methods for localConfig, moduleConfig and channels
    """

    def __init__(
        self,
        iface: "MeshInterface",
        nodeNum: int | str,
        noProto: bool = False,
        timeout: float = 300.0,
    ) -> None:
        """Create and initialize a Node instance that holds configuration, channel state, and runtime flags for a mesh node.

        Parameters
        ----------
        iface : 'MeshInterface'
            Interface used for network I/O and device interactions.
        nodeNum : int | str
            Node identifier (numeric or string convertible to a node number).
        noProto : bool
            If True, protocol-based operations are disabled for this node. (Default value = False)
        timeout : float
            Maximum seconds used for operations that wait for responses. (Default value = 300.0)
        """
        self.iface = iface
        self.nodeNum = toNodeNum(nodeNum) if isinstance(nodeNum, str) else nodeNum
        self.localConfig = localonly_pb2.LocalConfig()
        self.moduleConfig = localonly_pb2.LocalModuleConfig()
        self.channels: list[channel_pb2.Channel] | None = None
        self._channels_lock = threading.RLock()
        self._timeout = Timeout(maxSecs=timeout)
        self.partialChannels: list[channel_pb2.Channel] = []
        self.noProto = noProto
        self.cannedPluginMessage: str | None = None
        self.cannedPluginMessageMessages: str | None = None
        self.ringtone: str | None = None
        self.ringtonePart: str | None = None
        self._ringtone_lock = threading.Lock()
        self._canned_message_lock = threading.Lock()
        self._metadata_stdout_event_lock = threading.Lock()
        self._metadata_stdout_event: threading.Event | None = None
        self._admin_session_runtime = _NodeAdminSessionRuntime(self)
        self._admin_transport_runtime = _NodeAdminTransportRuntime(self)
        self._channel_write_runtime = _NodeChannelWriteRuntime(
            self,
            admin_session_runtime=self._admin_session_runtime,
            admin_transport_runtime=self._admin_transport_runtime,
        )
        self._delete_channel_runtime = _NodeDeleteChannelRuntime(
            self,
            channel_write_runtime=self._channel_write_runtime,
        )
        self._ack_nak_runtime = _NodeAckNakRuntime(self)
        self._settings_message_builder = _NodeSettingsMessageBuilder(self)
        self._settings_runtime = _NodeSettingsRuntime(
            self,
            message_builder=self._settings_message_builder,
        )
        self._settings_response_runtime = _NodeSettingsResponseRuntime(self)
        self._admin_command_runtime = _NodeAdminCommandRuntime(self)
        self._owner_profile_runtime = _NodeOwnerProfileRuntime(
            self,
            admin_command_runtime=self._admin_command_runtime,
        )
        self._content_cache_store = _NodeContentCacheStore(self)
        self._content_response_runtime = _NodeContentResponseRuntime(
            self,
            cache_store=self._content_cache_store,
        )
        self._content_request_runtime = _NodeAdminContentRuntime(
            self,
            cache_store=self._content_cache_store,
            response_runtime=self._content_response_runtime,
        )
        self._metadata_response_runtime = _NodeMetadataResponseRuntime(self)
        self._channel_response_runtime = _NodeChannelResponseRuntime(self)

    def __repr__(self) -> str:
        """Return a developer-oriented string identifying the Node.

        Returns
        -------
        str
            A debug-friendly representation containing the interface repr, the node number
            formatted as eight-hex digits (prefixed with '0x'), and any active
            non-default flags such as `noProto` or a non-default `timeout`.
        """
        r = f"Node({self.iface!r}, 0x{self.nodeNum:08x}"
        if self.noProto:
            r += ", noProto=True"
        if self._timeout.expireTimeout != 300.0:
            r += f", timeout={self._timeout.expireTimeout!r}"
        r += ")"
        return r

    @staticmethod
    def positionFlagsList(position_flags: int) -> list[str]:
        """Convert a PositionConfig position flags bitfield into a list of flag names.

        Parameters
        ----------
        position_flags : int
            Bitfield of flags from Config.PositionConfig.PositionFlags.

        Returns
        -------
        list[str]
            Names of the flags set in `position_flags`.
        """
        return flagsToList(
            config_pb2.Config.PositionConfig.PositionFlags, position_flags
        )

    # COMPAT_STABLE_SHIM: alias for positionFlagsList
    @staticmethod
    def position_flags_list(position_flags: int) -> list[str]:
        """Backward-compatible alias for positionFlagsList."""
        return Node.positionFlagsList(position_flags)

    @staticmethod
    def excludedModulesList(excluded_modules: int) -> list[str]:
        """Convert an ExcludedModules bitfield to a list of excluded module names.

        Parameters
        ----------
        excluded_modules : int
            Bitfield using values from mesh_pb2.ExcludedModules.

        Returns
        -------
        list[str]
            Names of modules whose bits are set in the bitfield.
        """
        return flagsToList(mesh_pb2.ExcludedModules, excluded_modules)

    # COMPAT_STABLE_SHIM: alias for excludedModulesList
    @staticmethod
    def excluded_modules_list(excluded_modules: int) -> list[str]:
        """Backward-compatible alias for excludedModulesList."""
        return Node.excludedModulesList(excluded_modules)

    @staticmethod
    def _emit_metadata_line(line: str) -> None:
        """Log a metadata line and mirror it to redirected stdout for compatibility."""
        logger.info("%s", line)
        # Historical callers parse getMetadata() output from redirected stdout.
        if sys.stdout is not sys.__stdout__:
            print(line)

    def _signal_metadata_stdout_event(self) -> None:
        """Signal redirected-stdout metadata waiters that a terminal response arrived."""
        with self._metadata_stdout_event_lock:
            metadata_stdout_event = self._metadata_stdout_event
        if metadata_stdout_event is not None:
            metadata_stdout_event.set()

    def _execute_with_node_db_lock(self, func: Callable[[], _ResultT]) -> _ResultT:
        """Execute ``func`` while holding ``iface._node_db_lock`` when available."""
        node_db_lock = getattr(self.iface, "_node_db_lock", None)
        if (
            node_db_lock is None
            or not hasattr(node_db_lock, "__enter__")
            or not hasattr(node_db_lock, "__exit__")
        ):
            return func()
        with node_db_lock:
            return func()

    def _get_metadata_snapshot(self) -> mesh_pb2.DeviceMetadata | None:
        """Return a stable snapshot of ``iface.metadata`` under the node DB lock when available."""

        def _read_and_copy() -> mesh_pb2.DeviceMetadata | None:
            metadata = getattr(self.iface, "metadata", None)
            if not isinstance(metadata, mesh_pb2.DeviceMetadata):
                return None
            metadata_snapshot = mesh_pb2.DeviceMetadata()
            metadata_snapshot.CopyFrom(metadata)
            return metadata_snapshot

        return self._execute_with_node_db_lock(_read_and_copy)

    def _set_metadata_snapshot(
        self, metadata_snapshot: mesh_pb2.DeviceMetadata
    ) -> None:
        """Persist a metadata snapshot to ``iface.metadata`` under the node DB lock when available."""

        def _write() -> None:
            stored_metadata = mesh_pb2.DeviceMetadata()
            stored_metadata.CopyFrom(metadata_snapshot)
            self.iface.metadata = stored_metadata

        self._execute_with_node_db_lock(_write)

    def _emit_cached_metadata_for_stdout(self) -> bool:
        """Emit metadata lines from ``self.iface.metadata`` for stdout parser compatibility."""
        metadata = self._get_metadata_snapshot()
        firmware_version = getattr(metadata, "firmware_version", "")
        if not isinstance(firmware_version, str) or not firmware_version:
            return False

        self._emit_metadata_line(f"\nfirmware_version: {firmware_version}")
        self._emit_metadata_line(
            f"device_state_version: {getattr(metadata, 'device_state_version', 0)}"
        )
        role = getattr(metadata, "role", 0)
        if role in config_pb2.Config.DeviceConfig.Role.values():
            role_value = cast(config_pb2.Config.DeviceConfig.Role.ValueType, role)
            self._emit_metadata_line(
                f"role: {config_pb2.Config.DeviceConfig.Role.Name(role_value)}"
            )
        else:
            self._emit_metadata_line(f"role: {role}")
        self._emit_metadata_line(
            f"position_flags: {self.position_flags_list(getattr(metadata, 'position_flags', 0))}"
        )
        hw_model = getattr(metadata, "hw_model", 0)
        if hw_model in mesh_pb2.HardwareModel.values():
            hw_model_value = cast(mesh_pb2.HardwareModel.ValueType, hw_model)
            self._emit_metadata_line(
                f"hw_model: {mesh_pb2.HardwareModel.Name(hw_model_value)}"
            )
        else:
            self._emit_metadata_line(f"hw_model: {hw_model}")
        self._emit_metadata_line(f"hasPKC: {getattr(metadata, 'hasPKC', False)}")
        excluded_modules = getattr(metadata, "excluded_modules", 0)
        if excluded_modules > 0:
            self._emit_metadata_line(
                f"excluded_modules: {self.excluded_modules_list(excluded_modules)}"
            )
        return True

    def moduleAvailable(self, excluded_bit: int) -> bool:
        """Determine whether a specific module bit is allowed by the interface metadata.

        Parameters
        ----------
        excluded_bit : int
            Bit mask for a module as defined in DeviceMetadata.excluded_modules.

        Returns
        -------
        bool
            `True` if the bit is not set in the interface metadata (module available), or if
            metadata is missing or an error occurs; `False` if the bit is set (module excluded).
        """
        meta = getattr(self.iface, "metadata", None)
        if meta is None:
            return True
        try:
            return bool((meta.excluded_modules & excluded_bit) == 0)
        except Exception as ex:  # noqa: BLE001 - defensive metadata compatibility
            logger.debug("Unable to evaluate module availability: %s", ex)
            return True

    # COMPAT_STABLE_SHIM: alias for moduleAvailable
    def module_available(self, excluded_bit: int) -> bool:
        """Backward-compatible alias for moduleAvailable."""
        return self.moduleAvailable(excluded_bit)

    def showChannels(self) -> None:
        """Print a human-readable list of configured channels and their shareable URLs.

        Each non-disabled channel is printed with its index, role, masked PSK, and settings as JSON.
        After listing channels, the primary channel URL is printed; if the full URL that includes
        all channels differs, it is printed as the "Complete URL".
        """
        print("Channels:")
        with self._channels_lock:
            channels_snapshot = list(self.channels) if self.channels else []
        if channels_snapshot:
            logger.debug("self.channels:%s", channels_snapshot)
            for c in channels_snapshot:
                cStr = messageToJson(c.settings)
                # don't show disabled channels
                if channel_pb2.Channel.Role.Name(c.role) != "DISABLED":
                    print(
                        f"  Index {c.index}: {channel_pb2.Channel.Role.Name(c.role)} psk={pskToString(c.settings.psk)} {cStr}"
                    )
        publicURL = self.getURL(includeAll=False)
        adminURL = self.getURL(includeAll=True)
        print(f"\nPrimary channel URL: {publicURL}")
        if adminURL != publicURL:
            print(f"Complete URL (includes all channels): {adminURL}")

    def showInfo(self) -> None:
        """Print the node's local and module configurations (as JSON when available) followed by its configured channels.

        If a configuration is not present, an empty placeholder is printed for that
        section. Channels are displayed using the node's channel listing format.
        """
        prefs = ""
        if self.localConfig:
            prefs = messageToJson(self.localConfig, multiline=True)
        print(f"Preferences: {prefs}\n")
        prefs = ""
        if self.moduleConfig:
            prefs = messageToJson(self.moduleConfig, multiline=True)
        print(f"Module preferences: {prefs}\n")
        self.showChannels()

    def setChannels(self, channels: Sequence[channel_pb2.Channel]) -> None:
        """Set the node's channel list and normalize channel entries.

        Parameters
        ----------
        channels : collections.abc.Sequence[meshtastic.protobuf.channel_pb2.Channel]
            Sequence of channel protobufs to assign to this node. The assigned
            list will be normalized (indices fixed) and padded as needed to meet expected
            channel count.
        """
        with self._channels_lock:
            copied_channels: list[channel_pb2.Channel] = []
            for source_channel in channels:
                copied_channel = channel_pb2.Channel()
                copied_channel.CopyFrom(source_channel)
                copied_channels.append(copied_channel)
            self.channels = copied_channels
            self._fixup_channels_locked()

    def requestChannels(self, startingIndex: int = 0) -> None:
        """Request channel definitions from the node, starting at the given channel index.

        When called with startingIndex 0, clears any cached channels and begins a fresh fetch into
        an internal partialChannels list. The method initiates the network request for the
        specified channel index.

        Parameters
        ----------
        startingIndex : int
            Zero-based channel index to start fetching from (typically 0-7). (Default value = 0)
        """
        logger.debug("requestChannels for nodeNum:%s", self.nodeNum)
        # only initialize if we're starting out fresh
        if startingIndex == 0:
            with self._channels_lock:
                self.channels = None
                # We keep our channels in a temp array until finished
                self.partialChannels = []
        self._request_channel(startingIndex)

    def onResponseRequestSettings(self, p: dict[str, Any]) -> None:
        """Process an admin response for a settings request and update the node's config objects.

        Parses the decoded response packet `p` to determine whether the request was acknowledged or
        rejected, marks the interface acknowledgment flags accordingly, and if the response contains
        `getConfigResponse` or `getModuleConfigResponse` copies the returned raw config into
        `self.localConfig` or `self.moduleConfig` respectively and logs the populated field.

        Parameters
        ----------
        p : dict[str, Any]
            Decoded response packet containing at least a `"decoded"` mapping with
            optional `"routing"` and `"admin"` entries. The `"admin"` entry is expected to
            contain either `getConfigResponse` or `getModuleConfigResponse` and accompanying
            `raw` bytes for the returned field.
        """
        self._settings_response_runtime.handle_settings_response(p)

    def requestConfig(
        self, configType: int | FieldDescriptor, adminIndex: int | None = None
    ) -> None:
        """Request a configuration subset or the full configuration from this node.

        If `configType` is an int it is treated as a config index. If it is a protobuf
        field descriptor, its `index` is used and the request targets `LocalConfig`
        when `containing_type.name == "LocalConfig"`, otherwise the module config is
        requested. For the local node the admin request is sent without a response
        handler; for a remote node this method registers a response handler and waits
        for an ACK/NAK before returning.

        Parameters
        ----------
        configType : int | FieldDescriptor
            Numeric config index or a
            protobuf field descriptor indicating which config field to fetch.
        adminIndex : int | None
            Admin channel index to use for sending; when None the node's
            configured admin channel is used. Pass 0 to force channel 0.
            (Default value = None)
        """
        self._settings_runtime.request_config(
            configType,
            admin_index=adminIndex,
        )

    def turnOffEncryptionOnPrimaryChannel(self) -> None:
        """Disable encryption on the primary channel and write the updated channel to the device.

        Raises
        ------
        MeshInterfaceError
            if channel data has not been loaded.
        """
        with self._channels_lock:
            channels = self.channels
            if not channels:
                self._raise_interface_error("Error: No channels have been read")
            channels[0].settings.psk = fromPSK("none")
        logger.info("Writing modified channels to device")
        self.writeChannel(0)

    def waitForConfig(self, attribute: str = "channels") -> bool:
        """Wait until a given attribute on the node's localConfig is populated or the timeout elapses.

        Parameters
        ----------
        attribute : str
            Name of the attribute on `localConfig` to wait for (default: "channels").

        Returns
        -------
        bool
            True if the attribute was set before the timeout expired, False otherwise.
        """
        return self._timeout.waitForSet(self, attrs=("localConfig", attribute))

    def _raise_interface_error(self, message: str) -> NoReturn:
        """Raise a MeshInterface-style error with the provided message.

        Parameters
        ----------
        message : str
            The error message to use for the raised MeshInterfaceError.

        Raises
        ------
        MeshInterface.MeshInterfaceError
            Always raised with the provided message.
        """
        from meshtastic.mesh_interface import (  # pylint: disable=import-outside-toplevel
            MeshInterface,
        )

        raise MeshInterface.MeshInterfaceError(message)

    def writeConfig(self, config_name: str) -> None:
        """Write a single named subsection of the node's edited configuration to the device.

        Sends only the specified device or module configuration section from this Node's cached
        localConfig/moduleConfig to the target node. For remote nodes the send expects an
        acknowledgment (ACK/NAK); for the local node the message is sent without waiting for an ACK/NAK.

        Parameters
        ----------
        config_name : str
            Configuration section to write. Valid values:
            "device", "position", "power", "network", "display", "lora", "bluetooth",
            "security", "mqtt", "serial", "external_notification", "store_forward",
            "range_test", "telemetry", "canned_message", "audio", "remote_hardware",
            "neighbor_info", "detection_sensor", "ambient_lighting", "paxcounter".

        Raises
        ------
        MeshInterfaceError
            If `config_name` is not one of the supported names, or if
            localConfig/moduleConfig has not been loaded.
        """
        self._settings_runtime.write_config(config_name)

    def _write_channel_snapshot(
        self,
        channel_to_write: channel_pb2.Channel,
        adminIndex: int | None = None,
    ) -> None:
        """Write a pre-built channel snapshot to the device.

        Parameters
        ----------
        channel_to_write : channel_pb2.Channel
            Snapshot payload to send via `set_channel`.
        adminIndex : int | None
            Admin channel index to use for sending; when None the node's
            configured admin channel is used. Pass 0 to force channel 0.
            (Default value = None)
        """
        self._channel_write_runtime.write_channel_snapshot(
            channel_to_write,
            admin_index=adminIndex,
        )

    def writeChannel(self, channelIndex: int, adminIndex: int | None = None) -> None:
        """Write the channel at the given index to the device.

        Sends the specified channel configuration to the node and ensures an admin session key is present before sending.

        Parameters
        ----------
        channelIndex : int
            Index of the channel to write.
        adminIndex : int | None
            Admin channel index to use for sending; when None the node's
            configured admin channel is used. (Default value = None)

        Raises
        ------
        MeshInterfaceError
            If channels have not been loaded (no channels to write).
        """
        self._channel_write_runtime.write_channel(
            channelIndex,
            admin_index=adminIndex,
        )

    # COMPAT_STABLE_SHIM: historical channel lookup helpers return live Channel
    # objects for mutate-then-write workflows (get*() -> edit -> writeChannel()).
    # Switching these accessors to defensive copies would be a behavioral break.
    def getChannelByChannelIndex(self, channelIndex: int) -> channel_pb2.Channel | None:
        """Retrieve the channel at the given zero-based index from this node's channels.

        Parameters
        ----------
        channelIndex : int
            Zero-based channel index (typically 0-7).

        Returns
        -------
        channel_pb2.Channel | None
            The channel at the specified index, or None if channels are unset or the index is out of range.

        Notes
        -----
        Returns a live channel object by design for backward compatibility with
        existing callers that mutate a selected channel and then persist via
        `writeChannel()`.
        """
        with self._channels_lock:
            channels = self.channels
            if channels and 0 <= channelIndex < len(channels):
                # Compatibility: return the live object, not a defensive copy.
                return channels[channelIndex]
            return None

    def getChannelCopyByChannelIndex(
        self, channelIndex: int
    ) -> channel_pb2.Channel | None:
        """Retrieve a defensive copy of the channel at the given zero-based index."""
        with self._channels_lock:
            channels = self.channels
            if channels and 0 <= channelIndex < len(channels):
                copied = channel_pb2.Channel()
                copied.CopyFrom(channels[channelIndex])
                return copied
            return None

    def deleteChannel(self, channelIndex: int) -> None:
        """Delete the channel at the given zero-based index and rewrite subsequent channels to normalize device channel state.

        Only channels with role SECONDARY or DISABLED may be removed; after
        removal, the channel list is normalized to the device channel count and
        affected channels are written back to the device. When operating on the local
        node, admin-channel indexing is adjusted so ongoing writes use the correct
        admin index.

        Parameters
        ----------
        channelIndex : int
            Zero-based index of the channel to delete.

        Raises
        ------
        MeshInterfaceError
            If channels have not been loaded.
        MeshInterfaceError
            If the channel at channelIndex is not Role.SECONDARY or Role.DISABLED.
        """
        self._delete_channel_runtime.delete_channel(channelIndex)

    def getChannelByName(self, name: str) -> channel_pb2.Channel | None:
        """Find a channel whose settings.name exactly matches the provided name.

        Parameters
        ----------
        name : str
            The channel name to search for.

        Returns
        -------
        channel_pb2.Channel | None
            The matching channel object if found, `None` otherwise.

        Notes
        -----
        Returns a live channel object by design for backward compatibility with
        existing callers that mutate a selected channel and then persist via
        `writeChannel()`.
        """
        with self._channels_lock:
            for c in self.channels or []:
                if c.settings and c.settings.name == name:
                    # Compatibility: return the live object, not a defensive copy.
                    return c
            return None

    def getChannelCopyByName(self, name: str) -> channel_pb2.Channel | None:
        """Find a channel by name and return a defensive copy for read-only use."""
        with self._channels_lock:
            for channel in self.channels or []:
                if channel.settings and channel.settings.name == name:
                    copied = channel_pb2.Channel()
                    copied.CopyFrom(channel)
                    return copied
            return None

    def getDisabledChannel(self) -> channel_pb2.Channel | None:
        """Find the first channel whose role is DISABLED.

        Returns
        -------
        channel_pb2.Channel | None
            The first disabled channel if present, `None` otherwise.

        Notes
        -----
        Returns a live channel object by design for backward compatibility with
        existing callers that mutate a selected channel and then persist via
        `writeChannel()`.
        """
        with self._channels_lock:
            channels = self.channels
            if channels is None:
                return None
            for c in channels:
                if c.role == channel_pb2.Channel.Role.DISABLED:
                    # Compatibility: return the live object, not a defensive copy.
                    return c
            return None

    def getDisabledChannelCopy(self) -> channel_pb2.Channel | None:
        """Find the first disabled channel and return a defensive copy for read-only use."""
        with self._channels_lock:
            channels = self.channels
            if channels is None:
                return None
            for channel in channels:
                if channel.role == channel_pb2.Channel.Role.DISABLED:
                    copied = channel_pb2.Channel()
                    copied.CopyFrom(channel)
                    return copied
            return None

    def getAdminChannelIndex(self) -> int:
        """Public accessor for the admin channel index on this node."""
        return self._get_admin_channel_index()

    def _get_named_admin_channel_index(self) -> int | None:
        """Return the index of a channel explicitly named ``admin``, if present."""
        with self._channels_lock:
            for channel in self.channels or []:
                if (
                    channel.role != channel_pb2.Channel.Role.DISABLED
                    and channel.settings
                    and _is_named_admin_channel_name(channel.settings.name)
                ):
                    return channel.index
            return None

    def _get_admin_channel_index(self) -> int:
        """Get the index of the channel named "admin", or 0 if no such channel exists.

        Returns
        -------
        int
            Index of the admin channel, or 0 if no channel with name "admin" is present.
        """
        named_admin_index = self._get_named_admin_channel_index()
        return 0 if named_admin_index is None else named_admin_index

    def setOwner(
        self,
        long_name: str | None = None,
        short_name: str | None = None,
        is_licensed: bool = False,
        is_unmessagable: bool | None = None,
    ) -> mesh_pb2.MeshPacket | None:
        """Set the device owner fields (long and short names) and optional license/unmessagable flags for this node.

        Parameters
        ----------
        long_name : str | None
            Owner long name; leading/trailing whitespace is trimmed. If provided and empty after trimming, an error is raised. (Default value = None)
        short_name : str | None
            Owner short name; leading/trailing whitespace
            is trimmed and truncated to 4 characters if longer. If provided and empty
            after trimming, an error is raised. (Default value = None)
        is_licensed : bool
            If `long_name` is provided, set the owner's licensed flag. (Default value = False)
        is_unmessagable : bool | None
            If provided, set the owner's unmessagable flag. (Default value = None)

        Returns
        -------
        mesh_pb2.MeshPacket | None
            The sent Admin message packet if available, otherwise `None`.

        Raises
        ------
        MeshInterfaceError
            If `long_name` or `short_name` is provided but empty or whitespace-only after trimming.
        """
        return self._owner_profile_runtime.set_owner(
            long_name=long_name,
            short_name=short_name,
            is_licensed=is_licensed,
            is_unmessagable=is_unmessagable,
        )

    def getURL(self, includeAll: bool = True) -> str:
        """Build a sharable meshtastic URL encoding the node's primary channel and LoRa configuration.

        Includes the secondary channels in the URL when requested.

        Parameters
        ----------
        includeAll : bool
            If True, include secondary channels in addition to the primary channel. (Default value = True)

        Returns
        -------
        share_url : str
            A meshtastic.org URL containing the encoded channel set and LoRa configuration.
        """
        # Only keep the primary/secondary channels, assume primary is first
        channelSet = apponly_pb2.ChannelSet()
        with self._channels_lock:
            channels_snapshot = list(self.channels) if self.channels else []
        if channels_snapshot:
            for c in channels_snapshot:
                if c.role == channel_pb2.Channel.Role.PRIMARY or (
                    includeAll and c.role == channel_pb2.Channel.Role.SECONDARY
                ):
                    channelSet.settings.append(c.settings)

        if len(self.localConfig.ListFields()) == 0:
            self.requestConfig(self.localConfig.DESCRIPTOR.fields_by_name["lora"])
        channelSet.lora_config.CopyFrom(self.localConfig.lora)
        some_bytes = channelSet.SerializeToString()
        s = base64.urlsafe_b64encode(some_bytes).decode("ascii")
        s = s.replace("=", "").replace("+", "-").replace("/", "_")
        return f"https://meshtastic.org/e/#{s}"

    def setURL(self, url: str, addOnly: bool = False) -> None:
        """Parse a Mesh URL and apply its channel and LoRa configuration to this node.

        If addOnly is False, replace the node's channel list with the channels encoded in the URL
        (first becomes PRIMARY, subsequent become SECONDARY) and write each channel to the device.
        If addOnly is True, add only channels from the URL whose names are not already present,
        placing each into the first available DISABLED channel and writing it.

        Parameters
        ----------
        url : str
            A Mesh share URL containing a base64-encoded ChannelSet (e.g., .../#<base64> or .../?add=true#<base64>).
        addOnly : bool
            If True, add channels without modifying existing ones; if False, replace channels with those from the URL. (Default value = False)

        Raises
        ------
        MeshInterfaceError
            If channels or configuration are not loaded, the URL is invalid or
            contains no settings, or no free channel slot is available when adding.
        """
        with self._channels_lock:
            if self.channels is None:
                self._raise_interface_error("Config or channels not loaded")
        parsed_input = _SetUrlParser.parse(
            url,
            raise_interface_error=self._raise_interface_error,
        )
        transaction = _SetUrlTransactionCoordinator(
            self,
            parsed_input=parsed_input,
        )
        if addOnly:
            transaction.apply_add_only()
            return
        transaction.apply_replace_all()

    def onResponseRequestRingtone(self, p: dict[str, Any]) -> None:
        """Process an admin response containing a ringtone fragment and cache it on the Node.

        If the decoded response has no routing error and contains an admin.raw
        get_ringtone_response, stores that value in self.ringtonePart; if a routing
        error is present, the cached ringtone is not modified.

        Parameters
        ----------
        p : dict[str, Any]
            Decoded response packet from the interface. Expected to include
            a "decoded" dict with optional "routing" (containing "errorReason") and
            "admin" -> "raw" -> get_ringtone_response payload.
        """
        self._content_response_runtime.handle_ringtone_response(p)

    def _get_ringtone(self) -> str | None:
        """Retrieve the node's ringtone as a single concatenated string.

        This call will wait for a device response and may block until the node replies
        or the node's timeout elapses. If the External Notification module is excluded
        by firmware, or if no ringtone is available or the request times out, the
        method returns None.

        Returns
        -------
        str | None
            The complete ringtone string if available, `None` if the
            module is not present, the ringtone is unavailable, or the request
            timed out.
        """
        return self._content_request_runtime.read_ringtone()

    def _set_ringtone(self, ringtone: str) -> mesh_pb2.MeshPacket | None:
        """Set the node's ringtone.

        Validates that the External Notification module is available and that the ringtone length
        is 230 characters or fewer; ensures an admin session key, then sends one admin message.
        Returns None if the External Notification module is not available. For remote nodes the
        send waits for an ACK/NAK response.

        Parameters
        ----------
        ringtone : str
            The ringtone text to set.

        Returns
        -------
        mesh_pb2.MeshPacket | None
            The result of sending the AdminMessage for the first chunk, or `None` if the External Notification module is unavailable.

        Raises
        ------
        MeshInterfaceError
            If `ringtone` length exceeds 230 characters.
        """
        return self._content_request_runtime.write_ringtone(ringtone)

    def onResponseRequestCannedMessagePluginMessageMessages(
        self, p: dict[str, Any]
    ) -> None:
        """Handle the admin response for a canned-message plugin messages request.

        If the response indicates a routing error, prints the error. On a successful response,
        stores the `get_canned_message_module_messages_response` payload from the admin raw data
        into `self.cannedPluginMessageMessages`.

        Parameters
        ----------
        p : dict[str, Any]
            Decoded packet dictionary containing response fields, expected to include
            keys like `"decoded"`, `"decoded"]["routing"]`, and `"decoded"]["admin"]["raw"]`.
        """
        self._content_response_runtime.handle_canned_message_response(p)

    def _get_canned_message(self) -> str | None:
        """Retrieve the device's canned message, requesting parts from the node if not already cached.

        If the canned-message module is excluded by firmware, returns None. When a
        request is made this call blocks until a response is received or the operation
        times out.

        Returns
        -------
        str | None
            str or None: The assembled canned message if available, or None if the module is unavailable or no response was received.
        """
        return self._content_request_runtime.read_canned_message()

    def _set_canned_message(self, message: str) -> mesh_pb2.MeshPacket | None:
        """Set the device's canned message.

        If the canned-message module is not available on the device, the method logs a warning and
        returns None. If the provided message is longer than 200 characters, a MeshInterfaceError
        is raised. The message is sent with one admin request (waiting for an ACK/NAK when
        targeting a remote node).

        Parameters
        ----------
        message : str
            The canned message to set; must be 200 characters or fewer.

        Returns
        -------
        mesh_pb2.MeshPacket | None
            The result returned by _send_admin for the first chunk, or `None` if the canned-message module is unavailable.

        Raises
        ------
        MeshInterfaceError
            If `message` length is greater than 200 characters.
        """
        return self._content_request_runtime.write_canned_message(message)

    # COMPAT_STABLE_SHIM: alias for getRingtone
    def get_ringtone(self) -> str | None:
        """Compatibility wrapper that returns the node's ringtone.

        Canonical public method: getRingtone().

        Returns
        -------
        ringtone : str | None
            The ringtone string if available, or None if unavailable or unsupported.
        """
        return self.getRingtone()

    # COMPAT_STABLE_SHIM: alias for setRingtone
    def set_ringtone(self, ringtone: str) -> mesh_pb2.MeshPacket | None:
        """Set the device's ringtone.

        Backward-compatibility alias for setRingtone().

        Parameters
        ----------
        ringtone : str
            Ringtone payload to apply to the device.

        Returns
        -------
        mesh_pb2.MeshPacket | None
            The Admin MeshPacket sent for the request, or `None` if no packet was produced.
        """
        return self.setRingtone(ringtone)

    # COMPAT_STABLE_SHIM: alias for getCannedMessage
    def get_canned_message(self) -> str | None:
        """Return the device's canned message.

        Canonical public method: getCannedMessage().

        Returns
        -------
        str | None
            The canned message string if available, `None` otherwise.
        """
        return self.getCannedMessage()

    # COMPAT_STABLE_SHIM: alias for setCannedMessage
    def set_canned_message(self, message: str) -> mesh_pb2.MeshPacket | None:
        """Set the device's canned message using a backward-compatible snake_case wrapper.

        Backward-compatibility alias for setCannedMessage().

        Parameters
        ----------
        message : str
            The canned message text to set (maximum 200 characters).

        Returns
        -------
        mesh_pb2.MeshPacket | None
            The Admin MeshPacket that was sent, or `None` if no packet is produced.
        """
        return self.setCannedMessage(message)

    def getRingtone(self) -> str | None:
        """Get the node's ringtone.

        Returns
        -------
        str | None
            The ringtone data as a single concatenated string, or `None` if the ringtone is unavailable.
        """
        return self._get_ringtone()

    def setRingtone(self, ringtone: str) -> mesh_pb2.MeshPacket | None:
        """Set the node's ringtone.

        Parameters
        ----------
        ringtone : str
            Ringtone string to set (maximum 230 characters).

        Returns
        -------
        mesh_pb2.MeshPacket | None
            The Admin MeshPacket sent to set the ringtone,
            or `None` if the operation could not be completed (for example, the
            ringtone feature is unavailable or the request timed out).
        """
        return self._set_ringtone(ringtone)

    def getCannedMessage(self) -> str | None:
        """Retrieve the node's canned message.

        Returns
        -------
        str | None
            The canned message string if available, `None` otherwise.
        """
        return self._get_canned_message()

    def setCannedMessage(self, message: str) -> mesh_pb2.MeshPacket | None:
        """Set the node's canned message.

        Validates module availability and that `message` is at most 200 characters,
        ensures an admin session key, sends the AdminMessage to write the canned
        message, and invalidates any cached canned-message state.

        Parameters
        ----------
        message : str
            The canned message text to set (maximum 200 characters).

        Returns
        -------
        mesh_pb2.MeshPacket | None
            The sent MeshPacket if a packet was transmitted, `None` if no packet was sent.
        """
        return self._set_canned_message(message)

    def exitSimulator(self) -> mesh_pb2.MeshPacket | None:
        """Request the target simulator process to exit; has no effect on non-simulator nodes.

        Returns
        -------
        mesh_pb2.MeshPacket | None
            A MeshPacket for the sent admin request, or `None` if the admin message was not sent.
        """
        return self._admin_command_runtime.exit_simulator()

    def reboot(self, secs: int = 10) -> mesh_pb2.MeshPacket | None:
        """Request the node to reboot after a delay.

        Parameters
        ----------
        secs : int
            Number of seconds to wait before rebooting. (Default value = 10)

        Returns
        -------
        mesh_pb2.MeshPacket | None
            The AdminMessage packet sent to the node, or `None` if no packet was sent.
        """
        return self._admin_command_runtime.reboot(secs)

    def beginSettingsTransaction(self) -> mesh_pb2.MeshPacket | None:
        """Request the node to open a settings edit transaction.

        Ensures an admin session key exists before sending the request and uses
        ACK/NAK handling for remote nodes while not waiting for a response from
        the local node.

        Returns
        -------
        mesh_pb2.MeshPacket | None
            The sent admin packet if available, or `None` otherwise.
        """
        return self._admin_command_runtime.begin_settings_transaction()

    def commitSettingsTransaction(self) -> mesh_pb2.MeshPacket | None:
        """Commit the node's open settings edit transaction.

        For remote nodes, waits for an ACK/NAK response; for the local node the commit is sent without waiting for a response.

        Returns
        -------
        mesh_pb2.MeshPacket | None
            The sent Admin `MeshPacket` when available, or `None`.
        """
        return self._admin_command_runtime.commit_settings_transaction()

    def rebootOTA(self, secs: int = 10) -> mesh_pb2.MeshPacket | None:
        """Request the node to perform an OTA reboot after a given delay.

        Parameters
        ----------
        secs : int
            Seconds to wait before rebooting. (Default value = 10)

        Returns
        -------
        mesh_pb2.MeshPacket | None
            The sent Admin message packet, or `None` if no packet was produced.
        """
        return self._admin_command_runtime.reboot_ota(secs)

    def startOTA(
        self,
        mode: admin_pb2.OTAMode.ValueType | None = None,
        ota_file_hash: bytes | None = None,
        *,
        ota_mode: admin_pb2.OTAMode.ValueType | None = None,
        ota_hash: bytes | None = None,
        **kwargs: Any,
    ) -> mesh_pb2.MeshPacket | None:
        """Request OTA mode for local node firmware that supports ota_request.

        Parameters
        ----------
        mode : admin_pb2.OTAMode.ValueType | None
            OTA transport mode to use after reboot (for example, ``admin_pb2.OTA_WIFI``).
        ota_file_hash : bytes | None
            Firmware hash bytes used by the node to validate OTA payload consistency.
        ota_mode : admin_pb2.OTAMode.ValueType | None
            Backward-compatible keyword alias for ``mode``.
        ota_hash : bytes | None
            Backward-compatible keyword alias for ``ota_file_hash``.

        Returns
        -------
        mesh_pb2.MeshPacket | None
            The sent Admin message packet, or ``None`` if no packet was produced.

        Raises
        ------
        MeshInterfaceError
            If called for a non-local node.
        """
        return self._admin_command_runtime.start_ota(
            mode=mode,
            ota_file_hash=ota_file_hash,
            ota_mode=ota_mode,
            ota_hash=ota_hash,
            extra_kwargs=kwargs,
        )

    def enterDFUMode(self) -> mesh_pb2.MeshPacket | None:
        """Request the node to enter DFU (NRF52) mode.

        Ensures an admin session key exists and sends an AdminMessage requesting DFU mode.
        When targeting a remote node, waits for an ACK/NAK response; local node sends without waiting.

        Returns
        -------
        mesh_pb2.MeshPacket | None
            The sent Admin message packet, or `None` if no packet was sent.
        """
        return self._admin_command_runtime.enter_dfu_mode()

    def shutdown(self, secs: int = 10) -> mesh_pb2.MeshPacket | None:
        """Request the node to shut down after a given number of seconds.

        Parameters
        ----------
        secs : int
            Number of seconds until the node shuts down. (Default value = 10)

        Returns
        -------
        mesh_pb2.MeshPacket | None
            The AdminMessage packet that was sent, or `None` if no packet was sent.
        """
        return self._admin_command_runtime.shutdown(secs)

    def getMetadata(self) -> None:
        """Request the node's device metadata and wait for an acknowledgement.

        Sends a metadata request to the node and blocks until an ACK or NAK is received; the
        received metadata is processed asynchronously when the response arrives.
        """
        p = admin_pb2.AdminMessage()
        p.get_device_metadata_request = True
        logger.info("Requesting device metadata")
        metadata_stdout_event = threading.Event()
        with self._metadata_stdout_event_lock:
            self._metadata_stdout_event = metadata_stdout_event
        try:
            self._send_admin(p, wantResponse=True, onResponse=self.onRequestGetMetadata)
            self.iface.waitForAckNak()
            if sys.stdout is not sys.__stdout__:
                callback_completed = metadata_stdout_event.wait(
                    METADATA_STDOUT_COMPAT_WAIT_SECONDS
                )
                # Ensure redirected-stdout parsers receive a deterministic metadata line
                # only when callback output may have been missed.
                if not callback_completed:
                    self._emit_cached_metadata_for_stdout()
        finally:
            with self._metadata_stdout_event_lock:
                if self._metadata_stdout_event is metadata_stdout_event:
                    self._metadata_stdout_event = None

    def factoryReset(self, full: bool = False) -> mesh_pb2.MeshPacket | None:
        """Request a factory reset on the node.

        Parameters
        ----------
        full : bool
            If True, perform a full device factory reset; if False, reset configuration only. (Default value = False)

        Returns
        -------
        mesh_pb2.MeshPacket | None
            The sent admin packet if sending succeeded, or None otherwise.
        """
        return self._admin_command_runtime.factory_reset(full=full)

    def removeNode(self, nodeId: int | str) -> mesh_pb2.MeshPacket | None:
        """Request removal of the mesh node identified by nodeId.

        Converts nodeId to a numeric node number and sends a remove-by-node-number
        admin request to the device. For remote targets, the request uses ACK/NAK
        handling; for the local node, no response callback is used.

        Parameters
        ----------
        nodeId : int | str
            Node number or a string convertible to a node number.

        Returns
        -------
        mesh_pb2.MeshPacket | None
            The admin packet returned by the send operation if available, `None` otherwise.
        """
        return self._admin_command_runtime.remove_node(nodeId)

    def setFavorite(self, nodeId: int | str) -> mesh_pb2.MeshPacket | None:
        """Mark a node as a favorite in the target device's NodeDB.

        Parameters
        ----------
        nodeId : int | str
            Node identifier (numeric or numeric string); will be converted to a node number.

        Returns
        -------
        mesh_pb2.MeshPacket | None
            The response packet if one was received, `None` otherwise.
        """
        return self._admin_command_runtime.set_favorite(nodeId)

    def removeFavorite(self, nodeId: int | str) -> mesh_pb2.MeshPacket | None:
        """Unmark a node as a favorite in the device's NodeDB.

        Parameters
        ----------
        nodeId : int | str
            Numeric node identifier or a string that can be converted to one.

        Returns
        -------
        mesh_pb2.MeshPacket | None
            The Admin packet sent to the device, or `None` if no packet was sent.
        """
        return self._admin_command_runtime.remove_favorite(nodeId)

    def setIgnored(self, nodeId: int | str) -> mesh_pb2.MeshPacket | None:
        """Mark a node in the device NodeDB as ignored.

        Parameters
        ----------
        nodeId : int | str
            Node number or string convertible to a node number.

        Returns
        -------
        mesh_pb2.MeshPacket | None
            The AdminMessage/packet sent to request the change, or `None` if no packet was sent.
        """
        return self._admin_command_runtime.set_ignored(nodeId)

    def removeIgnored(self, nodeId: int | str) -> mesh_pb2.MeshPacket | None:
        """Unmark a node as ignored in the device's NodeDB.

        Parameters
        ----------
        nodeId : int | str
            Node identifier (integer or numeric string). It will be converted to a numeric node number.

        Returns
        -------
        mesh_pb2.MeshPacket | None
            `mesh_pb2.MeshPacket` if an AdminMessage was sent, `None` otherwise.
        """
        return self._admin_command_runtime.remove_ignored(nodeId)

    def resetNodeDb(self) -> mesh_pb2.MeshPacket | None:
        """Request that the node clear its stored NodeDB (node database).

        Ensures an admin session key exists before sending. For remote targets, this
        waits for an ACK/NAK response; for the local node, it does not wait.

        Returns
        -------
        mesh_pb2.MeshPacket | None
            The AdminMessage packet sent, or `None` if no packet was sent.
        """
        return self._admin_command_runtime.reset_node_db()

    def setFixedPosition(
        self, lat: int | float, lon: int | float, alt: int
    ) -> mesh_pb2.MeshPacket | None:
        """Set the node's fixed position and enable the fixed-position setting on the device.

        Parameters
        ----------
        lat : int | float
            Latitude specified either as an integer in 1e-7 degrees units or as a float in decimal degrees.
        lon : int | float
            Longitude specified either as an integer in 1e-7 degrees units or as a float in decimal degrees.
        alt : int
            Altitude in meters (0 leaves altitude unset).

        Returns
        -------
        mesh_packet : mesh_pb2.MeshPacket | None
            The result from sending the AdminMessage, or `None` if no packet was sent.
        """
        self.ensureSessionKey()

        p = mesh_pb2.Position()
        if isinstance(lat, float) and lat != 0.0:
            p.latitude_i = int(lat * 1e7)
        elif isinstance(lat, int) and lat != 0:
            p.latitude_i = lat

        if isinstance(lon, float) and lon != 0.0:
            p.longitude_i = int(lon * 1e7)
        elif isinstance(lon, int) and lon != 0:
            p.longitude_i = lon

        if alt != 0:
            p.altitude = alt

        a = admin_pb2.AdminMessage()
        a.set_fixed_position.CopyFrom(p)

        if self == self.iface.localNode:
            onResponse = None
        else:
            onResponse = self.onAckNak
        return self._send_admin(a, onResponse=onResponse)

    def removeFixedPosition(self) -> mesh_pb2.MeshPacket | None:
        """Clear the node's fixed position setting.

        Sends an AdminMessage requesting removal of the node's fixed position; remote nodes will use ACK/NAK handling.

        Returns
        -------
        mesh_pb2.MeshPacket | None
            The sent AdminMessage (`mesh_pb2.MeshPacket`) if a packet was transmitted, or `None` if sending was skipped.
        """
        self.ensureSessionKey()
        p = admin_pb2.AdminMessage()
        p.remove_fixed_position = True
        logger.info("Telling node to remove fixed position")

        if self == self.iface.localNode:
            onResponse = None
        else:
            onResponse = self.onAckNak
        return self._send_admin(p, onResponse=onResponse)

    def setTime(self, timeSec: int = 0) -> mesh_pb2.MeshPacket | None:
        """Set the node's clock to the specified Unix timestamp.

        If `timeSec` is 0, the system's current time is used. The call sends an
        AdminMessage to set the node time; for remote nodes, the function waits for
        an ACK/NAK response.

        Parameters
        ----------
        timeSec : int
            Unix timestamp in seconds to set on the node; pass 0 to use the current system time. (Default value = 0)

        Returns
        -------
        mesh_pb2.MeshPacket | None
            mesh_pb2.MeshPacket or None: The sent AdminMessage packet when available, or `None` if no packet is produced.
        """
        self.ensureSessionKey()
        if timeSec == 0:
            timeSec = int(time.time())
        p = admin_pb2.AdminMessage()
        p.set_time_only = timeSec
        logger.info("Setting node time to %s", timeSec)

        if self == self.iface.localNode:
            onResponse = None
        else:
            onResponse = self.onAckNak
        return self._send_admin(p, onResponse=onResponse)

    def _fixup_channels(self) -> None:
        """Normalize the node's channel list by assigning sequential index values and ensuring the list contains the expected number of channels.

        If `channels` is None this is a no-op. Otherwise this method sets each channel's `index`
        field to its position in the list (starting at 0) and then appends disabled channels as
        needed so the channel list reaches the required length.
        """
        with self._channels_lock:
            self._fixup_channels_locked()

    def _fixup_channels_locked(self) -> None:
        """Normalize channel indices and size while holding ``self._channels_lock``."""
        channels = self.channels
        if channels is None:
            return

        if len(channels) > MAX_CHANNELS:
            logger.warning(
                "Truncating channel list from %d to %d entries",
                len(channels),
                MAX_CHANNELS,
            )
            del channels[MAX_CHANNELS:]

        # Add extra disabled channels as needed
        # This is needed because the protobufs will have index **missing** if the channel number is zero
        for index, ch in enumerate(channels):
            ch.index = index  # fixup indexes

        self._fill_channels_locked()

    def _fill_channels(self) -> None:
        """Ensure the node has exactly eight channels by appending DISABLED channels as needed.

        If `self.channels` is None this is a no-op. Appends new Channel objects with
        role `DISABLED` and sequential `index` values until the list length reaches
        ``MAX_CHANNELS``.
        """
        with self._channels_lock:
            self._fill_channels_locked()

    def _fill_channels_locked(self) -> None:
        """Append disabled channels up to ``MAX_CHANNELS`` while holding ``self._channels_lock``."""
        channels = self.channels
        if channels is None:
            return

        # Add extra disabled channels as needed
        index = len(channels)
        while index < MAX_CHANNELS:
            ch = channel_pb2.Channel()
            ch.role = channel_pb2.Channel.Role.DISABLED
            ch.index = index
            channels.append(ch)
            index += 1

    def onRequestGetMetadata(self, p: dict[str, Any]) -> None:
        """Handle an incoming device metadata response packet and display the parsed metadata.

        Parses the decoded packet, updates the interface acknowledgment state (ACK/NAK), handles
        routing-layer ACK/NAK packets, and logs the device metadata
        fields (firmware_version, device_state_version, role, position_flags, hw_model, hasPKC,
        and excluded_modules) when available.

        Parameters
        ----------
        p : dict[str, Any]
            Decoded packet containing at minimum a 'decoded' key with routing and
            admin/raw get_device_metadata_response fields.
        """
        self._metadata_response_runtime.handle_metadata_response(p)

    def onResponseRequestChannel(self, p: dict[str, Any]) -> None:
        """Process a response packet for a previously requested channel and update the Node's channel state.

        If the packet is a routing message with an error, expire the request timeout and retry the
        last requested channel. If the packet contains an admin get_channel_response, append that
        channel to the node's partial channel list, reset the request timeout, and either continue
        requesting the next channel or, when the final channel is received, replace the node's
        channels with the collected channels and normalize them.

        Parameters
        ----------
        p : dict[str, Any]
            Decoded packet dictionary from the interface. Expected to contain either
            - a routing message with 'routing.errorReason', or
            - an admin message with 'admin.raw.get_channel_response' (a Channel protobuf-like object with an `index` field).
        """
        self._channel_response_runtime.handle_channel_response(p)

    def onAckNak(self, p: dict[str, Any]) -> None:
        """Handle an incoming ACK/NAK admin response and update interface acknowledgment state.

        Inspect the routing error reason in the parsed packet `p` and:
        - If the errorReason is not "NONE", log a NAK message and set
          iface._acknowledgment.receivedNak to True.
        - If the errorReason is "NONE" and the packet originates from the local node, log an
          implicit-ACK message and set iface._acknowledgment.receivedImplAck to True.
        - Otherwise log a normal ACK message and set iface._acknowledgment.receivedAck to True.

        Parameters
        ----------
        p : dict[str, Any]
            Parsed packet dictionary expected to contain:
            - p["decoded"]["routing"]["errorReason"]: routing error reason string.
            - p["from"]: numeric origin node identifier (string or int convertible).
        """
        self._ack_nak_runtime.handle_ack_nak(p)

    def _request_channel(self, channelNum: int) -> mesh_pb2.MeshPacket | None:
        """Request settings for a single channel from this node.

        Sends an admin request for the channel at the given zero-based index and registers the response handler.

        Parameters
        ----------
        channelNum : int
            Zero-based index of the channel to request.

        Returns
        -------
        mesh_pb2.MeshPacket | None
            The AdminMessage packet sent to the interface, or `None` if sending was skipped (e.g., protocol disabled).
        """
        p = admin_pb2.AdminMessage()
        p.get_channel_request = channelNum + 1

        # Show progress message for super slow operations
        if self != self.iface.localNode:
            logger.info(
                "Requesting channel %s info from remote node (this could take a while)",
                channelNum,
            )
        else:
            logger.debug("Requesting channel %s", channelNum)

        return self._send_admin(
            p, wantResponse=True, onResponse=self.onResponseRequestChannel
        )

    # pylint: disable=R1710
    def _send_admin(
        self,
        p: admin_pb2.AdminMessage,
        wantResponse: bool = False,
        onResponse: Callable[[dict[str, Any]], Any] | None = None,
        adminIndex: int | None = None,
    ) -> mesh_pb2.MeshPacket | None:
        """Send an AdminMessage to this Node's admin channel.

        Parameters
        ----------
        p : admin_pb2.AdminMessage
            AdminMessage to send; a session passkey may be attached.
        wantResponse : bool
            Request a response from the recipient when True. (Default value = False)
        onResponse : Callable[[dict[str, Any]], Any] | None
            Optional callback invoked with the received response packet. (Default value = None)
        adminIndex : int | None
            Channel index to use for the admin message; when None the node's
            configured admin channel is used. Pass 0 to force channel 0.
            (Default value = None)

        Returns
        -------
        mesh_pb2.MeshPacket | None
            The MeshPacket returned by the send operation,
            or `None` if sending was skipped because protocol use is disabled.
        """
        return self._admin_transport_runtime.send_admin(
            p,
            want_response=wantResponse,
            on_response=onResponse,
            admin_index=adminIndex,
        )

    def ensureSessionKey(self, adminIndex: int | None = None) -> None:
        """Ensure an admin session key exists for this node, requesting one if missing.

        If protocol use is disabled (`noProto`), no action is taken. Otherwise, if the node has no
        `adminSessionPassKey` recorded, a session-key request is sent.

        Parameters
        ----------
        adminIndex : int | None
            Admin channel index to use for the session key request; when None
            the node's configured admin channel is used. Pass 0 to force
            channel 0. (Default value = None)
        """
        self._admin_session_runtime.ensure_session_key(admin_index=adminIndex)

    def _get_channels_with_hash(self) -> list[dict[str, Any]]:
        """Return a list of channel descriptors containing index, role, name, and an optional hash.

        Returns
        -------
        list[dict[str, Any]]
            A list of dictionaries, each with keys:
            - "index" (int): The channel's zero-based index.
            - "role" (str): The channel role name.
            - "name" (str): The channel settings name, or an empty string if missing.
            - "hash" (int or None): Computed channel hash when both name and PSK are present, otherwise None.
        """
        result: list[dict[str, Any]] = []
        with self._channels_lock:
            channels_snapshot = list(self.channels) if self.channels else []
        if channels_snapshot:
            for c in channels_snapshot:
                settings = getattr(c, "settings", None)
                name = getattr(settings, "name", "")
                psk = getattr(settings, "psk", b"")
                has_name = bool(name)
                has_psk = bool(psk)
                hash_val = (
                    generate_channel_hash(name, psk) if has_name and has_psk else None
                )
                result.append(
                    {
                        "index": c.index,
                        "role": channel_pb2.Channel.Role.Name(c.role),
                        "name": name if has_name else "",
                        "hash": hash_val,
                    }
                )
        return result

    # COMPAT_STABLE_SHIM: alias for getChannelsWithHash
    def get_channels_with_hash(self) -> list[dict[str, Any]]:
        """Get channel entries with computed per-channel hashes.

        Each entry is a dict containing:
        - `index` (int): zero-based channel index.
        - `role` (str): channel role name.
        - `name` (str): channel settings name, or an empty string if unset.
        - `hash` (int | None): computed channel hash when both `name` and PSK are present, otherwise `None`.

        Returns
        -------
        list[dict[str, Any]]
            The list of channel entries described above.
        """
        return self.getChannelsWithHash()

    def getChannelsWithHash(self) -> list[dict[str, Any]]:
        """Compatibility wrapper that returns channel entries including computed per-channel hashes.

        Returns
        -------
        list[dict[str, Any]]
            A list of dictionaries, each with keys 'index', 'role', 'name', and 'hash'
            where 'hash' is the computed channel hash when both name and PSK are
            present, or `None` otherwise.
        """
        return self._get_channels_with_hash()
