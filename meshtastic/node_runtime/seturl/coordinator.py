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
        """Execute the replace-all setURL transaction pipeline."""
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
            logger.error(
                "setURL replace-all failed after writing %d channel(s); "
                "device may be partially configured. "
                "Local caches invalidated — reconnect and reload before further operations.",
                len(execution_state.written_channel_indices),
            )
            self._cache_manager.invalidate_channel_cache(
                "Channel cache invalidated after setURL replace-all partial failure."
            )
            if execution_state.lora_write_started:
                self._cache_manager.clear_lora_cache_with_warning(
                    "LoRa config cache cleared after setURL replace-all partial failure; "
                    "reload config before using localConfig.lora."
                )
            raise
