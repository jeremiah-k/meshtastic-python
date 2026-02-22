"""Error handling utilities for BLE operations."""

from concurrent.futures import TimeoutError as FutureTimeoutError
from typing import Any, Callable, Type, TypeVar

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

T = TypeVar("T")


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
    def safeExecute(
        func: Callable[[], T],
        default_return: T | None = None,
        log_error: bool = True,
        error_msg: str = "Error in operation",
        reraise: bool = False,
    ) -> T | None:
        """
        Execute a zero-argument callable, returning its result or a fallback on handled errors.

        Parameters
        ----------
            func: Zero-argument callable to execute.
            default_return: Value to return if execution fails due to a handled exception.
            log_error: If True, log caught exceptions using the configured logger.
            error_msg: Message prefix used when logging errors.
            reraise: If True, re-raise caught exceptions instead of returning `default_return`.

        Notes:
        -----
            SystemExit and KeyboardInterrupt are re-raised and not swallowed.
            Specifically handles BleakError, DecodeError, and FutureTimeoutError; other exceptions are also caught and treated per `log_error`/`reraise`.

        Returns:
        -------
            The value returned by `func()` on success, or `default_return` if execution fails.

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
    def _safe_execute(*args, **kwargs):
        """
        Provides a backward-compatible snake_case entry point that executes a callable using the module's BLE error handling.

        Returns:
            The callable's result, or the provided `default_return` if a handled BLE-related error occurred; may re-raise the original exception when configured.
        """
        return BLEErrorHandler.safeExecute(*args, **kwargs)

    @staticmethod
    def safe_execute(*args, **kwargs):
        """
        Execute a zero-argument callable with centralized BLE error handling and return its result or a provided default on handled errors.

        Returns:
            The callable's return value, or the provided `default_return` when a handled BLE-related error occurs.
        """
        return BLEErrorHandler.safeExecute(*args, **kwargs)

    @staticmethod
    def safeCleanup(
        func: Callable[[], Any], cleanup_name: str = "cleanup operation"
    ) -> bool:
        """
        Run a zero-argument cleanup callable and suppress any exceptions.

        Logs a debug message that includes `cleanup_name` if the callable raises an exception.

        Parameters:
            func (Callable[[], Any]): Cleanup operation to execute with no arguments.
            cleanup_name (str): Human-readable name included in debug messages (default: "cleanup operation").

        Returns:
            bool: `True` if the cleanup completed without raising an exception, `False` otherwise.
        """
        try:
            func()
        except (SystemExit, KeyboardInterrupt):
            raise
        except Exception as e:  # noqa: BLE001 - cleanup must never raise
            logger.debug("Error during %s: %s", cleanup_name, e)
            return False
        else:
            return True

    @staticmethod
    def _safe_cleanup(*args, **kwargs):
        """
        Execute a cleanup callable and return whether it completed without raising an exception.

        Parameters:
            *args: Positional arguments forwarded to the cleanup call; expected usage is a single zero-argument callable.
            **kwargs: Keyword arguments forwarded to the cleanup call; supports `cleanup_name` (str) to label the operation in logs.

        Returns:
            bool: `True` if the cleanup callable completed without raising (excluding `SystemExit` and `KeyboardInterrupt`), `False` if an exception was caught.
        """
        return BLEErrorHandler.safeCleanup(*args, **kwargs)

    @staticmethod
    def safe_cleanup(*args, **kwargs):
        """Backward-compatible snake_case alias for safeCleanup."""
        return BLEErrorHandler.safeCleanup(*args, **kwargs)
