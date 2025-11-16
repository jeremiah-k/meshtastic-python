"""Shared BLE-specific exceptions and error handling helpers."""

from __future__ import annotations

from concurrent.futures import TimeoutError as FutureTimeoutError
from typing import TYPE_CHECKING

from bleak.exc import BleakDBusError, BleakError

from .config import logger

if TYPE_CHECKING:
    class DecodeError(Exception):
        """Fallback DecodeError type used for static type checking."""

        pass
else:  # pragma: no cover - import real exception only at runtime
    from google.protobuf.message import DecodeError


class BLEError(Exception):
    """Base class for BLE interface errors."""


class BLEErrorHandler:
    """Helper class for consistent error handling in BLE operations."""

    @staticmethod
    def safe_execute(
        func,
        default_return=None,
        log_error: bool = True,
        error_msg: str = "Error in operation",
        reraise: bool = False,
    ):
        """
        Execute a callable and return its result while converting handled exceptions into a default value.
        """
        try:
            return func()
        except (BleakError, BleakDBusError, DecodeError, FutureTimeoutError) as e:
            if log_error:
                logger.debug("%s: %s", error_msg, e)
            if reraise:
                raise
            return default_return
        except Exception:  # noqa: BLE001 - catch-all to keep BLE helpers resilient
            if log_error:
                logger.exception("%s", error_msg)
            if reraise:
                raise
            return default_return

    @staticmethod
    def safe_cleanup(func, cleanup_name: str = "cleanup operation"):
        """Safely execute cleanup operations without raising exceptions."""
        try:
            func()
        except Exception as e:  # noqa: BLE001 - cleanup must never raise
            logger.debug("Error during %s: %s", cleanup_name, e)


__all__ = ["BLEError", "BLEErrorHandler"]
