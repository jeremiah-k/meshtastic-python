"""Serial interface class for communicating with Meshtastic devices over serial connections.

This module provides the SerialInterface class which handles communication with
Meshtastic devices via USB/serial connections.
"""

import contextlib
import logging
import os
import sys
import time
import types
from typing import IO, Any, BinaryIO, Callable

import serial  # type: ignore[import-untyped]

import meshtastic.util
from meshtastic.stream_interface import StreamInterface

logger = logging.getLogger(__name__)

# Serial interface constants
DEFAULT_BAUD_RATE = 115200
"""Default baud rate for serial communication."""

SERIAL_READ_TIMEOUT = 0.5
"""Default read timeout for serial operations (seconds)."""

SERIAL_WRITE_TIMEOUT = 3.0
"""Default write timeout for serial operations (seconds).

Increased from 0.5s to accommodate rapid-fire admin messages during
setURL replace-all operations, which write up to 8 channel snapshots
plus LoRa config in quick succession over serial connections.
"""

SERIAL_SETTLING_DELAY = 0.1
"""Delay for serial port operations to settle (seconds)."""

SERIAL_CONNECT_ATTEMPTS = 3
"""Maximum serial connect attempts for transient reboot/disconnect windows."""

SERIAL_CONNECT_RETRY_DELAY_SECONDS = 1.5
"""Delay between serial connect retry attempts."""

SERIAL_PORT_PATH_EMPTY_ERROR = (
    "Serial port path cannot be empty; pass None to auto-detect."
)


