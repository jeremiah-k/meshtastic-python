"""Serial interface class."""

import logging
import sys
import time
import types
from io import TextIOWrapper
from typing import List, Optional, Type

import serial  # type: ignore[import-untyped]

import meshtastic.util
from meshtastic.stream_interface import StreamInterface

logger = logging.getLogger(__name__)


class SerialInterface(StreamInterface):
    """Interface class for meshtastic devices over a serial link."""

    # pylint: disable=R0917
    def __init__(
        self,
        devPath: Optional[str] = None,
        debugOut=None,
        noProto: bool = False,
        connectNow: bool = True,
        noNodes: bool = False,
        timeout: float = 300.0,
    ) -> None:
        """
        Initialize the SerialInterface and, if a device is available, open a serial connection to a Meshtastic device.

        Parameters
        ----------
            devPath (Optional[str]): Filesystem path to a serial device (e.g., "/dev/ttyUSB0").
                If None, a single available Meshtastic port will be auto-detected; if none are
                found a fallback StreamInterface without a serial connection is created.
            debugOut: Optional stream to which raw debug serial output will be emitted.
            noProto (bool): If True, disable higher-level protocol handling.
            connectNow (bool): If True, perform connection/setup actions immediately after opening the serial stream.
            noNodes (bool): If True, disable node discovery/management.
            timeout (float): Time in seconds to wait for replies or operations.

        """
        self.noProto = noProto
        self.stream: Optional[serial.Serial] = None  # Initialize early for safe cleanup

        self.devPath: Optional[str] = devPath

        if self.devPath is None:
            ports: List[str] = meshtastic.util.findPorts(True)
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
                    connectNow=connectNow,
                    noNodes=noNodes,
                    timeout=timeout,
                )
                return
            elif len(ports) > 1:
                message: str = "Multiple serial ports were detected; one serial port must be specified with '--port'.\n"
                message += f"  Ports detected: {ports}"
                raise ValueError(message)
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
        self.stream.flush()  # type: ignore[attr-defined]
        time.sleep(0.1)

        StreamInterface.__init__(
            self,
            debugOut=debugOut,
            noProto=noProto,
            connectNow=connectNow,
            noNodes=noNodes,
            timeout=timeout,
        )

    def _set_hupcl_with_termios(self, f: TextIOWrapper):
        """
        Clear the terminal's HUPCL flag for the given device file to prevent the device from rebooting when RTS/DTR change.

        This modifies the terminal control flags of the provided file handle so the hang-up-on-close
        behavior is disabled. On Windows this function is a no-op.

        Parameters
        ----------
            f (TextIOWrapper): Open file-like handle for the serial device whose terminal attributes
                will be adjusted.

        """
        if sys.platform == "win32":
            return

        import termios  # pylint: disable=C0415,E0401

        attrs = termios.tcgetattr(f)
        attrs[2] = attrs[2] & ~termios.HUPCL
        termios.tcsetattr(f, termios.TCSAFLUSH, attrs)

    def __repr__(self) -> str:
        """
        Return a concise, machine-readable representation of the SerialInterface instance.

        Includes the device path and, when present, the debug output target. Also notes the `noProto` and `noNodes` flags when they are true.

        Returns
        -------
            str: A representation string of the form "SerialInterface(devPath=..., debugOut=...,
                noProto=True, noNodes=True)" with only the applicable fields included.

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
        """
        Close the serial connection and ensure any pending outgoing data is transmitted.

        If a serial stream is present, flushes the stream and waits briefly to allow outstanding
        data to reach the device before closing; then logs the closure and delegates final cleanup
        to the base StreamInterface.close(). This operation may block briefly while flushing.
        """
        if self.stream:  # Stream can be null if we were already closed
            # Flush and sleep to ensure all pending data is transmitted before closing.
            # This workaround ensures the device receives all data before the serial
            # connection is terminated, particularly important for some USB-serial
            # adapters and hardware configurations.
            self.stream.flush()
            time.sleep(0.1)
            self.stream.flush()
            time.sleep(0.1)
        logger.debug("Closing Serial stream")
        StreamInterface.close(self)

    def __enter__(self) -> "SerialInterface":
        """
        Return the SerialInterface instance for use in a with-statement.

        Returns
        -------
            SerialInterface: The same SerialInterface instance.

        """
        return self

    def __exit__(
        self,
        exc_type: Optional[Type[BaseException]],
        exc_val: Optional[BaseException],
        exc_tb: Optional[types.TracebackType],
    ) -> None:
        """
        Handle exit from a context manager and preserve exception handling and logging behavior.

        This method processes any exception information provided by the context manager and lets
        the base implementation perform the actual cleanup and logging.
        """
        return super().__exit__(exc_type, exc_val, exc_tb)
