"""Error handling for BLE operations."""
import logging
from concurrent.futures import TimeoutError as FutureTimeoutError
from typing import TYPE_CHECKING

from bleak.exc import BleakDBusError, BleakError

if TYPE_CHECKING:
    class DecodeError(Exception):
        """Fallback DecodeError type used for static type checking."""
        pass
else:  # pragma: no cover - import real exception only at runtime
    from google.protobuf.message import DecodeError

logger = logging.getLogger(__name__)


class BLEErrorHandler:
    """
    Helper class for consistent error handling in BLE operations.

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
        Execute a zero-argument callable and return its result, falling back to a provided default on failure.
        
        Handles BLE- and decode-related exceptions (BleakError, BleakDBusError, DecodeError, FutureTimeoutError) as well as other exceptions; handled exceptions are logged when requested.
        
        Parameters:
            func (callable): A zero-argument callable to execute.
            default_return: Value to return if execution fails.
            log_error (bool): If True, log caught exceptions.
            error_msg (str): Message prefix used when logging errors.
            reraise (bool): If True, re-raise any caught exception instead of returning default_return.
        
        Returns:
            The value returned by `func()` on success, or `default_return` if execution failed.
        
        Raises:
            Exception: Re-raises the original exception if `reraise` is True.
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
        """
        Execute a cleanup callable and suppress any exceptions raised during its execution.
        
        If the callable raises an exception, the error is caught and logged at debug level with the provided cleanup name; exceptions are not propagated.
        
        Parameters:
            func (Callable[[], Any]): Zero-argument cleanup function to execute.
            cleanup_name (str): Human-readable name for the cleanup operation used in the log message.
        """
        try:
            func()
        except Exception as e:  # noqa: BLE001 - cleanup paths must not raise
            logger.debug("Error during %s: %s", cleanup_name, e)