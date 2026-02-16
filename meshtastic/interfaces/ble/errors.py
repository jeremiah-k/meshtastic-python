"""Error handling utilities for BLE operations."""

from concurrent.futures import TimeoutError as FutureTimeoutError
from typing import Any, Callable, Type

from bleak.exc import BleakError

from meshtastic.interfaces.ble.constants import logger

# Import DecodeError from protobuf, or create a fallback if not available
DecodeError: Type[Exception]
try:
    from google.protobuf.message import DecodeError as _ProtobufDecodeError
except ImportError:

    class _FallbackDecodeError(Exception):
        """Fallback DecodeError when protobuf is unavailable."""

    DecodeError = _FallbackDecodeError
else:
    DecodeError = _ProtobufDecodeError


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
        func: Callable[[], Any],
        default_return: Any = None,
        log_error: bool = True,
        error_msg: str = "Error in operation",
        reraise: bool = False,
    ) -> Any:
        """
        Execute a zero-argument callable and return its result, or fall back to a provided default or re-raise on error.

        Parameters
        ----------
            func (Callable[[], Any]): Zero-argument callable to execute.
            default_return (Any): Value to return if execution fails.
            log_error (bool): If True, log caught exceptions.
            error_msg (str): Message prefix used when logging errors.
            reraise (bool): If True, re-raise caught exceptions instead of returning `default_return`.

        Returns
        -------
            Any: The value returned by `func()` on success, or `default_return` if execution fails.

        """
        try:
            return func()
        except (
            BleakError,
            DecodeError,
            FutureTimeoutError,
        ) as e:  # BleakError includes BleakDBusError
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
    def safe_cleanup(
        func: Callable[[], Any], cleanup_name: str = "cleanup operation"
    ) -> bool:
        """
        Execute a cleanup callable and return whether it succeeded.

        Calls the provided zero-argument callable and suppresses any exception raised, logging a debug message that includes `cleanup_name` when an exception occurs.

        Parameters
        ----------
            func (Callable[[], Any]): Cleanup operation to execute with no arguments.
            cleanup_name (str): Human-readable name included in debug messages (default: "cleanup operation").

        Returns
        -------
            bool: `True` if the cleanup completed without raising an exception, `False` otherwise.

        """
        try:
            func()
            return True
        except Exception as e:
            logger.debug("Error during %s: %s", cleanup_name, e)
            return False
