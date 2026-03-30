"""Rollback ordering/admin-index fallback and cache restore/invalidate strategy."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

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
from meshtastic.node_runtime.shared import (
    ordered_admin_indexes as _ordered_admin_indexes,
)
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
