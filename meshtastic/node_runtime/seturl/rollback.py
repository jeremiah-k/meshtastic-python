"""Rollback ordering/admin-index fallback and cache restore/invalidate strategy."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Callable

from meshtastic.node_runtime.seturl.cache import _SetUrlCacheManager
from meshtastic.node_runtime.seturl.context import _SetUrlAdminContext
from meshtastic.node_runtime.seturl.execution import (
    _SetUrlAddOnlyExecutionState,
    _SetUrlReplaceExecutionState,
)
from meshtastic.node_runtime.seturl.planner import (
    _SetUrlAddOnlyPlan,
    _SetUrlReplacePlan,
)
from meshtastic.node_runtime.shared import orderedAdminIndexes as _orderedAdminIndexes
from meshtastic.protobuf import admin_pb2

if TYPE_CHECKING:
    from meshtastic.node import Node

logger = logging.getLogger(__name__)


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

    def _best_effort_rollback(
        self,
        *,
        rollback_admin_indexes: list[int],
        channel_rollback_order: list[int],
        get_channel_snapshot: Callable[[int], Any | None],
        do_channel_write: Callable[[Any, int], None],
        channel_warning_template: str,
        lora_write_started: bool,
        original_lora_config: Any | None,
        lora_warning_message: str,
        lora_no_snapshot_message: str,
        cache_invalidate_message: str,
    ) -> bool:
        """Shared helper for best-effort rollback of channels and LoRa config.

        Returns True if any rollback failed (for cache invalidation tracking).
        """
        rollback_failed = False

        # Rollback channels
        for index in channel_rollback_order:
            rollback_channel = get_channel_snapshot(index)
            if rollback_channel is None:
                continue
            rollback_succeeded = False
            last_rollback_error: Exception | None = None
            for rollback_admin_index in rollback_admin_indexes:
                try:
                    do_channel_write(rollback_channel, rollback_admin_index)
                    rollback_succeeded = True
                    break
                # Best-effort rollback path; keep attempting remaining steps.
                except Exception as rollback_error:  # noqa: BLE001
                    last_rollback_error = rollback_error
            if not rollback_succeeded:
                rollback_failed = True
                logger.warning(
                    channel_warning_template,
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

        # Invalidate cache if channel rollback failed
        if rollback_failed:
            self._cache_manager.invalidate_channel_cache(cache_invalidate_message)

        # Rollback LoRa config if needed
        if lora_write_started and original_lora_config is not None:
            rollback_lora = admin_pb2.AdminMessage()
            rollback_lora.set_config.lora.CopyFrom(original_lora_config)
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
                    self._cache_manager.restore_lora_snapshot(original_lora_config)
                    rollback_lora_succeeded = True
                    break
                # Best-effort rollback path; keep original failure semantics.
                except Exception as rollback_lora_error:  # noqa: BLE001
                    last_rollback_lora_error = rollback_lora_error
            if not rollback_lora_succeeded:
                logger.warning(
                    lora_warning_message,
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
        elif lora_write_started:
            self._cache_manager.clear_lora_cache_with_warning(lora_no_snapshot_message)

        return rollback_failed

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

        # Compute rollback admin indexes
        written_index_set = set(state.written_indices)
        if plan.deferred_add_only_admin_index in written_index_set:
            rollback_admin_indexes = _orderedAdminIndexes(
                plan.deferred_add_only_admin_index,
                admin_context.admin_index_for_write,
            )
        else:
            rollback_admin_indexes = _orderedAdminIndexes(
                admin_context.admin_index_for_write,
                plan.deferred_add_only_admin_index,
            )

        # Build channel rollback order
        channel_rollback_order: list[int] = []
        if (
            plan.deferred_add_only_admin_index is not None
            and plan.deferred_add_only_admin_index in written_index_set
        ):
            channel_rollback_order.append(plan.deferred_add_only_admin_index)
        for index in reversed(state.written_indices):
            if index not in channel_rollback_order:
                channel_rollback_order.append(index)

        # Define callbacks for the shared helper
        def get_channel_snapshot(index: int) -> object | None:
            return plan.original_channels_by_index.get(index)

        def do_channel_write(channel: object, admin_index: int) -> None:
            self._node._write_channel_snapshot(  # noqa: SLF001
                channel,  # type: ignore[arg-type]
                adminIndex=admin_index,
            )

        # Use shared helper
        self._best_effort_rollback(
            rollback_admin_indexes=rollback_admin_indexes,
            channel_rollback_order=channel_rollback_order,
            get_channel_snapshot=get_channel_snapshot,
            do_channel_write=do_channel_write,
            channel_warning_template="Rollback of channel index %s failed after addOnly partial failure.",
            lora_write_started=state.lora_write_started,
            original_lora_config=plan.original_lora_config,
            lora_warning_message="Rollback of LoRa config failed after addOnly partial failure.",
            lora_no_snapshot_message=(
                "LoRa config cache cleared after addOnly failure without rollback "
                "snapshot; reload config before using localConfig.lora."
            ),
            cache_invalidate_message="Channel rollback incomplete after addOnly failure; invalidated local channel cache.",
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
            rollback_admin_indexes = _orderedAdminIndexes(
                plan.deferred_new_named_admin_index,
                *state.rollback_admin_indexes_for_write,
                admin_context.admin_index_for_write,
            )
        else:
            rollback_admin_indexes = _orderedAdminIndexes(
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

        # Define callbacks for the shared helper
        def get_channel_snapshot(index: int) -> object | None:
            return plan.replace_original_channels_by_index.get(index)

        def do_channel_write(channel: object, admin_index: int) -> None:
            self._node._write_channel_snapshot(
                channel, adminIndex=admin_index
            )  # noqa: SLF001

        rollback_failed = self._best_effort_rollback(
            rollback_admin_indexes=rollback_admin_indexes,
            channel_rollback_order=replace_channel_rollback_order,
            get_channel_snapshot=get_channel_snapshot,
            do_channel_write=do_channel_write,
            channel_warning_template="Rollback of channel index %s failed after replace-all partial failure.",
            lora_write_started=state.lora_write_started,
            original_lora_config=plan.replace_original_lora_config,
            lora_warning_message="Rollback of LoRa config failed after replace-all partial failure.",
            lora_no_snapshot_message=(
                "LoRa config cache cleared after replace-all failure without "
                "rollback snapshot; reload config before using localConfig.lora."
            ),
            cache_invalidate_message="Replace-all rollback incomplete after failure; invalidated local channel cache.",
        )
        if not rollback_failed:
            self._cache_manager.restore_replace_channels_snapshot(
                plan.replace_original_channels_snapshot,
                expected_channels_ref=plan.replace_original_channels_ref,
            )
