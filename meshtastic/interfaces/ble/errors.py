"""Error handling utilities for BLE operations."""

from concurrent.futures import TimeoutError as FutureTimeoutError
from typing import Any, Callable, TypeVar

from bleak.exc import BleakDBusError, BleakDeviceNotFoundError, BleakError

from meshtastic.interfaces.ble.constants import logger
from meshtastic.mesh_interface import MeshInterface

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
__all__ = [
    "MeshtasticBLEError",
    "BLEDiscoveryError",
    "BLEDeviceNotFoundError",
    "BLEConnectionSuppressedError",
    "BLEConnectionTimeoutError",
    "BLEAddressMismatchError",
    "BLEDBusTransportError",
    "BLEErrorHandler",
    "DecodeError",
]

T = TypeVar("T")


class MeshtasticBLEError(MeshInterface.MeshInterfaceError):
    """Base exception for structured Meshtastic BLE failures."""

    def __init__(
        self,
        message: str,
        *,
        address: str | None = None,
        requested_identifier: str | None = None,
        timeout: float | None = None,
        connected_address: str | None = None,
        cause: BaseException | None = None,
    ) -> None:
        """Create a BLE error with optional structured context attributes."""
        # Avoid cooperative super() here so multiple-inheritance variants
        # (for example BleakDBusError mixins) do not require foreign
        # constructor signatures.
        self.message = message
        Exception.__init__(self, message)
        self.address = address
        self.requested_identifier = requested_identifier
        self.timeout = timeout
        self.connected_address = connected_address
        self.cause = cause


class BLEDiscoveryError(MeshtasticBLEError):
    """Raised when BLE discovery cannot resolve a usable target."""


class BLEDeviceNotFoundError(BLEDiscoveryError, BleakDeviceNotFoundError):
    """Raised when a specific BLE target cannot be located."""


class BLEConnectionSuppressedError(MeshtasticBLEError):
    """Raised when duplicate-connect gating suppresses a connection attempt."""


class BLEConnectionTimeoutError(MeshtasticBLEError, TimeoutError):
    """Raised when BLE connection setup exceeds a timeout budget."""

    @classmethod
    def from_exception(
        cls,
        error: BaseException,
        *,
        message: str,
        requested_identifier: str | None = None,
        timeout: float | None = None,
    ) -> "BLEConnectionTimeoutError":
        """Normalize a timeout failure with structured metadata."""
        return cls(
            message,
            requested_identifier=requested_identifier,
            timeout=timeout,
            cause=error,
        )


class BLEAddressMismatchError(MeshtasticBLEError):
    """Raised when explicit-address connect resolves to a different peer."""


class BLEDBusTransportError(MeshtasticBLEError, BleakDBusError):
    """Raised for normalized BlueZ/DBus transport failures."""

    @classmethod
    def from_exception(
        cls,
        error: BaseException,
        *,
        message: str,
        requested_identifier: str | None = None,
        address: str | None = None,
    ) -> "BLEDBusTransportError":
        """Normalize DBus transport failures while preserving the original cause."""
        return cls(
            message,
            requested_identifier=requested_identifier,
            address=address,
            cause=error,
        )


class BLEErrorHandler:
    """Shared helper class for consistent BLE error handling patterns.

    The underscore-prefixed methods implement canonical behavior, and
    ``safe_execute`` / ``safe_cleanup`` provide stable public compatibility
    aliases that delegate to those internal helpers.
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
            Exception
        ):  # noqa: BLE001 - intentional catch-all; caller controls reraise
            if log_error:
                logger.exception("%s", error_msg)
            if reraise:
                raise
            return default_return

    # COMPAT_STABLE_SHIM: Public compatibility alias; delegates to _safe_execute.
    @classmethod
    def safe_execute(  # noqa: FBT001,FBT002 - compatibility shim intentionally preserves legacy positional booleans.
        cls,
        func: Callable[[], T],
        default_return: T | None = None,
        log_error: bool = True,  # noqa: FBT001,FBT002
        error_msg: str = "Error in operation",
        reraise: bool = False,  # noqa: FBT001,FBT002
    ) -> T | None:
        """Execute a callable with standardized guarded BLE error handling.

        Parameters
        ----------
        func : Callable[[], T]
            Zero-argument callable to execute.
        default_return : T | None
            Value returned when a handled exception occurs and ``reraise`` is
            False. (Default value = None)
        log_error : bool
            Whether caught exceptions should be logged. (Default value = True)
        error_msg : str
            Message prefix used for logging when an exception is handled.
            (Default value = "Error in operation")
        reraise : bool
            When True, handled exceptions are re-raised after logging.
            (Default value = False)

        Returns
        -------
        T | None
            Return value from ``func`` on success, otherwise ``default_return``
            when a handled exception occurs and ``reraise`` is False.
        """
        return cls._safe_execute(
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

    # COMPAT_STABLE_SHIM: Public compatibility alias; delegates to _safe_cleanup.
    @classmethod
    def safe_cleanup(
        cls, func: Callable[[], Any], cleanup_name: str = "cleanup operation"
    ) -> bool:
        """Run a cleanup callable and suppress non-fatal cleanup exceptions.

        Parameters
        ----------
        func : Callable[[], Any]
            Cleanup callback invoked with no arguments.
        cleanup_name : str
            Human-readable operation label included in debug logs when cleanup
            fails. (Default value = "cleanup operation")

        Returns
        -------
        bool
            True when cleanup completed without exceptions, otherwise False.
        """
        return cls._safe_cleanup(func=func, cleanup_name=cleanup_name)