class SerialInterface(StreamInterface):
    """Interface class for meshtastic devices over a serial link."""

    devPath: str | None
    stream: serial.Serial | BinaryIO | None

    def _open_serial_stream(self) -> serial.Serial:
        """Open and return a configured serial stream for this interface."""
        if self.devPath is None:
            resolved_dev_path = self._resolve_dev_path()
            if resolved_dev_path is None:
                raise self.MeshInterfaceError(
                    "No serial Meshtastic device detected for reconnect."
                )
            self.devPath = resolved_dev_path

        if not getattr(self, "noProto", False) and not os.path.exists(self.devPath):
            raise self.MeshInterfaceError(
                f"Serial port {self.devPath} does not exist (device disconnected)"
            )

        if sys.platform != "win32":
            with open(self.devPath, encoding="utf8") as f:
                self._set_hupcl_with_termios(f)
            time.sleep(SERIAL_SETTLING_DELAY)

        serial_kwargs: dict[str, Any] = {
            "timeout": SERIAL_READ_TIMEOUT,
            "write_timeout": SERIAL_WRITE_TIMEOUT,
        }
        if sys.platform != "win32":
            serial_kwargs["exclusive"] = True

        stream = serial.Serial(
            self.devPath,
            DEFAULT_BAUD_RATE,
            **serial_kwargs,
        )
        stream.flush()
        time.sleep(SERIAL_SETTLING_DELAY)
        if stream.in_waiting:
            stream.reset_input_buffer()
        return stream

    def _resolve_dev_path(self) -> str | None:
        """Return an explicit or auto-detected serial device path.

        Returns
        -------
        str | None
            Resolved serial port path, or ``None`` when no ports are detected.

        Raises
        ------
        MeshInterfaceError
            If an explicit path is empty/whitespace-only or if multiple ports are
            detected without an explicit ``--port`` selection.
        """
        if self.devPath is not None:
            stripped_dev_path = self.devPath.strip()
            if not stripped_dev_path:
                raise self.MeshInterfaceError(SERIAL_PORT_PATH_EMPTY_ERROR)
            return stripped_dev_path

        ports: list[str] = meshtastic.util.findPorts(eliminate_duplicates=True)
        logger.debug("ports: %s", ports)
        if len(ports) == 0:
            return None
        if len(ports) > 1:
            message: str = "Multiple serial ports were detected; one serial port must be specified with '--port'.\n"
            message += (
                "  Auto-detection cannot disambiguate when multiple compatible devices "
                "or overlapping USB VID/PID aliases are present.\n"
            )
            message += f"  Ports detected: {ports}"
            raise self.MeshInterfaceError(message)
        return ports[0]

    # pylint: disable=R0917
    def __init__(
        self,
        devPath: str | None = None,
        debugOut: IO[str] | Callable[[str], Any] | None = None,
        noProto: bool = False,
        connectNow: bool = True,
        noNodes: bool = False,
        timeout: float = 300.0,
    ) -> None:
        """Initialize the SerialInterface and open a serial connection to a Meshtastic device when available.

        Parameters
        ----------
        devPath : str | None
            Filesystem path to a serial device (e.g.,
            "/dev/ttyUSB0"). If None, a single available Meshtastic port will be
            auto-detected; if none are found, a fallback StreamInterface without a
            serial connection is created. (Default value = None)
        debugOut : IO[str] | Callable[[str], Any] | None
            Optional stream or callable to emit raw debug serial output. (Default value = None)
        noProto : bool
            Disable higher-level protocol handling when True. (Default value = False)
        connectNow : bool
            If True, perform connection and setup actions immediately after opening the serial stream. (Default value = True)
        noNodes : bool
            Disable node discovery and management when True. (Default value = False)
        timeout : float
            Time in seconds to wait for replies or other operations. (Default value = 300.0)

        Raises
        ------
        MeshInterfaceError
            When multiple serial ports are detected and none was explicitly specified.
        """
        self.noProto = noProto
        self.stream = None  # Initialize early for safe cleanup
        self._dev_path_auto_detected = False

        self.devPath = devPath
        resolved_dev_path = self._resolve_dev_path()
        if resolved_dev_path is None:
            logger.info(
                "No serial Meshtastic device detected; creating StreamInterface fallback without a serial connection."
            )
            # Ensure base classes are initialized so close() is safe.
            # Use noProto=True for fallback since no stream is available.
            self.noProto = True
            super().__init__(
                debugOut=debugOut,
                noProto=True,
                connectNow=False,
                noNodes=noNodes,
                timeout=timeout,
            )
            return
        self.devPath = resolved_dev_path
        self._dev_path_auto_detected = devPath is None

        logger.debug("Connecting to %s", self.devPath)

        self.stream = self._open_serial_stream()
        initialized = False
        try:
            super().__init__(
                debugOut=debugOut,
                noProto=noProto,
                connectNow=connectNow,
                noNodes=noNodes,
                timeout=timeout,
            )
            initialized = True
        finally:
            if self.stream is not None:
                if not initialized:
                    # Ensure stream lock is released when base initialization fails.
                    with contextlib.suppress(
                        OSError, ValueError, serial.SerialException
                    ):
                        self.stream.close()
                    self.stream = None

    def connect(self) -> None:
        """Reconnect by reopening serial stream when needed, then run StreamInterface connect."""
        for attempt in range(SERIAL_CONNECT_ATTEMPTS):
            try:
                super().connect()
                return
            except Exception as exc:
                if (
                    attempt + 1 >= SERIAL_CONNECT_ATTEMPTS
                    or not self._is_retryable_connect_error(exc)
                ):
                    raise
                logger.warning(
                    "Serial connect attempt %d/%d failed: %s. Retrying in %.1fs...",
                    attempt + 1,
                    SERIAL_CONNECT_ATTEMPTS,
                    exc,
                    SERIAL_CONNECT_RETRY_DELAY_SECONDS,
                )
                with self._connect_lock:
                    with contextlib.suppress(
                        OSError, ValueError, serial.SerialException
                    ):
                        if self.stream is not None and getattr(
                            self.stream, "is_open", True
                        ):
                            self.stream.close()
                    self.stream = None
                if self._dev_path_auto_detected:
                    self.devPath = None
                time.sleep(SERIAL_CONNECT_RETRY_DELAY_SECONDS)

    def _ensure_stream_for_connect_locked(self, *, requires_stream: bool) -> None:
        """Open/reopen serial stream atomically under StreamInterface connect lock."""
        if not requires_stream:
            return
        if self.stream is None or not getattr(self.stream, "is_open", True):
            self.stream = self._open_serial_stream()

    def _is_retryable_connect_error(self, exc: Exception) -> bool:
        """Return True when serial connect failures are likely transient."""
        if isinstance(
            exc,
            (
                serial.SerialException,
                OSError,
                StreamInterface.StreamClosedError,
            ),
        ):
            return True
        message = str(exc)
        if isinstance(exc, self.MeshInterfaceError):
            return (
                "Timed out waiting for connection completion" in message
                or "Connection lost while waiting for connection completion" in message
                or "No serial Meshtastic device detected for reconnect." in message
            )
        return False

    def _set_hupcl_with_termios(self, f: IO[str]) -> None:
        """Clear the terminal HUPCL (hang-up-on-close) flag for the given device file to prevent the device from rebooting when RTS/DTR change.

        On Windows this is a no-op.

        Parameters
        ----------
        f : IO[str]
            Open file-like handle for the serial device whose terminal attributes will be adjusted.
        """
        if sys.platform == "win32":
            return

        import termios  # pylint: disable=C0415,E0401

        attrs = termios.tcgetattr(f)
        attrs[2] = attrs[2] & ~termios.HUPCL
        termios.tcsetattr(f, termios.TCSAFLUSH, attrs)

    def __repr__(self) -> str:
        """Provide a concise, machine-readable representation of the SerialInterface instance.

        Returns
        -------
        str
            A string like "SerialInterface(devPath=..., debugOut=..., noProto=True, noNodes=True)"
            that includes only the applicable fields (devPath always; debugOut, noProto, noNodes when present).
        """
        rep = f"SerialInterface(devPath={self.devPath!r}"
        debug_out = getattr(self, "debugOut", None)
        if debug_out is not None:
            rep += f", debugOut={debug_out!r}"
        if self.noProto:
            rep += ", noProto=True"
        if getattr(self, "noNodes", False):
            rep += ", noNodes=True"
        rep += ")"
        return rep

    def close(self) -> None:
        """Close the serial connection and ensure any pending outgoing data is transmitted.

        If a serial stream exists, flushes pending outgoing data before closing and then
        delegates remaining cleanup to StreamInterface.close(). This operation may block
        briefly while flushing.
        """
        stream = self.stream
        if stream is not None and getattr(stream, "is_open", True):
            # Flush and sleep to ensure all pending data is transmitted before closing.
            # This workaround ensures the device receives all data before the serial
            # connection is terminated, particularly important for some USB-serial
            # adapters and hardware configurations. SERIAL_SETTLING_DELAY (100 ms)
            # is an empirically chosen compromise that gives common USB serial
            # stacks time to drain host-side buffers; running the cycle twice has
            # proven more reliable for delivering trailing bytes before close().
            with contextlib.suppress(OSError, ValueError, serial.SerialException):
                stream.flush()
                time.sleep(SERIAL_SETTLING_DELAY)
                stream.flush()
                time.sleep(SERIAL_SETTLING_DELAY)
        logger.debug("Closing Serial stream")
        try:
            super().close()
        finally:
            self.stream = None

    def __enter__(self) -> "SerialInterface":
        """Provide the SerialInterface instance for use in a with-statement.

        Returns
        -------
        self : 'SerialInterface'
            The same SerialInterface instance.
        """
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: types.TracebackType | None,
    ) -> None:
        """Handle exiting a context manager and delegate cleanup and exception propagation to the base class.

        When used as a context manager exit hook, forwards any exception information to the superclass so it can perform cleanup and logging.

        Parameters
        ----------
        exc_type : type[BaseException] | None
            The exception class if an exception was raised, otherwise None.
        exc_val : BaseException | None
            The exception instance if raised, otherwise None.
        exc_tb : types.TracebackType | None
            The traceback object for the exception, or None.

        Returns
        -------
        None
            Always returns None to ensure exceptions are never suppressed,
            regardless of the base class behavior.
        """
        super().__exit__(exc_type, exc_val, exc_tb)
