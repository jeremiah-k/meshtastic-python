"""Stream Interface base class."""

import contextlib
import logging
import threading
import time
from typing import IO, Any, Callable

import serial  # type: ignore[import-untyped]

from meshtastic.mesh_interface import MeshInterface
from meshtastic.protobuf import mesh_pb2
from meshtastic.util import is_windows11, stripnl

START1 = 0x94
START2 = 0xC3
HEADER_LEN = 4
MAX_TO_FROM_RADIO_SIZE = 512

# Stream timing constants
DEVICE_WAKE_DELAY = 0.1  # Delay after wake bytes to allow device startup.
WAKE_BYTE_COUNT = 32  # Number of wake bytes sent to resync the device parser.
STANDARD_WRITE_DELAY = 0.1  # Standard post-write delay.
WINDOWS11_WRITE_DELAY = 1.0  # Extended post-write delay on Windows 11.
READER_THREAD_JOIN_TIMEOUT = 2.0  # Reader thread join timeout during shutdown.
READER_IDLE_BACKOFF_SECONDS = 0.01  # Backoff when read loop receives no bytes.

STREAM_WRITE_EXCEPTIONS = (
    OSError,
    ValueError,
    serial.SerialException,
    serial.SerialTimeoutException,
)
# Write/flush exceptions that indicate stream closure.

logger = logging.getLogger(__name__)


