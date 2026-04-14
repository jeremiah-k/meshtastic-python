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
from meshtastic.node_runtime.seturl.parser import _SetUrlParsedInput
from meshtastic.node_runtime.seturl.planner import (
    _SetUrlAddOnlyPlanner,
    _SetUrlReplacePlanner,
)

if TYPE_CHECKING:
    from meshtastic.node import Node

logger = logging.getLogger(__name__)

POST_FAILURE_RECONNECT_TIMEOUT_SECONDS = 10.0
"""Maximum time to wait for transport reconnect after a replace-all write failure."""


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

    def _attempt_post_failure_reconnect_report(self) -> None:
        """Best-effort reconnect and device state report after a write failure.

        Attempts to wait for the transport to reconnect, reload the node
        configuration, and log the actual device channel state.  This does
        **not** resume writes — it only provides diagnostic information
        about what the device actually contains after the partial failure.

        All steps are wrapped in broad exception handling so that a
        secondary failure during diagnostics never suppresses the original
        error.
        """
        try:
            iface = self._node.iface
            connected = iface.isConnected.is_set()
            if not connected:
                logger.info(
                    "Attempting to wait for transport reconnect after failure "
                    "(timeout=%.1fs)...",
                    POST_FAILURE_RECONNECT_TIMEOUT_SECONDS,
                )
                connected = iface.isConnected.wait(
                    POST_FAILURE_RECONNECT_TIMEOUT_SECONDS
                )
            if not connected:
                logger.warning(
                    "Transport did not reconnect within %.1fs; "
                    "cannot report actual device channel state.",
                    POST_FAILURE_RECONNECT_TIMEOUT_SECONDS,
                )
                return
            logger.info("Transport reconnected; reloading device config...")
            iface.waitForConfig()
            actual_url = self._node.getURL()
            logger.info(
                "Actual device channel URL after partial failure: %s", actual_url
            )
            channels = self._node.channels
            if channels is not None:
                active_names = [
                    ch.settings.name
                    for ch in channels
                    if ch.role != 0 and ch.HasField("settings") and ch.settings.name
                ]
                logger.info(
                    "Active named channels on device: %s",
                    ", ".join(active_names) if active_names else "(none)",
                )
        except Exception:
            logger.debug(
                "Post-failure reconnect/report failed; ignoring diagnostic error.",
                exc_info=True,
            )

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
        """Execute the replace-all setURL transaction pipeline.

        Plans the write sequence, delegates execution to the engine, and
        invalidates local caches on failure.  The execution state tracks
        the current stage and written channel indices so that error
        reports identify the exact failure point.

        On failure, attempts a best-effort reconnect and device state
        report so that operators can see what the device actually
        contains without having to manually reconnect first.
        """
        planner = _SetUrlReplacePlanner(
            self._node,
            parsed_input=self._parsed_input,
            admin_context=self._admin_context,
        )
        plan = planner.build_plan()
        execution_state = _SetUrlReplaceExecutionState()
        try:
            self._execution_engine.executeReplaceAll(
                parsed_input=self._parsed_input,
                admin_context=self._admin_context,
                plan=plan,
                state=execution_state,
            )
        except Exception:
            _stage_label = (
                execution_state.stage.value
                if execution_state.stage is not None
                else "unknown"
            )
            _total_channels = len(plan.staged_channels)
            logger.error(
                "setURL replace-all failed at stage '%s' after writing %d of %d "
                "planned channel writes; device may be partially configured. "
                "Phase 3 reconnect/verify did not run because failure occurred "
                "during Phase 1. Local caches invalidated — reconnect and reload "
                "before further operations.",
                _stage_label,
                len(execution_state.written_channel_indices),
                _total_channels,
            )
            self._cache_manager.invalidate_channel_cache(
                "Channel cache invalidated after setURL replace-all partial failure."
            )
            if execution_state.lora_write_started:
                self._cache_manager.clear_lora_cache_with_warning(
                    "LoRa config cache cleared after setURL replace-all partial failure; "
                    "reload config before using localConfig.lora."
                )
            self._attempt_post_failure_reconnect_report()
            raise
