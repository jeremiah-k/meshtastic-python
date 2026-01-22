"""Error handling utilities for BLE operations."""

from concurrent.futures import TimeoutError as FutureTimeoutError

from bleak.exc import BleakDBusError, BleakError

from meshtastic.interfaces.ble.constants import logger

try:
    from google.protobuf.message import DecodeError
except ImportError:

    class DecodeError(Exception):  # type: ignore
        """Fallback for when google.protobuf is not available."""

        pass


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
        Execute a zero-argument callable and return its result, falling back to a default value on error.

        Parameters
        ----------
        func : Any
            callable Zero-argument callable to execute.
        default_return : Any
            Value returned when execution fails.
        log_error : Any
            If True, log caught exceptions.
        error_msg : Any
            Message prefix used when logging errors.
        reraise : Any
            If True, re-raise the caught exception instead of returning `default_return`.

        Returns
        -------
            The value returned by `func()` on success, or `default_return` if execution fails.

        Raises
        ------
        Exception: Re-raises the original exception if `reraise` is True.

        Notes
        -----
            Handled exceptions include BleakError, BleakDBusError, DecodeError, and FutureTimeoutError; other exceptions are also caught and treated the same.
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
    def safe_cleanup(func, cleanup_name: str = "cleanup operation") -> bool:
        """
        Perform a cleanup callable and suppress any exceptions.

        Parameters
        ----------
        func : Any
            callable Cleanup operation to execute; called with no arguments.
        cleanup_name : Any
            Descriptive name included in debug log messages.

        Returns
        -------
                success (bool): True if the cleanup completed without raising an exception, False otherwise.
        """
        try:
            func()
            return True
        except Exception as e:
            logger.debug("Error during %s: %s", cleanup_name, e)
            return False
