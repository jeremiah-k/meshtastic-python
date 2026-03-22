"""setURL transaction runtime owners and planning/execution boundaries."""

import base64
import binascii
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, NoReturn

from google.protobuf.message import DecodeError

from meshtastic.node_runtime.shared import (
    is_named_admin_channel_name as _is_named_admin_channel_name,
)
from meshtastic.node_runtime.shared import (
    ordered_admin_indexes as _ordered_admin_indexes,
)
from meshtastic.protobuf import admin_pb2, apponly_pb2, channel_pb2, config_pb2

if TYPE_CHECKING:
    from meshtastic.node import Node

logger = logging.getLogger(__name__)

_ERR_CONFIG_OR_CHANNELS_NOT_LOADED = "Config or channels not loaded"


def _channels_fingerprint(
    channels: list[channel_pb2.Channel],
) -> tuple[bytes, ...]:
    """Return an immutable, deterministic fingerprint of channel states for comparison."""
    return tuple(c.SerializeToString() for c in channels)


@dataclass(frozen=True)
class _SetUrlParsedInput:
    """Parsed/decoded setURL input."""

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
    original_channels_ref: list[channel_pb2.Channel]
    original_channels_fingerprint: tuple[bytes, ...]
    original_channels_by_index: dict[int, channel_pb2.Channel]
    original_lora_config: config_pb2.Config.LoRaConfig | None


