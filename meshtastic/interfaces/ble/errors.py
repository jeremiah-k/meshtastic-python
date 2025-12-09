"""Error handling utilities for BLE operations."""

import logging
from concurrent.futures import Future
from concurrent.futures import TimeoutError as FutureTimeoutError
from typing import Any, Callable, Optional, TYPE_CHECKING

from bleak.exc import BleakDBusError, BleakError

from meshtastic.interfaces.ble.constants import logger

if TYPE_CHECKING:
    from google.protobuf.message import DecodeError
else:
    try:
        from google.protobuf.message import DecodeError  # type: ignore
    except ImportError:
        class DecodeError(Exception):
            """Fallback DecodeError type used for static type checking."""
            pass

__all__ = ["BLEErrorHandler"]

class BLEErrorHandler:
    """Helper class for consistent error handling in BLE operations.

    This class provides static methods for standardized error handling patterns
    throughout the BLE interface. It centralizes error logging and recovery strategies.

    Features:
        - Safe execution with fallback return values
        - Consistent error logging and classification
        - Cleanup operations that never raise exceptions
    """

    @staticmethod
    def safe_execute(
        func,
        default_return=None,
        log_error: bool = True,
        error_msg: str = "Error in operation",
        reraise: bool = False,
    ):
        """Execute a callable and return its result while converting handled exceptions into a default value.

        Args:
        ----
            func (callable): A zero-argument callable to execute.
            default_return: Value to return if execution fails; defaults to None.
            log_error (bool): If True, log caught exceptions; defaults to True.
            error_msg (str): Message used when logging errors; defaults to "Error in operation".
            reraise (bool): If True, re-raise any caught exception instead of returning default_return.

        Returns:
        -------
            The value returned by `func()` on success, or `default_return` if a handled exception occurs.

        Raises:
        ------
            Exception: Re-raises the original exception if `reraise` is True.

        Notes:
        -----
            Handled exceptions include BleakError, BleakDBusError, DecodeError, and FutureTimeoutError;
        all other exceptions are also caught and treated the same.

        """
        try:
            return func()
        except (BleakError, BleakDBusError, DecodeError, FutureTimeoutError) as e:
            if log_error:
                logger.debug("%s: %s", error_msg, e)
            if reraise:
                raise
            return default_return
        except Exception:
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
        except Exception as e:
            logger.debug("Error during %s: %s", cleanup_name, e)
