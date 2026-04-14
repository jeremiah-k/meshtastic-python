# COMPAT_STABLE_SHIM: historical import surface for meshtastic.node_runtime.seturl_runtime
"""Backward-compat shim — canonical code lives in meshtastic.node_runtime.seturl package."""

from meshtastic.node_runtime.seturl import (  # noqa: F401
    _channel_matches_desired,
    _channels_fingerprint,
    _compute_remaining_channel_writes,
    _SetUrlAddOnlyExecutionState,
    _SetUrlAddOnlyPlan,
    _SetUrlAddOnlyPlanner,
    _SetUrlAdminContext,
    _SetUrlCacheManager,
    _SetUrlExecutionEngine,
    _SetUrlParsedInput,
    _SetUrlParser,
    _SetUrlReplaceExecutionState,
    _SetUrlReplacePlan,
    _SetUrlReplacePlanner,
    _SetUrlTransactionCoordinator,
)

__all__ = [
    "_channel_matches_desired",
    "_channels_fingerprint",
    "_compute_remaining_channel_writes",
    "_SetUrlAddOnlyExecutionState",
    "_SetUrlAddOnlyPlan",
    "_SetUrlAddOnlyPlanner",
    "_SetUrlAdminContext",
    "_SetUrlCacheManager",
    "_SetUrlExecutionEngine",
    "_SetUrlParsedInput",
    "_SetUrlParser",
    "_SetUrlReplaceExecutionState",
    "_SetUrlReplacePlan",
    "_SetUrlReplacePlanner",
    "_SetUrlTransactionCoordinator",
]
