"""Explicit failure dispositions for BLE runtime error handling."""

from __future__ import annotations

import logging
from enum import Enum

from meshtastic.interfaces.ble.constants import logger


class _BLEFailureDisposition(str, Enum):
    """How a caught BLE failure affects the current operation."""

    COMPATIBILITY_FALLBACK = "compatibility_fallback"
    BEST_EFFORT = "best_effort"
    RETRYABLE = "retryable"
    TERMINAL = "terminal"


def _log_ble_failure(
    disposition: _BLEFailureDisposition,
    message: str,
    *args: object,
    level: int = logging.DEBUG,
    exc_info: bool = True,
) -> None:
    """Log a caught BLE failure without changing its user-visible message.

    Parameters
    ----------
    disposition : _BLEFailureDisposition
        Semantic outcome of the caught failure.
    message : str
        Existing log message format.
    *args : object
        Values interpolated by the logger.
    level : int
        Logging level preserving the caller's historical severity.
    exc_info : bool
        Whether to attach the active exception traceback.

    Notes
    -----
    The disposition is attached as structured ``LogRecord`` metadata so tests,
    diagnostics, and future telemetry can reason about policy without changing
    historical log text.
    """
    logger.log(
        level,
        message,
        *args,
        exc_info=exc_info,
        extra={"ble_failure_disposition": disposition.value},
    )
