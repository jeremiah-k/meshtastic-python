"""Serial interface class for communicating with Meshtastic devices over serial connections.

This module provides the SerialInterface class which handles communication with
Meshtastic devices via USB/serial connections.
"""

import contextlib
import logging
import sys
import time
import types
from typing import IO

import serial  # type: ignore[import-untyped]

import meshtastic.util
from meshtastic.stream_interface import StreamInterface

logger = logging.getLogger(__name__)


class SerialInterface(StreamInterface):
    """Interface class for meshtastic devices over a serial link."""

    # pylint: disable=R0917
    def __init__(
        self,
        devPath: str | None = None,
        debugOut: IO[str] | None = None,
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
        debugOut : IO[str] | None
            Optional stream to emit raw debug serial output. (Default value = None)
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
        self.stream: serial.Serial | None = None  # Initialize early for safe cleanup

        self.devPath: str | None = devPath

        if self.devPath is None:
            ports: list[str] = meshtastic.util.findPorts(True)
            logger.debug("ports: %s", ports)
            if len(ports) == 0:
                logger.warning(
                    "No serial Meshtastic device detected; creating StreamInterface fallback without a serial connection."
                )
                # Ensure base classes are initialized so close() is safe
                StreamInterface.__init__(
                    self,
                    debugOut=debugOut,
                    noProto=noProto,
                    connectNow=False,
                    noNodes=noNodes,
                    timeout=timeout,
                )
                return
            elif len(ports) > 1:
                message: str = (
                    "Multiple serial ports were detected; one serial port must be specified with '--port'.\n"
                )
                message += f"  Ports detected: {ports}"
                raise self.MeshInterfaceError(message)
            else:
                self.devPath = ports[0]

        logger.debug("Connecting to %s", self.devPath)

        if sys.platform != "win32":
            with open(self.devPath, encoding="utf8") as f:
                self._set_hupcl_with_termios(f)
            time.sleep(0.1)

        self.stream = serial.Serial(
            self.devPath, 115200, exclusive=True, timeout=0.5, write_timeout=0
        )
        initialized = False
        try:
            self.stream.flush()
            time.sleep(0.1)
            StreamInterface.__init__(
                self,
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
                    self.stream.close()
                    self.stream = None

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
        if hasattr(self, "debugOut") and self.debugOut is not None:
            rep += f", debugOut={self.debugOut!r}"
        if self.noProto:
            rep += ", noProto=True"
        if hasattr(self, "noNodes") and self.noNodes:
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
            # adapters and hardware configurations.
            with contextlib.suppress(Exception):
                stream.flush()
                time.sleep(0.1)
                stream.flush()
                time.sleep(0.1)
        logger.debug("Closing Serial stream")
        StreamInterface.close(self)

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
            Always returns None, as per context manager protocol.
        """
        return super().__exit__(exc_type, exc_val, exc_tb)
