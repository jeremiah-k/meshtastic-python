"""Transaction coordination for setURL transactions."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from meshtastic.node_runtime.seturl.cache import _SetUrlCacheManager
from meshtastic.node_runtime.seturl.context import _SetUrlAdminContext
from meshtastic.node_runtime.seturl.execution import (
    _SetUrlAddOnlyExecutionState,
    _SetUrlExecutionEngine,
    _SetUrlReplaceExecutionState,
)
from meshtastic.node_runtime.seturl.helpers import _compute_remaining_channel_writes
from meshtastic.node_runtime.seturl.parser import _SetUrlParsedInput
from meshtastic.node_runtime.seturl.planner import (
    _SetUrlAddOnlyPlanner,
    _SetUrlReplacePlan,
    _SetUrlReplacePlanner,
)

if TYPE_CHECKING:
    from meshtastic.node import Node

logger = logging.getLogger(__name__)

MAX_REPLACE_ALL_RESUME_ATTEMPTS = 3
"""Maximum number of reconnect/resume cycles for replace-all."""

RESUME_RECONNECT_TIMEOUT_SECONDS = 15.0
"""Timeout for waiting for transport reconnect during resume."""


class _SetUrlTransactionCoordinator:
    """Coordinates setURL transaction planning, execution, and cache policy with fail-fast semantics."""

    def __init__(self, node: "Node", *, parsed_input: _SetUrlParsedInput) -> None:
        """Initialize the setURL transaction coordinator.

        Parameters
        ----------
        node : Node
            The node instance to coordinate transactions for.
        parsed_input : _SetUrlParsedInput
            Parsed setURL input containing channel set and flags.
        """
        self._node = node
        self._parsed_input = parsed_input
        self._admin_context = self._resolve_admin_context()
        self._cache_manager = _SetUrlCacheManager(node)
        self._execution_engine = _SetUrlExecutionEngine(
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
        admin_index_for_write = admin_write_node._get_admin_channel_index()  # noqa: SLF001
        named_admin_index_for_write = (
            admin_write_node._get_named_admin_channel_index()  # noqa: SLF001
        )
        return _SetUrlAdminContext(
            admin_write_node=admin_write_node,
            admin_index_for_write=admin_index_for_write,
            named_admin_index_for_write=named_admin_index_for_write,
            has_admin_write_node_named_admin=(named_admin_index_for_write is not None),
        )

    def _reconnect_and_compute_remaining(
        self,
        plan: _SetUrlReplacePlan,
    ) -> tuple[set[int], bool] | None:
        """Reconnect, reload device state, and compute remaining writes.

        Returns
        -------
        tuple[set[int], bool] | None
            ``(remaining_channel_indices, lora_still_needed)`` if reconnect
            succeeded, or ``None`` if reconnect failed.
        """
        try:
            iface = self._node.iface
            connected = iface.isConnected.is_set()
            if not connected:
                logger.info(
                    "Waiting for transport reconnect (timeout=%.1fs)...",
                    RESUME_RECONNECT_TIMEOUT_SECONDS,
                )
                connected = iface.isConnected.wait(RESUME_RECONNECT_TIMEOUT_SECONDS)
            if not connected:
                logger.warning(
                    "Transport did not reconnect within %.1fs; cannot resume.",
                    RESUME_RECONNECT_TIMEOUT_SECONDS,
                )
                return None
            logger.info("Transport reconnected; reloading device config...")
            self._cache_manager.invalidate_channel_cache(
                "Channel cache invalidated before resume reload."
            )
            iface.waitForConfig()
            actual_channels = self._node.channels
            if actual_channels is None:
                logger.warning(
                    "Device channels not available after reconnect; cannot resume."
                )
                return None
            remaining_indices = _compute_remaining_channel_writes(
                actual_channels, plan.staged_channels_by_index
            )
            lora_needed = False
            if self._parsed_input.has_lora_update:
                actual_lora = self._node.localConfig.lora
                lora_needed = (
                    actual_lora.SerializeToString()
                    != self._parsed_input.channel_set.lora_config.SerializeToString()
                )
            if not remaining_indices and not lora_needed:
                logger.info(
                    "Device state already matches desired configuration after "
                    "reconnect; no further writes needed."
                )
                return (set(), False)
            logger.info(
                "After reconnect: %d channel(s) still differ from desired state, "
                "LoRa update %s.",
                len(remaining_indices),
                "still needed" if lora_needed else "already applied",
            )
            if remaining_indices:
                diff_names: list[str] = []
                for idx in sorted(remaining_indices):
                    desired_ch = plan.staged_channels_by_index.get(idx)
                    name = (
                        desired_ch.settings.name
                        if desired_ch and desired_ch.settings
                        else f"index {idx}"
                    )
                    diff_names.append(name)
                logger.info(
                    "Channels still needing writes: %s",
                    ", ".join(diff_names),
                )
            return (remaining_indices, lora_needed)
        except Exception:
            logger.debug(
                "Reconnect/compute-remaining failed; ignoring diagnostic error.",
                exc_info=True,
            )
            return None

    def _apply_add_only(self) -> None:
        """Execute the addOnly setURL transaction pipeline."""
        planner = _SetUrlAddOnlyPlanner(
            self._node,
            parsed_input=self._parsed_input,
            admin_context=self._admin_context,
        )
        plan = planner.build_plan()
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
            self._execution_engine.executeAddOnly(
                parsed_input=self._parsed_input,
                admin_context=self._admin_context,
                plan=plan,
                state=execution_state,
            )
        except Exception:
            logger.error(
                "setURL addOnly failed after writing %d channel(s); "
                "device may be partially configured. "
                "Local caches invalidated — reconnect and reload before further operations.",
                len(execution_state.written_indices),
            )
            self._cache_manager.invalidate_channel_cache(
                "Channel cache invalidated after setURL addOnly partial failure."
            )
            if execution_state.lora_write_started:
                self._cache_manager.clear_lora_cache_with_warning(
                    "LoRa config cache cleared after setURL addOnly partial failure; "
                    "reload config before using localConfig.lora."
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
        """Execute the replace-all setURL transaction pipeline with bounded resume.

        Plans the write sequence, delegates execution to the engine, and
        invalidates local caches on failure.  If a write failure occurs
        (typically from transport disconnect during a large channel set
        replacement over TCP), the coordinator attempts to reconnect,
        reload actual device state, compute remaining writes, and continue
        only the channels/LoRa config that still differ from desired state.

        Resume is bounded by ``MAX_REPLACE_ALL_RESUME_ATTEMPTS`` total
        attempts (initial + resume retries).  If convergence is not
        achieved within the bound, the final error is raised with
        precise reporting of the failure stage.
        """
        planner = _SetUrlReplacePlanner(
            self._node,
            parsed_input=self._parsed_input,
            admin_context=self._admin_context,
        )
        plan = planner.build_plan()
        skip_channel_indices: set[int] = set()
        skip_lora = False
        last_exception: Exception | None = None
        execution_state = _SetUrlReplaceExecutionState()
        for attempt in range(MAX_REPLACE_ALL_RESUME_ATTEMPTS):
            execution_state = _SetUrlReplaceExecutionState()
            resume_skip_channel_indices = (
                skip_channel_indices if attempt > 0 else None
            )
            try:
                self._execution_engine.executeReplaceAll(
                    parsed_input=self._parsed_input,
                    admin_context=self._admin_context,
                    plan=plan,
                    state=execution_state,
                    skip_channel_indices=resume_skip_channel_indices,
                    skip_lora=skip_lora,
                )
                return
            except Exception as exc:
                last_exception = exc
                _stage_label = (
                    execution_state.stage.value
                    if execution_state.stage is not None
                    else "unknown"
                )
                logger.warning(
                    "setURL replace-all attempt %d/%d failed at stage '%s'; "
                    "%d channel writes completed before failure.",
                    attempt + 1,
                    MAX_REPLACE_ALL_RESUME_ATTEMPTS,
                    _stage_label,
                    len(execution_state.written_channel_indices),
                )
                if attempt + 1 >= MAX_REPLACE_ALL_RESUME_ATTEMPTS:
                    break
                self._cache_manager.invalidate_channel_cache(
                    "Channel cache invalidated after setURL replace-all partial failure."
                )
                if execution_state.lora_write_started:
                    self._cache_manager.clear_lora_cache_with_warning(
                        "LoRa config cache cleared after setURL replace-all partial failure."
                    )
                resume_result = self._reconnect_and_compute_remaining(plan)
                if resume_result is None:
                    logger.error(
                        "Could not reconnect after replace-all failure; "
                        "cannot attempt resume."
                    )
                    break
                remaining_indices, lora_needed = resume_result
                if not remaining_indices and not lora_needed:
                    logger.info(
                        "Replace-all resumed successfully: device converged to "
                        "desired state after reconnect (attempt %d).",
                        attempt + 1,
                    )
                    return
                skip_channel_indices = {
                    idx
                    for idx in plan.staged_channels_by_index
                    if idx not in remaining_indices
                }
                skip_lora = not lora_needed
                self._admin_context = self._resolve_admin_context()
                logger.info(
                    "Resuming replace-all (attempt %d/%d): %d channel(s) and %s"
                    "LoRa write remaining.",
                    attempt + 2,
                    MAX_REPLACE_ALL_RESUME_ATTEMPTS,
                    len(remaining_indices),
                    "" if lora_needed else "no ",
                )
        _stage_label = (
            execution_state.stage.value
            if execution_state.stage is not None
            else "unknown"
        )
        logger.error(
            "setURL replace-all failed at stage '%s' after %d attempt(s); "
            "device may be partially configured. Phase 3 reconnect/verify did "
            "not run because failure occurred during Phase 1. Local caches "
            "invalidated — reconnect and reload before further operations.",
            _stage_label,
            MAX_REPLACE_ALL_RESUME_ATTEMPTS,
        )
        self._cache_manager.invalidate_channel_cache(
            "Channel cache invalidated after setURL replace-all final failure."
        )
        if execution_state.lora_write_started:
            self._cache_manager.clear_lora_cache_with_warning(
                "LoRa config cache cleared after setURL replace-all final failure; "
                "reload config before using localConfig.lora."
            )
        if last_exception is not None:
            raise last_exception
