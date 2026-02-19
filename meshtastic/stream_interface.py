"""Stream Interface base class."""

import io
import logging
import threading
import time
from typing import Optional, cast

import serial  # type: ignore[import-untyped]

from meshtastic.mesh_interface import MeshInterface
from meshtastic.protobuf import mesh_pb2
from meshtastic.util import is_windows11, stripnl

START1 = 0x94
START2 = 0xC3
HEADER_LEN = 4
MAX_TO_FROM_RADIO_SIZE = 512
logger = logging.getLogger(__name__)


class StreamInterface(MeshInterface):
    """Interface class for meshtastic devices over a stream link (serial, TCP, etc)."""

    class StreamInterfaceError(MeshInterface.MeshInterfaceError):
        """Raised when StreamInterface is instantiated without a concrete stream."""

        DEFAULT_MSG: str = "StreamInterface is now abstract (to update existing code create SerialInterface instead)"

        def __init__(self, message: str = DEFAULT_MSG) -> None:
            super().__init__(message)

    def __init__(  # pylint: disable=R0917
        self,
        debugOut: Optional[io.TextIOWrapper] = None,
        noProto: bool = False,
        connectNow: bool = True,
        noNodes: bool = False,
        timeout: float = 300.0,
    ) -> None:
        """
        Initialize the StreamInterface, prepare its reader thread, and optionally
        open and configure the underlying stream connection.

        Parameters
        ----------
            debugOut (Optional[io.TextIOWrapper]): If provided, device debug serial output will be written to this stream.
            noProto (bool): If True, skip protocol-specific startup and allow using this class without a concrete stream implementation.
            connectNow (bool): If True, call connect() after initialization and, unless `noProto` is True, wait for protocol configuration.
            noNodes (bool): Passed to the MeshInterface initializer to control node discovery behavior.
            timeout (float): Seconds to wait for replies and configuration operations.

        Raises
        ------
            StreamInterfaceError: If this class has not been specialized with a concrete
                `self.stream` and `noProto` is False (indicates
                StreamInterface is abstract).

        """

        # Initialize disconnect provenance early so pylint (and callers) see a
        # defined attribute even if initialization aborts before stream setup.
        self._last_disconnect_source = "stream.initialized"

        if not hasattr(self, "stream") and not noProto:
            raise StreamInterface.StreamInterfaceError()
        self.stream: Optional[serial.Serial] = cast(
            Optional[serial.Serial],
            getattr(self, "stream", None),
        )  # only serial uses this, TCPInterface overrides the relevant methods instead
        self._rxBuf = bytes()  # empty
        self._wantExit = False

        self.is_windows11 = is_windows11()
        self.cur_log_line = ""

        # daemon=True so the reader thread does not prevent process exit;
        # callers must call close() explicitly for a clean shutdown.
        self._rxThread = threading.Thread(
            target=self.__reader, args=(), daemon=True, name="stream reader"
        )

        MeshInterface.__init__(
            self, debugOut=debugOut, noProto=noProto, noNodes=noNodes, timeout=timeout
        )

        # Start the reader thread after superclass constructor completes init
        if connectNow:
            # Use a sentinel attribute to detect if subclass provides its own stream I/O.
            # This is more robust than method identity checks which break with decorators.
            _provides_own_stream = getattr(self, "_provides_own_stream", False)
            if self.stream is None and not _provides_own_stream:
                logger.debug(
                    "No stream configured for %s; deferring connect()",
                    self.__class__.__name__,
                )
            else:
                self.connect()
                if not noProto:
                    self.waitForConfig()

    def connect(self) -> None:
        """
        Establish the connection to the radio and start the background
        reader and configuration process.

        Sends wake/resynchronization bytes to the device, starts the reader
        thread, begins protocol configuration, and — if the instance uses the
        protocol — waits for the protocol/database download to complete.
        """

        # Send some bogus UART characters to force a sleeping device to wake, and
        # if the reading statemachine was parsing a bad packet make sure
        # we write enough start bytes to force it to resync (we don't use START1
        # because we want to ensure it is looking for START1)
        p: bytes = bytes([START2] * 32)
        self._writeBytes(p)
        time.sleep(0.1)  # wait 100ms to give device time to start running

        self._rxThread.start()

        self._startConfig()

        if not self.noProto:  # Wait for the db download if using the protocol
            self._waitConnected()

    def _disconnected(self) -> None:
        """
        Clean up after a disconnection and close the underlying stream if present.

        Calls the superclass disconnect handler, then closes self.stream and sets it to None if a stream exists.
        """
        MeshInterface._disconnected(self)

        logger.debug("Closing our port")
        # pylint: disable=E0203
        if self.stream is not None:
            # pylint: disable=E0203
            self.stream.close()
            # pylint: disable=W0201
            self.stream = None

    def _writeBytes(self, b: bytes) -> None:
        """
        Write bytes to the underlying stream and ensure the device has time to
        process them.

        If the stream is closed this call is ignored. The bytes are written and
        flushed; after flushing the method sleeps to give the device time to
        handle the data (1.0 second on Windows 11, 0.1 second otherwise).

        Parameters
        ----------
                b (bytes): Data to write to the stream.

        """
        if self.stream:  # ignore writes when stream is closed
            self.stream.write(b)
            self.stream.flush()
            # win11 might need a bit more time, too
            if self.is_windows11:
                time.sleep(1.0)
            else:
                # we sleep here to give the TBeam a chance to work
                time.sleep(0.1)

    def _readBytes(self, length: int) -> Optional[bytes]:
        """
        Read up to the specified number of bytes from the configured underlying stream.

        Returns:
            `bytes` containing up to `length` bytes read from the stream, or `None` if no stream is configured.

        """
        if self.stream:
            return self.stream.read(length)
        else:
            return None

    def _sendToRadioImpl(self, toRadio: mesh_pb2.ToRadio) -> None:
        """
        Serialize a ToRadio protobuf, frame it with the protocol header, and send the framed payload to the underlying stream.

        The header is START1, START2, then a two-byte big-endian payload length followed by the serialized message.

        Parameters
        ----------
            toRadio (mesh_pb2.ToRadio): The protobuf message to serialize and transmit.

        """
        logger.debug("Sending: %s", stripnl(toRadio))
        b: bytes = toRadio.SerializeToString()
        bufLen: int = len(b)
        # We convert into a string, because the TCP code doesn't work with byte arrays
        header: bytes = bytes([START1, START2, (bufLen >> 8) & 0xFF, bufLen & 0xFF])
        logger.debug("sending header:%r b:%r", header, b)
        self._writeBytes(header + b)

    def close(self) -> None:
        """
        Shut down the stream connection and request the background reader thread to exit.

        Sets the internal shutdown flag to request reader termination, attempts to join the reader thread
        for up to 2 seconds (skipping join if the thread was never started), and logs a warning if the
        reader remains alive after the timeout.
        """
        logger.debug("Closing stream")
        # Set _wantExit before calling MeshInterface.close() to prevent the
        # reader thread from misinterpreting intentional shutdown as a spurious disconnect
        self._wantExit = True
        MeshInterface.close(self)
        # pyserial cancel_read doesn't seem to work, therefore we ask the
        # reader thread to close things for us
        # close() can be called before connect() starts the reader thread
        # (e.g., tests using connectNow=False). In that case join() would raise.
        # Also handle partially initialized objects from early __init__ returns.
        rx_thread = getattr(self, "_rxThread", None)
        if (
            rx_thread is not None
            and rx_thread != threading.current_thread()
            and rx_thread.is_alive()
        ):
            rx_thread.join(timeout=2.0)
            if rx_thread.is_alive():
                logger.warning("Reader thread did not exit within shutdown timeout")

    def _handle_log_byte(self, b: bytes) -> None:
        r"""
        Process a single byte from the device log stream, accumulate characters into the current log line, and dispatch complete lines.

        Undecodable bytes are replaced with '?'. Carriage return ('\r') bytes are ignored; newline ('\n')
        completes the current line and dispatches it via self._handleLogLine, after which the accumulator is cleared.

        Parameters
        ----------
            b (bytes): Single-byte bytes object from the device.

        """

        utf = "?"  # assume we might fail
        try:
            utf = b.decode("utf-8")
        except UnicodeDecodeError:
            pass

        if utf == "\r":
            pass  # ignore
        elif utf == "\n":
            self._handleLogLine(self.cur_log_line)
            self.cur_log_line = ""
        else:
            self.cur_log_line += utf

    def __reader(self) -> None:
        """
        Background reader loop that reads from the configured stream and dispatches device log bytes and framed radio messages.

        Runs in a daemon background thread, reading incoming bytes and forwarding non-protocol bytes
        to _handle_log_byte. Recognizes protocol frames prefixed by START1/START2 with a two-byte length
        header and delivers complete payloads to _handleFromRadio. On termination updates _last_disconnect_source,
        logs the exit, and invokes _disconnected() to perform cleanup.
        """
        logger.debug("in __reader()")
        empty = bytes()
        disconnect_source = "stream.reader_exit"

        try:
            while not self._wantExit:
                # logger.debug("reading character")
                b: Optional[bytes] = self._readBytes(1)
                # logger.debug("In reader loop")
                # logger.debug(f"read returned {b}")
                if b is not None and len(b) > 0:
                    c: int = b[0]
                    # logger.debug(f'c:{c}')
                    ptr: int = len(self._rxBuf)

                    # Assume we want to append this byte, fixme use bytearray instead
                    self._rxBuf = self._rxBuf + b

                    if ptr == 0:  # looking for START1
                        if c != START1:
                            self._rxBuf = empty  # failed to find start
                            # This must be a log message from the device

                            self._handle_log_byte(b)

                    elif ptr == 1:  # looking for START2
                        if c != START2:
                            self._rxBuf = empty  # failed to find start2
                    elif ptr >= HEADER_LEN - 1:  # we've at least got a header
                        # logger.debug('at least we received a header')
                        # big endian length follows header
                        packetlen = (self._rxBuf[2] << 8) + self._rxBuf[3]

                        if (
                            ptr == HEADER_LEN - 1
                        ):  # we _just_ finished reading the header, validate length
                            if packetlen > MAX_TO_FROM_RADIO_SIZE:
                                self._rxBuf = (
                                    empty  # length was out out bounds, restart
                                )

                        if len(self._rxBuf) != 0 and ptr + 1 >= packetlen + HEADER_LEN:
                            try:
                                self._handleFromRadio(self._rxBuf[HEADER_LEN:])
                            except Exception:
                                logger.exception(
                                    "Error while handling message from radio"
                                )
                            self._rxBuf = empty
                else:
                    # logger.debug(f"timeout")
                    pass
        except serial.SerialException as ex:
            if (
                not self._wantExit
            ):  # We might intentionally get an exception during shutdown
                disconnect_source = "stream.serial_exception"
                logger.warning(
                    f"Meshtastic serial port disconnected, disconnecting... {ex}"
                )
            else:
                disconnect_source = "stream.close_requested"
        except OSError:
            if (
                not self._wantExit
            ):  # We might intentionally get an exception during shutdown
                disconnect_source = "stream.os_error"
                logger.exception("Unexpected OSError, terminating meshtastic reader...")
            else:
                disconnect_source = "stream.close_requested"
        except Exception:
            disconnect_source = "stream.exception"
            logger.exception("Unexpected exception, terminating meshtastic reader...")
        finally:
            if self._wantExit and disconnect_source == "stream.reader_exit":
                disconnect_source = "stream.close_requested"
            self._last_disconnect_source = disconnect_source
            logger.debug("reader is exiting")
            self._disconnected()
