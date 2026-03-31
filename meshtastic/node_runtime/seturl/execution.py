"""Remote write ordering and admin-index strategy for setURL transactions."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from meshtastic.node_runtime.seturl.cache import _SetUrlCacheManager
from meshtastic.node_runtime.seturl.context import _SetUrlAdminContext
from meshtastic.node_runtime.seturl.helpers import _channels_fingerprint
from meshtastic.node_runtime.seturl.parser import _SetUrlParsedInput
from meshtastic.node_runtime.seturl.planner import (
    _SetUrlAddOnlyPlan,
    _SetUrlReplacePlan,
)
from meshtastic.node_runtime.shared import (
    isNamedAdminChannelName as _isNamedAdminChannelName,
)
from meshtastic.node_runtime.shared import orderedAdminIndexes as _orderedAdminIndexes
from meshtastic.protobuf import admin_pb2, channel_pb2

if TYPE_CHECKING:
    from meshtastic.node import Node

logger = logging.getLogger(__name__)


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
                and _isNamedAdminChannelName(channel.settings.name)
            ):
                return channel.index
        return 0

    def _write_lora_config(
        self,
        parsed_input: _SetUrlParsedInput,
        admin_context: _SetUrlAdminContext,
        state: _SetUrlAddOnlyExecutionState | _SetUrlReplaceExecutionState,
    ) -> None:
        """Write LoRa configuration to the device."""
        if not parsed_input.has_lora_update:
            return
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

    def executeAddOnly(
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

        self._write_lora_config(parsed_input, admin_context, state)

        if plan.deferred_add_only_admin_channel is not None:
            staged_channel, channel_name = plan.deferred_add_only_admin_channel
            logger.info("Adding new channel '%s' to device", channel_name)
            self._node._write_channel_snapshot(  # noqa: SLF001
                staged_channel,
                adminIndex=admin_context.admin_index_for_write,
            )
            state.written_indices.append(staged_channel.index)

    def executeReplaceAll(
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
                staged_channel.settings.name if staged_channel.settings else None,
            )
            self._node._write_channel_snapshot(  # noqa: SLF001
                staged_channel,
                adminIndex=admin_context.admin_index_for_write,
            )
            state.written_channel_indices.append(staged_channel.index)
            self._cache_manager.apply_replace_channel_write(
                staged_channel,
                expected_channels_ref=plan.replace_original_channels_ref,
            )

        self._write_lora_config(parsed_input, admin_context, state)
        if parsed_input.has_lora_update:
            self._cache_manager.apply_lora_success(parsed_input.channel_set.lora_config)

        if plan.deferred_new_named_admin_channel is not None:
            logger.debug(
                "Writing deferred admin channel index=%s role=%s name=%s",
                plan.deferred_new_named_admin_channel.index,
                self._safe_channel_role_name(
                    plan.deferred_new_named_admin_channel.role
                ),
                (
                    plan.deferred_new_named_admin_channel.settings.name
                    if plan.deferred_new_named_admin_channel.settings
                    else None
                ),
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
            )
            state.rollback_admin_indexes_for_write = _orderedAdminIndexes(
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
            state.rollback_admin_indexes_for_write = _orderedAdminIndexes(
                updated_admin_index_for_write,
                post_write_fallback_admin_index,
                *state.rollback_admin_indexes_for_write,
            )
            self._cache_manager.apply_replace_channel_write(
                plan.deferred_previous_admin_slot_channel,
                expected_channels_ref=plan.replace_original_channels_ref,
            )
