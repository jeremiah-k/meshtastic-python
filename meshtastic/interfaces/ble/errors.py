"""Error handling utilities for BLE operations."""

from concurrent.futures import TimeoutError as FutureTimeoutError
from typing import Type

from bleak.exc import BleakDBusError, BleakError

from meshtastic.interfaces.ble.constants import logger

# Import DecodeError from protobuf, or create a fallback if not available
try:
    from google.protobuf.message import DecodeError as _DecodeError  # type: ignore

    DecodeError = _DecodeError
except ImportError:
    DecodeError = type("DecodeError", (Exception,), {})


__all__ = ["BLEErrorHandler", "DecodeError"]


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
        """
        Safely execute a zero-argument callable and return its result, falling back to a default on error.

        Parameters:
            func: Zero-argument callable to execute.
            default_return: Value returned if execution fails.
            log_error (bool): If True, log caught exceptions.
            error_msg (str): Message prefix used when logging errors.
            reraise (bool): If True, re-raise caught exceptions instead of returning default_return.

        Returns:
            The value returned by func() on success, or default_return if execution fails.

        Notes:
            Catches BleakError, BleakDBusError, DecodeError, FutureTimeoutError, and other Exceptions; SystemExit and KeyboardInterrupt are re-raised.
        """
        try:
            return func()
        except (BleakError, BleakDBusError, DecodeError, FutureTimeoutError) as e:
            if log_error:
                logger.debug("%s: %s", error_msg, e)
            if reraise:
                raise
            return default_return
        except (SystemExit, KeyboardInterrupt):  # pylint: disable=W0706
            raise
        except Exception:
            if log_error:
                logger.exception("%s", error_msg)
            if reraise:
                raise
            return default_return

    @staticmethod
    def safe_cleanup(func, cleanup_name: str = "cleanup operation") -> bool:
        """
        Execute a cleanup callable and return whether it succeeded.

        Calls the provided zero-argument callable and suppresses any exception raised, logging a debug message that includes `cleanup_name` when an exception occurs.

        Parameters:
            func (Callable[[], Any]): Cleanup operation to execute with no arguments.
            cleanup_name (str): Human-readable name included in debug messages (default: "cleanup operation").

        Returns:
            bool: `True` if the cleanup completed without raising an exception, `False` otherwise.
        """
        try:
            func()
            return True
        except Exception as e:
            logger.debug("Error during %s: %s", cleanup_name, e)
            return False