class StreamInterface(MeshInterface):
    """Interface class for meshtastic devices over a stream link (serial, TCP, etc).

    Subclasses that manage their own I/O (e.g., TCPInterface) should set the
    class attribute ``_provides_own_stream = True`` to indicate that
    ``connect()`` should be called even when ``self.stream`` is ``None``.
    """

    class StreamInterfaceError(MeshInterface.MeshInterfaceError):
        """Raised when StreamInterface is instantiated without a concrete stream."""

        DEFAULT_MSG = "StreamInterface is now abstract (to update existing code create SerialInterface instead)"
        CONNECT_WITHOUT_STREAM_MSG = "connect() called without an active stream"

        def __init__(self, message: str = DEFAULT_MSG) -> None:
            """Initialize the StreamInterfaceError with a provided or default message.

            Parameters
            ----------
            message : str
                The error message to use. Defaults to DEFAULT_MSG which
                explains that StreamInterface is abstract and a concrete
                stream-backed subclass (e.g., SerialInterface) should be instantiated.
            """
            super().__init__(message)

    class PayloadTooLargeError(MeshInterface.MeshInterfaceError, ValueError):
        """Raised when a serialized ToRadio payload exceeds MAX_TO_FROM_RADIO_SIZE."""

        def __init__(self, payload_size: int, max_size: int) -> None:
            """Initialize with actual `payload_size` and allowed `max_size`."""
            super().__init__(f"ToRadio payload too large ({payload_size} > {max_size})")

    class StreamClosedError(StreamInterfaceError, ConnectionError):
        """Raised when stream I/O is attempted without an active stream."""

        DEFAULT_MSG = "stream is not available"
        WRITE_NO_PROGRESS_MSG = "stream write returned no bytes"

        def __init__(self, message: str = DEFAULT_MSG) -> None:
            """Initialize with a provided or default stream-closed message."""
            super().__init__(message)

    def __init__(  # pylint: disable=R0917
        self,
        debugOut: IO[str] | Callable[[str], Any] | None = None,
        noProto: bool = False,
        connectNow: bool = True,
        noNodes: bool = False,
        timeout: float = 300.0,
    ) -> None:
        """Initialize the StreamInterface, prepare its reader thread, and optionally open and configure the underlying stream connection.

        Parameters
        ----------
        debugOut : IO[str] | Callable[[str], Any] | None
            If provided, device debug serial output will be written to this stream or callable. (Default value = None)
        noProto : bool
            If True, skip protocol-specific startup and allow using this class without a concrete stream implementation. (Default value = False)
        connectNow : bool
            If True, call connect() after initialization and, unless `noProto` is True, wait for protocol configuration. (Default value = True)
        noNodes : bool
            Passed to the MeshInterface initializer to control node discovery behavior. (Default value = False)
        timeout : float
            Seconds to wait for replies and configuration operations. (Default value = 300.0)

        Raises
        ------
        StreamInterfaceError
            If this class has not been specialized with a concrete
            `self.stream` and `noProto` is False (indicates
            StreamInterface is abstract).
        """

        # Initialize disconnect provenance early so pylint (and callers) see a
        # defined attribute even if initialization aborts before stream setup.
        self._last_disconnect_source = "stream.initialized"

        _provides_own_stream = getattr(self, "_provides_own_stream", False)
        local_stream = getattr(self, "stream", None)
        if local_stream is None and not noProto and not _provides_own_stream:
            raise StreamInterface.StreamInterfaceError()
        self.stream: serial.Serial | None = local_stream
        self._rxBuf = bytearray()
        self._wantExit = False
        # Locking contract:
        # - _connect_lock serializes connect()/close() transitions that mutate
        #   _wantExit, stream lifecycle, and reader-thread start.
        # - Never hold _connect_lock while waiting on thread join or while
        #   performing MeshInterface callback publication; those paths can call
        #   back into higher-level code and must remain lock-free.
        # Serialize reader-thread creation/start across concurrent connect() calls.
        self._connect_lock = threading.Lock()

        self.is_windows11 = is_windows11()
        self.cur_log_line = ""

        # daemon=True so the reader thread does not prevent process exit;
        # callers must call close() explicitly for a clean shutdown.
        self._rxThread = threading.Thread(
            target=self._reader, args=(), daemon=True, name="stream reader"
        )

        super().__init__(
            debugOut=debugOut, noProto=noProto, noNodes=noNodes, timeout=timeout
        )

        # Start the reader thread after superclass constructor completes init
        if connectNow:
            # Use a sentinel attribute to detect if subclass provides its own stream I/O.
            # This is more robust than method identity checks which break with decorators.
            if self.stream is None and not _provides_own_stream:
                if noProto:
                    logger.debug(
                        "No stream configured for %s; deferring connect()",
                        self.__class__.__name__,
                    )
                else:
                    raise StreamInterface.StreamInterfaceError()
            else:
                self.connect()
                if not noProto:
                    # connect() waits only for transport-connected state; constructor
                    # still waits for full config materialization for legacy behavior.
                    self.waitForConfig()

    def connect(self) -> None:
        """Establish the connection to the radio and start the background reader and configuration process.

        Sends wake/resynchronization bytes to the device, starts the reader
        thread, begins protocol configuration, and — if the instance uses the
        protocol — waits for the protocol/database download to complete.
        """

        _provides_own_stream = getattr(self, "_provides_own_stream", False)
        requires_stream = not _provides_own_stream
        # Check if thread has already been started (threads can only be started once)
        # If ident is not None, the thread was started before and needs recreation.
        with self._connect_lock:
            if self.stream is None and requires_stream:
                raise StreamInterface.StreamInterfaceError(
                    StreamInterface.StreamInterfaceError.CONNECT_WITHOUT_STREAM_MSG
                )
            if self._rxThread.is_alive():
                logger.warning(
                    "connect() called while reader thread is still alive; ignoring request"
                )
                return
            # All reconnect side effects happen under the same lock so a concurrent
            # close() intent is not accidentally cleared by an early-return connect().
            self._wantExit = False
            should_wake_stream = requires_stream and self.stream is not None
            if should_wake_stream:
                # Send bogus UART characters to wake sleeping devices and force parser
                # resynchronization before starting a new reader thread.
                p: bytes = bytes([START2] * WAKE_BYTE_COUNT)
                self._write_bytes(p)
                time.sleep(DEVICE_WAKE_DELAY)  # give device time to start running
            if self._rxThread.ident is not None:
                self._rxThread = threading.Thread(
                    target=self._reader, args=(), daemon=True, name="stream reader"
                )
            self._rxThread.start()

        try:
            self._start_config()
            if not self.noProto:  # Wait for the db download if using the protocol
                self._wait_connected()
        except Exception:
            # If protocol startup fails after reader launch, tear down to avoid
            # leaked background threads and partial connection state.
            with contextlib.suppress(Exception):
                self._shared_close()
            with contextlib.suppress(Exception):
                self._join_reader_thread()
            raise

    def _close_stream_safely(self) -> None:
        """Best-effort close and clear of the underlying stream handle."""
        s = self.stream
        if s is not None:
            with contextlib.suppress(OSError, ValueError, serial.SerialException):
                s.close()
            self.stream = None

    def _disconnected(self) -> None:
        """Perform superclass disconnection cleanup, close the underlying stream if present, and clear the stream reference.

        This method calls MeshInterface._disconnected(self), then, if self.stream is not None, closes it and sets self.stream to None.
        """
        super()._disconnected()

        with self._connect_lock:
            logger.debug("Closing our port")
            self._close_stream_safely()

    def _write_bytes(self, b: bytes) -> None:
        """Write bytes to the underlying stream and pause briefly to allow the device to process them.

        When a stream exists and is open, bytes are written and flushed; after
        flushing the method sleeps to allow the device time to handle the data:
        WINDOWS11_WRITE_DELAY seconds on Windows 11, STANDARD_WRITE_DELAY
        seconds otherwise.

        Parameters
        ----------
        b : bytes
            Bytes to write to the stream.

        Raises
        ------
        StreamClosedError
            If no stream is configured or the configured stream is closed.
        """
        s = self.stream
        if s is None or not getattr(s, "is_open", True):
            raise StreamInterface.StreamClosedError()
        payload = memoryview(b)
        bytes_written = 0
        try:
            while bytes_written < len(payload):
                written = s.write(payload[bytes_written:])
                if written is None:
                    raise StreamInterface.StreamClosedError(
                        StreamInterface.StreamClosedError.WRITE_NO_PROGRESS_MSG
                    )
                try:
                    written_count = int(written)
                except (TypeError, ValueError) as exc:
                    raise StreamInterface.StreamClosedError(
                        StreamInterface.StreamClosedError.WRITE_NO_PROGRESS_MSG
                    ) from exc
                if written_count <= 0:
                    raise StreamInterface.StreamClosedError(
                        StreamInterface.StreamClosedError.WRITE_NO_PROGRESS_MSG
                    )
                bytes_written += written_count
            s.flush()
        except STREAM_WRITE_EXCEPTIONS as exc:
            raise StreamInterface.StreamClosedError(
                str(exc) or StreamInterface.StreamClosedError.DEFAULT_MSG
            ) from exc
        self._sleep_after_write()

    def _sleep_after_write(self) -> None:
        """Pause after writes so device-side parsers can process framed input."""
        # Win11 sometimes needs additional settling time after writes.
        delay = WINDOWS11_WRITE_DELAY if self.is_windows11 else STANDARD_WRITE_DELAY
        time.sleep(delay)

    def _read_bytes(self, length: int) -> bytes:
        """Read up to the specified number of bytes from the configured underlying stream.

        Parameters
        ----------
        length : int
            Maximum number of bytes to read.

        Returns
        -------
        bytes
            Up to `length` bytes read from the stream.

        Raises
        ------
        StreamClosedError
            If no stream is configured or the configured stream is closed.
        """
        s = self.stream
        # Default is_open to True for stream types that don't expose this attribute,
        # treating them as open for backward compatibility (e.g., mock streams, test doubles).
        if s is None or not getattr(s, "is_open", True):
            raise StreamInterface.StreamClosedError()
        return s.read(length)  # type: ignore[no-any-return]

    def _send_to_radio_impl(self, toRadio: mesh_pb2.ToRadio) -> None:
        """Frame and send a ToRadio protobuf to the underlying stream.

        The message is serialized and prefixed with START1, START2 and a two-byte big-endian payload length before being written to the stream.

        Parameters
        ----------
        toRadio : mesh_pb2.ToRadio
            The protobuf message to transmit.
        """
        logger.debug("Sending: %s", stripnl(toRadio))
        b: bytes = toRadio.SerializeToString()
        buf_len: int = len(b)
        if buf_len > MAX_TO_FROM_RADIO_SIZE:
            raise StreamInterface.PayloadTooLargeError(
                payload_size=buf_len,
                max_size=MAX_TO_FROM_RADIO_SIZE,
            )
        # We convert into a string, because the TCP code doesn't work with byte arrays
        header: bytes = bytes([START1, START2, (buf_len >> 8) & 0xFF, buf_len & 0xFF])
        logger.debug("sending header:%r b:%r", header, b)
        self._write_bytes(header + b)

    def close(self) -> None:
        """Shut down the stream connection and request the background reader thread to exit.

        Sets the internal shutdown flag to request reader termination, attempts to join the reader thread
        for up to 2 seconds (skipping join if the thread was never started), and logs a warning if the
        reader remains alive after the timeout.
        """
        logger.debug("Closing stream")
        self._shared_close()
        self._join_reader_thread()

    def _shared_close(self) -> None:
        """Run close() cleanup shared by stream-like subclasses.

        Sets the shutdown intent before delegating to MeshInterface.close() so
        background readers don't treat intentional close as an unexpected
        disconnect. Calls MeshInterface.close() while the stream is still open
        so best-effort disconnect frames can be transmitted, then closes the
        underlying stream in a finally block to unblock pending reads and ensure
        resources are released even when no reader thread was started (for
        example, connectNow=False tests).

        All shutdown state transitions are synchronized with connect() via
        _connect_lock to prevent a concurrent connect() from clearing the
        shutdown intent and restarting the reader against a closed stream.
        """
        with self._connect_lock:
            self._wantExit = True
        try:
            super().close()
        finally:
            with self._connect_lock:
                self._close_stream_safely()

    def _join_reader_thread(self) -> None:
        """Join the reader thread when it is alive and not the current thread."""
        # pyserial cancel_read doesn't seem to work, therefore we ask the
        # reader thread to close things for us
        # close() can be called before connect() starts the reader thread
        # (e.g., tests using connectNow=False). In that case join() would raise.
        # Also handle partially initialized objects from early __init__ returns.
        rx_thread = getattr(self, "_rxThread", None)
        if (
            rx_thread is not None
            and rx_thread is not threading.current_thread()
            and rx_thread.is_alive()
        ):
            rx_thread.join(timeout=READER_THREAD_JOIN_TIMEOUT)
            if rx_thread.is_alive():
                logger.warning("Reader thread did not exit within shutdown timeout")

    def _handle_log_byte(self, b: bytes) -> None:
        r"""Accumulate device log bytes into the current log line and dispatch completed lines.

        Decodes the single-byte input as UTF-8, using '?' for undecodable bytes. Ignores
        carriage return ('\r'); on newline ('\n') calls self._handle_log_line with the
        accumulated line and clears the accumulator; otherwise appends the decoded
        character to the accumulator.

        Parameters
        ----------
        b : bytes
            A single-byte bytes object from the device log stream.
        """

        utf = "?"  # assume we might fail
        try:
            utf = b.decode("utf-8")
        except UnicodeDecodeError:
            logger.debug("Non-UTF8 log byte encountered: %r", b)

        if utf == "\r":
            return
        if utf == "\n":
            self._handle_log_line(self.cur_log_line)
            self.cur_log_line = ""
            return
        self.cur_log_line += utf

    def _reader(self) -> None:
        """Background reader loop that reads from the configured stream and dispatches device log bytes and framed radio messages.

        Continuously reads incoming bytes, forwarding non-protocol bytes to
        _handle_log_byte and delivering complete protocol frames to _handle_from_radio.
        On exit records the disconnect source in _last_disconnect_source, logs the
        shutdown, and calls _disconnected() to perform cleanup.
        """
        logger.debug("in _reader()")
        disconnect_source = "stream.reader_exit"

        try:
            while not self._wantExit:
                # logger.debug("reading character")
                b = self._read_bytes(1)
                # logger.debug("In reader loop")
                # logger.debug(f"read returned {b}")
                if b:
                    c: int = b[0]
                    # logger.debug(f'c:{c}')
                    ptr: int = len(self._rxBuf)

                    self._rxBuf.append(c)

                    if ptr == 0:  # looking for START1
                        if c != START1:
                            self._rxBuf.clear()  # failed to find start
                            # This must be a log message from the device

                            self._handle_log_byte(b)

                    elif ptr == 1:  # looking for START2
                        if c != START2:
                            # Preserve repeated START1 to resync on
                            # START1, START1, START2.
                            if c == START1:
                                self._rxBuf[:] = bytes([START1])
                            else:
                                self._rxBuf.clear()  # failed to find start2
                    elif ptr >= HEADER_LEN - 1:  # we've at least got a header
                        # logger.debug('at least we received a header')
                        # big endian length follows header
                        packetlen = (self._rxBuf[2] << 8) + self._rxBuf[3]

                        if (
                            ptr == HEADER_LEN - 1
                        ):  # we _just_ finished reading the header, validate length
                            if packetlen == 0 or packetlen > MAX_TO_FROM_RADIO_SIZE:
                                self._rxBuf.clear()  # malformed length, restart
                                continue

                        if self._rxBuf and ptr + 1 >= packetlen + HEADER_LEN:
                            try:
                                self._handle_from_radio(
                                    bytes(
                                        self._rxBuf[HEADER_LEN : packetlen + HEADER_LEN]
                                    )
                                )
                            except Exception:
                                logger.exception(
                                    "Error while handling message from radio"
                                )
                            self._rxBuf.clear()
                else:
                    # Avoid tight busy-spin when read() returns no bytes.
                    time.sleep(READER_IDLE_BACKOFF_SECONDS)
        except serial.SerialException as ex:
            if (
                not self._wantExit
            ):  # We might intentionally get an exception during shutdown
                disconnect_source = "stream.serial_exception"
                logger.warning(
                    "Meshtastic serial port disconnected, disconnecting... %s", ex
                )
            else:
                disconnect_source = "stream.close_requested"
        except StreamInterface.StreamClosedError as ex:
            if self._wantExit:
                disconnect_source = "stream.close_requested"
                logger.debug("Stream closed during shutdown: %s", ex)
            else:
                disconnect_source = "stream.closed"
                logger.warning("Stream closed unexpectedly: %s", ex)
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
