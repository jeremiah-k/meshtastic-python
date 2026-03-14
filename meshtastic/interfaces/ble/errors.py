"""Error handling utilities for BLE operations."""

from concurrent.futures import TimeoutError as FutureTimeoutError
from typing import Any, Callable, TypeVar

from bleak.exc import BleakError

from meshtastic.interfaces.ble.constants import logger

# Import DecodeError from protobuf, or create a fallback if not available
DecodeError: type[Exception]
try:
    from google.protobuf.message import DecodeError as _ProtobufDecodeError
except ImportError as exc:
    missing_name = getattr(exc, "name", None)
    if missing_name not in ("google", "google.protobuf", "google.protobuf.message"):
        raise

    class _FallbackDecodeError(Exception):
        """Fallback DecodeError when protobuf is unavailable."""

    DecodeError = _FallbackDecodeError
else:
    DecodeError = _ProtobufDecodeError


# Public exports for BLE error handling utilities.
__all__ = ["BLEErrorHandler", "DecodeError"]

T = TypeVar("T")


class BLEErrorHandler:
    """Internal helper class for consistent BLE error handling patterns.

    Static methods are intentionally underscore-prefixed because they are internal
    orchestration helpers, not public BLE interface APIs.
    """

    @staticmethod
    def _safe_execute(
        func: Callable[[], T],
        default_return: T | None = None,
        log_error: bool = True,
        error_msg: str = "Error in operation",
        reraise: bool = False,
    ) -> T | None:
        """Execute a callable with standardized BLE exception handling.

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
        except (
            Exception  # noqa: BLE001 - intentional catch-all; caller controls reraise
        ):
            if log_error:
                logger.exception("%s", error_msg)
            if reraise:
                raise
            return default_return

    @staticmethod
    def safe_execute(
        func: Callable[[], T],
        default_return: T | None = None,
        *,
        log_error: bool = True,
        error_msg: str = "Error in operation",
        reraise: bool = False,
    ) -> T | None:
        """Public wrapper for standardized guarded callable execution."""
        return BLEErrorHandler._safe_execute(
            func=func,
            default_return=default_return,
            log_error=log_error,
            error_msg=error_msg,
            reraise=reraise,
        )

    @staticmethod
    def _safe_cleanup(
        func: Callable[[], Any], cleanup_name: str = "cleanup operation"
    ) -> bool:
        """Run cleanup callable and suppress non-fatal exceptions.

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
    def safe_cleanup(
        func: Callable[[], Any], cleanup_name: str = "cleanup operation"
    ) -> bool:
        """Public wrapper for best-effort cleanup execution."""
        return BLEErrorHandler._safe_cleanup(func=func, cleanup_name=cleanup_name)
