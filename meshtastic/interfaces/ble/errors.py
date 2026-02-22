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
        """Execute a zero-argument callable, returning its result or a fallback on handled errors.

        Parameters
        ----------
        func : Callable[[], T]
            Zero-argument callable to execute.
        default_return : T | None
            Value to return if execution fails due to a handled exception. (Default value = None)
        log_error : bool
            If True, log caught exceptions using the configured logger. (Default value = True)
        error_msg : str
            Message prefix used when logging errors. (Default value = 'Error in operation')
        reraise : bool
            If True, re-raise caught exceptions instead of returning `default_return`.

        Returns
        -------
        T | None
            The value returned by `func()` on success, or `default_return` if execution fails.

        Raises
        ------
        BleakError
            If a BLE-specific error occurs and `reraise` is True.
        DecodeError
            If a protobuf decode error occurs and `reraise` is True.
        FutureTimeoutError
            If a timeout error occurs and `reraise` is True.
        Exception
            For other unexpected exceptions if `reraise` is True.

        Notes
        -----
        SystemExit and KeyboardInterrupt are always re-raised and not swallowed.
        Specifically handles BleakError, DecodeError, and FutureTimeoutError; other exceptions are also caught and treated per `log_error`/`reraise`.
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
        """Provides a backward-compatible snake_case entry point that executes a callable using the module's BLE error handling.

        Parameters
        ----------
        *args : Any
            Positional arguments forwarded to `safeExecute`.
        **kwargs : Any
            Keyword arguments forwarded to `safeExecute`.

        Returns
        -------
        Any
            The callable's result, or the provided `default_return` if a handled BLE-related error occurred; may re-raise the original exception when configured.
        """
        return BLEErrorHandler.safeExecute(*args, **kwargs)

    @staticmethod
    def safe_execute(*args, **kwargs):
        """Execute a zero-argument callable with centralized BLE error handling and return its result or a provided default on handled errors.

        Parameters
        ----------
        *args : Any
            Positional arguments forwarded to `safeExecute`.
        **kwargs : Any
            Keyword arguments forwarded to `safeExecute`.

        Returns
        -------
        Any
            The callable's return value, or the provided `default_return` when a handled BLE-related error occurs.
        """
        return BLEErrorHandler.safeExecute(*args, **kwargs)

    @staticmethod
    def safeCleanup(
        func: Callable[[], Any], cleanup_name: str = "cleanup operation"
    ) -> bool:
        """Run a zero-argument cleanup callable and suppress any exceptions.

        Logs a debug message that includes `cleanup_name` if the callable raises an exception.

        Parameters
        ----------
        func : Callable[[], Any]
            Cleanup operation to execute with no arguments.
        cleanup_name : str
            Human-readable name included in debug messages (default: "cleanup operation").

        Returns
        -------
        bool
            `True` if the cleanup completed without raising an exception, `False` otherwise.

        Raises
        ------
        SystemExit
            Always re-raised if caught during cleanup.
        KeyboardInterrupt
            Always re-raised if caught during cleanup.
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
        """Execute a cleanup callable and return whether it completed without raising an exception.

        Parameters
        ----------
        *args : Any
            Positional arguments forwarded to the cleanup call; expected usage is a single zero-argument callable.
        **kwargs : Any
            Keyword arguments forwarded to the cleanup call; supports `cleanup_name` (str) to label the operation in logs.

        Returns
        -------
        bool
            `True` if the cleanup callable completed without raising (excluding `SystemExit` and `KeyboardInterrupt`), `False` if an exception was caught.
        """
        return BLEErrorHandler.safeCleanup(*args, **kwargs)

    @staticmethod
    def safe_cleanup(*args, **kwargs):
        """Backward-compatible snake_case alias for safeCleanup.

        Parameters
        ----------
        *args : Any
            Positional arguments forwarded to .
        **kwargs : Any
            Keyword arguments forwarded to .

        Returns
        -------
        bool
            True if the cleanup completed without raising, False otherwise.
        """
        return BLEErrorHandler.safeCleanup(*args, **kwargs)
