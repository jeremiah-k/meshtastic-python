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
from meshtastic.node_runtime.seturl.rollback import _SetUrlRollbackEngine

if TYPE_CHECKING:
    from meshtastic.node import Node

logger = logging.getLogger(__name__)


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
