"""Internal mesh-interface runtime decomposition modules.

This package contains internal runtime components for MeshInterface.
These are implementation details and NOT part of the stable public API.
Imports from this package are not guaranteed to remain stable across versions.

Re-export Policy:
- __all__ is intentionally minimal as all internal code imports directly
  from submodules (e.g., `from .flows import TelemetryType`)
- Underscore-prefixed symbols are never exported
- No stability guarantees for symbols in this package
"""

from .flows import (
    DEFAULT_TELEMETRY_TYPE,
    VALID_TELEMETRY_TYPE_SET,
    VALID_TELEMETRY_TYPES,
    TelemetryType,
)

__all__ = [
    "DEFAULT_TELEMETRY_TYPE",
    "TelemetryType",
    "VALID_TELEMETRY_TYPE_SET",
    "VALID_TELEMETRY_TYPES",
]