@dataclass
class _SetUrlReplacePlan:
    """Planning output for replace-all transactions."""

    max_channels: int
    replace_original_channels_ref: list[channel_pb2.Channel]
    replace_original_channels_fingerprint: tuple[bytes, ...]
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
    rollback_admin_indexes_for_write: list[int] = field(default_factory=list)


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
            raise_interface_error("Invalid URL")
        b64 = url.split("#")[-1]
        if not b64:
            raise_interface_error("Invalid URL: no channel data found")

        # We normally strip padding to make for a shorter URL, but the python parser doesn't like
        # that.  So add back any missing padding
        # per https://stackoverflow.com/a/9807138
        missing_padding = len(b64) % 4
        if missing_padding:
            b64 += "=" * (4 - missing_padding)

        try:
            decoded_url = base64.urlsafe_b64decode(b64)
        except (binascii.Error, ValueError) as ex:
            raise_interface_error(f"Invalid URL: {ex}")

        channel_set = apponly_pb2.ChannelSet()
        try:
            channel_set.ParseFromString(decoded_url)
        except (DecodeError, ValueError) as ex:
            raise_interface_error(f"Unable to parse channel settings from URL: {ex}")

        if len(channel_set.settings) == 0:
            raise_interface_error("There were no settings.")
        return _SetUrlParsedInput(
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

        def _capture() -> config_pb2.Config.LoRaConfig:
            if not self._node.localConfig.HasField("lora"):
                self._node._raise_interface_error(  # noqa: SLF001
                    "LoRa config must be loaded before setURL(addOnly=True)"
                )
            original_lora_config = config_pb2.Config.LoRaConfig()
            original_lora_config.CopyFrom(self._node.localConfig.lora)
            return original_lora_config

        return self._node._execute_with_node_db_lock(_capture)

    def build_plan(
        self,
        *,
        original_lora_config: config_pb2.Config.LoRaConfig | None,
    ) -> _SetUrlAddOnlyPlan:
        """Build addOnly staging plan, dedupe selection, and deferred admin handling."""
        ignored_channel_names: list[str] = []
        channels_to_write: list[tuple[channel_pb2.Channel, str]] = []
        original_channels_ref: list[channel_pb2.Channel] = []
        original_channels_by_index: dict[int, channel_pb2.Channel] = {}
        original_channels_fingerprint: tuple[bytes, ...] = ()
        with self._node._channels_lock:  # noqa: SLF001
            channels = self._node.channels
            if channels is None:
                self._node._raise_interface_error(  # noqa: SLF001
                    _ERR_CONFIG_OR_CHANNELS_NOT_LOADED
                )
            original_channels_ref = channels
            original_channels_fingerprint = _channels_fingerprint(channels)
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
            original_channels_ref=original_channels_ref,
            original_channels_fingerprint=original_channels_fingerprint,
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
        replace_original_channels_fingerprint: tuple[bytes, ...] = ()
        with self._node._channels_lock:  # noqa: SLF001
            replace_original_channels_ref: list[channel_pb2.Channel] = []
            channels = self._node.channels
            if channels is None:
                self._node._raise_interface_error(  # noqa: SLF001
                    _ERR_CONFIG_OR_CHANNELS_NOT_LOADED
                )
            replace_original_channels_ref = channels
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
            replace_original_channels_fingerprint = _channels_fingerprint(channels)

        replace_original_lora_config: config_pb2.Config.LoRaConfig | None = None
        if self._parsed_input.has_lora_update:

            def _capture_replace_lora() -> config_pb2.Config.LoRaConfig:
                has_lora_config = self._node.localConfig.HasField("lora")
                has_any_local_config = len(self._node.localConfig.ListFields()) > 0
                if not has_lora_config or not has_any_local_config:
                    self._node._raise_interface_error(  # noqa: SLF001
                        "LoRa config must be loaded before setURL() when the URL updates LoRa settings"
                    )
                snapshot = config_pb2.Config.LoRaConfig()
                snapshot.CopyFrom(self._node.localConfig.lora)
                return snapshot

            replace_original_lora_config = self._node._execute_with_node_db_lock(
                _capture_replace_lora
            )

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
            replace_original_channels_ref=replace_original_channels_ref,
            replace_original_channels_fingerprint=replace_original_channels_fingerprint,
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

    def _invalidate_channel_cache_locked(self, warning_message: str) -> None:
        """Invalidate channel cache while already holding ``_channels_lock``."""
        self._node.channels = None
        self._node.partialChannels = []
        logger.warning("%s", warning_message)

    def apply_add_only_success(
        self,
        channels_to_write: list[tuple[channel_pb2.Channel, str]],
        *,
        expected_channels_fingerprint: tuple[bytes, ...] | None = None,
    ) -> None:
        """Apply addOnly channel cache updates after remote writes succeed."""
        with self._node._channels_lock:  # noqa: SLF001
            channels = self._node.channels
            if channels is None:
                self._node.partialChannels = []
                logger.warning(
                    "Channel cache unavailable after successful addOnly apply; reload channels to refresh local state."
                )
                return
            if (
                expected_channels_fingerprint is not None
                and _channels_fingerprint(channels) != expected_channels_fingerprint
            ):
                self._invalidate_channel_cache_locked(
                    "Channel cache changed during addOnly cache update; invalidating local channel cache."
                )
                return
            for staged_channel, _ in channels_to_write:
                if 0 <= staged_channel.index < len(channels):
                    channels[staged_channel.index].CopyFrom(staged_channel)
                else:
                    self._invalidate_channel_cache_locked(
                        f"Channel index {staged_channel.index} out of range during addOnly cache update; invalidating local channel cache."
                    )
                    break
            else:
                self._node.partialChannels = []

    def apply_replace_channel_write(
        self,
        staged_channel: channel_pb2.Channel,
        *,
        expected_channels_fingerprint: tuple[bytes, ...] | None = None,
        expected_channels_ref: list[channel_pb2.Channel] | None = None,
    ) -> None:
        """Apply one replace-path channel cache update after a successful write."""
        with self._node._channels_lock:  # noqa: SLF001
            channels = self._node.channels
            if channels is None:
                self._node._raise_interface_error(  # noqa: SLF001
                    _ERR_CONFIG_OR_CHANNELS_NOT_LOADED
                )
            channels_changed = False
            if (
                expected_channels_ref is not None
                and channels is not expected_channels_ref
            ):
                channels_changed = True
            if not channels_changed and expected_channels_fingerprint is not None:
                channels_changed = (
                    _channels_fingerprint(channels) != expected_channels_fingerprint
                )
            if channels_changed:
                self._invalidate_channel_cache_locked(
                    "Channel cache changed during replace-all cache update; invalidating local channel cache."
                )
                self._node._raise_interface_error(  # noqa: SLF001
                    "Channel cache changed during replace-all cache update; aborting transaction."
                )
            if staged_channel.index < 0 or staged_channel.index >= len(channels):
                self._node._raise_interface_error(  # noqa: SLF001
                    f"Channel index {staged_channel.index} out of range during cache update"
                )
            channels[staged_channel.index].CopyFrom(staged_channel)
            self._node.partialChannels = []

    def invalidate_channel_cache(self, warning_message: str) -> None:
        """Invalidate local channel cache after incomplete rollback."""
        with self._node._channels_lock:  # noqa: SLF001
            self._invalidate_channel_cache_locked(warning_message)

    def restore_replace_channels_snapshot(
        self,
        replace_original_channels_snapshot: list[channel_pb2.Channel],
        *,
        expected_channels_fingerprint: tuple[bytes, ...] | None = None,
        expected_channels_ref: list[channel_pb2.Channel] | None = None,
    ) -> None:
        """Restore replace-all channel cache from pre-transaction snapshot."""
        with self._node._channels_lock:  # noqa: SLF001
            channels = self._node.channels
            channels_changed = False
            if (
                expected_channels_ref is not None
                and channels is not None
                and channels is not expected_channels_ref
            ):
                channels_changed = True
            if (
                not channels_changed
                and expected_channels_fingerprint is not None
                and channels is not None
            ):
                channels_changed = (
                    _channels_fingerprint(channels) != expected_channels_fingerprint
                )
            if channels_changed:
                self._invalidate_channel_cache_locked(
                    "Channel cache changed during replace-all rollback restore; invalidating local channel cache."
                )
                return
            restored_channels: list[channel_pb2.Channel] = []
            for original_channel in replace_original_channels_snapshot:
                restored_channel = channel_pb2.Channel()
                restored_channel.CopyFrom(original_channel)
                restored_channels.append(restored_channel)
            self._node.channels = restored_channels
            self._node.partialChannels = []

    def apply_lora_success(self, lora_config: config_pb2.Config.LoRaConfig) -> None:
        """Apply successful LoRa cache update."""

        def _apply() -> None:
            self._node.localConfig.lora.CopyFrom(lora_config)

        self._node._execute_with_node_db_lock(_apply)

    def restore_lora_snapshot(
        self,
        original_lora_config: config_pb2.Config.LoRaConfig,
    ) -> None:
        """Restore LoRa cache from pre-transaction snapshot."""

        def _restore() -> None:
            self._node.localConfig.lora.CopyFrom(original_lora_config)

        self._node._execute_with_node_db_lock(_restore)

    def clear_lora_cache_with_warning(self, warning_message: str) -> None:
        """Clear LoRa cache when rollback cannot restore prior value."""

        def _clear() -> None:
            self._node.localConfig.ClearField("lora")

        self._node._execute_with_node_db_lock(_clear)
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

    @staticmethod
    def _safe_channel_role_name(role: int) -> str:
        """Return a safe channel role label for logging."""
        if role in channel_pb2.Channel.Role.values():
            return channel_pb2.Channel.Role.Name(role)  # type: ignore[arg-type]
        return f"UNKNOWN({role})"

    @staticmethod
    def _post_write_fallback_admin_index(plan: _SetUrlReplacePlan) -> int:
        """Return fallback admin index from staged post-write channel state."""
        for channel_index in sorted(plan.staged_channels_by_index):
            channel = plan.staged_channels_by_index[channel_index]
            if (
                channel.role != channel_pb2.Channel.Role.DISABLED
                and channel.settings
                and channel.settings.name
                and _is_named_admin_channel_name(channel.settings.name)
            ):
                return channel.index
        return 0

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
            self._node._write_channel_snapshot(  # noqa: SLF001
                staged_channel,
                adminIndex=admin_context.admin_index_for_write,
            )
            state.written_indices.append(staged_channel.index)

        if parsed_input.has_lora_update:
            set_lora = admin_pb2.AdminMessage()
            set_lora.set_config.lora.CopyFrom(parsed_input.channel_set.lora_config)
            self._node.ensureSessionKey(adminIndex=admin_context.admin_index_for_write)
            request = self._node._send_admin(  # noqa: SLF001
                set_lora,
                adminIndex=admin_context.admin_index_for_write,
            )
            if request is None:
                self._node._raise_interface_error(  # noqa: SLF001
                    "LoRa config update was not started"
                )
            state.lora_write_started = True

        if plan.deferred_add_only_admin_channel is not None:
            staged_channel, channel_name = plan.deferred_add_only_admin_channel
            logger.info("Adding new channel '%s' to device", channel_name)
            self._node._write_channel_snapshot(  # noqa: SLF001
                staged_channel,
                adminIndex=admin_context.admin_index_for_write,
            )
            state.written_indices.append(staged_channel.index)

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
        channels = self._node.channels
        channels_changed = plan.replace_original_channels_ref is not channels
        if (
            not channels_changed
            and plan.replace_original_channels_fingerprint
            and channels is not None
        ):
            channels_changed = (
                _channels_fingerprint(channels)
                != plan.replace_original_channels_fingerprint
            )
        if channels_changed:
            self._cache_manager.invalidate_channel_cache(
                "Channel cache changed before replace-all write; invalidating local channel cache."
            )
            self._node._raise_interface_error(  # noqa: SLF001
                "Channel cache changed before replace-all write; aborting transaction."
            )
        for staged_channel in plan.staged_channels:
            if staged_channel.index in deferred_channel_indexes:
                continue
            logger.debug(
                "Writing channel index=%s role=%s name=%s",
                staged_channel.index,
                self._safe_channel_role_name(staged_channel.role),
                staged_channel.settings.name,
            )
            self._node._write_channel_snapshot(  # noqa: SLF001
                staged_channel,
                adminIndex=admin_context.admin_index_for_write,
            )
            state.written_channel_indices.append(staged_channel.index)
            self._cache_manager.apply_replace_channel_write(
                staged_channel,
                expected_channels_ref=plan.replace_original_channels_ref,
                expected_channels_fingerprint=plan.replace_original_channels_fingerprint,
            )
            self._node.partialChannels = []

        if parsed_input.has_lora_update:
            set_lora = admin_pb2.AdminMessage()
            set_lora.set_config.lora.CopyFrom(parsed_input.channel_set.lora_config)
            self._node.ensureSessionKey(adminIndex=admin_context.admin_index_for_write)
            request = self._node._send_admin(  # noqa: SLF001
                set_lora,
                adminIndex=admin_context.admin_index_for_write,
            )
            if request is None:
                self._node._raise_interface_error(  # noqa: SLF001
                    "LoRa config update was not started"
                )
            state.lora_write_started = True
            self._cache_manager.apply_lora_success(parsed_input.channel_set.lora_config)

        if plan.deferred_new_named_admin_channel is not None:
            logger.debug(
                "Writing deferred admin channel index=%s role=%s name=%s",
                plan.deferred_new_named_admin_channel.index,
                self._safe_channel_role_name(
                    plan.deferred_new_named_admin_channel.role
                ),
                plan.deferred_new_named_admin_channel.settings.name,
            )
            self._node._write_channel_snapshot(  # noqa: SLF001
                plan.deferred_new_named_admin_channel,
                adminIndex=admin_context.admin_index_for_write,
            )
            state.written_channel_indices.append(
                plan.deferred_new_named_admin_channel.index
            )
            self._cache_manager.apply_replace_channel_write(
                plan.deferred_new_named_admin_channel,
                expected_channels_ref=plan.replace_original_channels_ref,
                expected_channels_fingerprint=plan.replace_original_channels_fingerprint,
            )
            state.rollback_admin_indexes_for_write = _ordered_admin_indexes(
                plan.deferred_new_named_admin_channel.index,
                *state.rollback_admin_indexes_for_write,
            )

        if plan.deferred_previous_admin_slot_channel is not None:
            updated_admin_index_for_write = admin_context.admin_index_for_write
            if plan.deferred_new_named_admin_channel is not None:
                updated_admin_index_for_write = (
                    admin_context.admin_write_node._get_admin_channel_index()  # noqa: SLF001
                )
            post_write_fallback_admin_index = self._post_write_fallback_admin_index(
                plan
            )
            logger.debug(
                "Rewriting deferred admin slot i:%s via admin index %s",
                plan.deferred_previous_admin_slot_channel.index,
                updated_admin_index_for_write,
            )
            self._node._write_channel_snapshot(  # noqa: SLF001
                plan.deferred_previous_admin_slot_channel,
                adminIndex=updated_admin_index_for_write,
            )
            state.written_channel_indices.append(
                plan.deferred_previous_admin_slot_channel.index
            )
            state.rollback_admin_indexes_for_write = _ordered_admin_indexes(
                updated_admin_index_for_write,
                post_write_fallback_admin_index,
                *state.rollback_admin_indexes_for_write,
            )
            self._cache_manager.apply_replace_channel_write(
                plan.deferred_previous_admin_slot_channel,
                expected_channels_ref=plan.replace_original_channels_ref,
                expected_channels_fingerprint=plan.replace_original_channels_fingerprint,
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
                    request = self._node._send_admin(  # noqa: SLF001
                        rollback_lora,
                        adminIndex=rollback_admin_index,
                    )
                    if request is None:
                        continue
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
                *state.rollback_admin_indexes_for_write,
                admin_context.admin_index_for_write,
            )
        else:
            rollback_admin_indexes = _ordered_admin_indexes(
                *state.rollback_admin_indexes_for_write,
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
                        request = self._node._send_admin(  # noqa: SLF001
                            rollback_lora,
                            adminIndex=rollback_admin_index,
                        )
                        if request is None:
                            continue
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
                plan.replace_original_channels_snapshot,
                expected_channels_ref=plan.replace_original_channels_ref,
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
        if admin_write_node is None:
            self._node._raise_interface_error(  # noqa: SLF001
                "Interface localNode not initialized"
            )
        admin_index_for_write = (
            admin_write_node._get_admin_channel_index()
        )  # noqa: SLF001
        named_admin_index_for_write = (
            admin_write_node._get_named_admin_channel_index()  # noqa: SLF001
        )
        return _SetUrlAdminContext(
            admin_write_node=admin_write_node,
            admin_index_for_write=admin_index_for_write,
            named_admin_index_for_write=named_admin_index_for_write,
            has_admin_write_node_named_admin=(named_admin_index_for_write is not None),
        )

    def _apply_add_only(self) -> None:
        """Execute the addOnly setURL transaction pipeline."""
        planner = _SetUrlAddOnlyPlanner(
            self._node,
            parsed_input=self._parsed_input,
            admin_context=self._admin_context,
        )
        original_lora_config = planner.capture_original_lora_snapshot()
        plan = planner.build_plan(original_lora_config=original_lora_config)
        for ignored_name in plan.ignored_channel_names:
            logger.info(
                'Ignoring existing or empty channel "%s" from add URL',
                ignored_name,
            )
        if not plan.channels_to_write and not self._parsed_input.has_lora_update:
            return
        self._node.ensureSessionKey(
            adminIndex=self._admin_context.admin_index_for_write
        )
        execution_state = _SetUrlAddOnlyExecutionState()
        try:
            self._execution_engine.execute_add_only(
                parsed_input=self._parsed_input,
                admin_context=self._admin_context,
                plan=plan,
                state=execution_state,
            )
        except Exception:
            self._rollback_engine.rollback_add_only(
                admin_context=self._admin_context,
                plan=plan,
                state=execution_state,
            )
            raise
        self._cache_manager.apply_add_only_success(
            plan.channels_to_write,
            expected_channels_fingerprint=plan.original_channels_fingerprint,
        )
        if self._parsed_input.has_lora_update:
            self._cache_manager.apply_lora_success(
                self._parsed_input.channel_set.lora_config
            )

    def _apply_replace_all(self) -> None:
        """Execute the replace-all setURL transaction pipeline."""
        planner = _SetUrlReplacePlanner(
            self._node,
            parsed_input=self._parsed_input,
            admin_context=self._admin_context,
        )
        plan = planner.build_plan()
        execution_state = _SetUrlReplaceExecutionState(
            rollback_admin_indexes_for_write=[self._admin_context.admin_index_for_write]
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
