"""TCPInterface class for communicating with Meshtastic devices over TCP connections.

This module provides the TCPInterface class which handles communication with
Meshtastic devices via TCP/IP network connections.
"""

# pylint: disable=R0917
import contextlib
import logging
import socket
import threading
import time
from typing import IO, Any, Callable

from meshtastic.stream_interface import StreamInterface

DEFAULT_TCP_PORT = 4403
logger = logging.getLogger(__name__)


class TCPInterface(StreamInterface):
    """Interface class for meshtastic devices over a TCP link."""

    DEFAULT_CONNECT_TIMEOUT = 10.0
    CONNECT_TIMEOUT_ERROR = "connectTimeout must be a positive number, got {!r}"
    SOCKET_NOT_CONNECTED_ERROR = "TCP socket is closed or not connected"
    DEFAULT_MAX_RECONNECT_ATTEMPTS = 8
    DEFAULT_RECONNECT_BACKOFF = 1.6
    DEFAULT_RECONNECT_BASE_DELAY = 1.0
    DEFAULT_RECONNECT_MAX_DELAY = 30.0
    DEFAULT_RECONNECT_SLEEP_SLICE = 0.25

    def __init__(
        self,
        hostname: str,
        debugOut: IO[str] | Callable[[str], Any] | None = None,
        noProto: bool = False,
        connectNow: bool = True,
        portNumber: int = DEFAULT_TCP_PORT,
        noNodes: bool = False,
        timeout: float = 300.0,
        connectTimeout: float | None = DEFAULT_CONNECT_TIMEOUT,
    ) -> None:
        """Initialize a TCPInterface for a meshtastic device and optionally establish a TCP connection.

        Parameters
        ----------
        hostname : str
            Hostname or IP address of the device to connect to.
        debugOut : IO[str] | Callable[[str], Any] | None
            Optional debug output stream or callable; passed to the base class. (Default value = None)
        noProto : bool
            If True, disable protocol handling. (Default value = False)
        connectNow : bool
            If True, attempt to open the TCP connection during initialization. (Default value = True)
        portNumber : int
            TCP port to connect to (default: DEFAULT_TCP_PORT).
        noNodes : bool
            If True, do not populate node state. (Default value = False)
        timeout : float
            Request/response timeout in seconds (default: 300.0).
        connectTimeout : float | None
            Timeout in seconds for socket connect attempts (default: 10.0).
            ``None`` omits the timeout parameter, allowing the platform socket
            default to apply.
        """
        if connectTimeout is not None and connectTimeout <= 0:
            raise ValueError(self.CONNECT_TIMEOUT_ERROR.format(connectTimeout))

        self.stream = None
        self._provides_own_stream = True

        self.hostname: str = hostname
        self.portNumber: int = portNumber
        self._connect_now: bool = connectNow
        self._connect_timeout: float | None = connectTimeout
        # Pre-assign base-class attributes so __repr__ stays safe even if
        # myConnect() raises before StreamInterface.__init__ runs.
        self.debugOut = debugOut
        self.noProto = noProto
        self.noNodes = noNodes

        self.socket: socket.socket | None = None
        self._reconnect_attempts = 0
        self._max_reconnect_attempts = self.DEFAULT_MAX_RECONNECT_ATTEMPTS
        self._reconnect_backoff = self.DEFAULT_RECONNECT_BACKOFF
        self._reconnect_base_delay = self.DEFAULT_RECONNECT_BASE_DELAY
        self._reconnect_max_delay = self.DEFAULT_RECONNECT_MAX_DELAY
        self._reconnect_sleep_slice = self.DEFAULT_RECONNECT_SLEEP_SLICE
        self._reconnect_lock = threading.Lock()
        self._fatal_disconnect = False

        if connectNow:
            self.myConnect()

        try:
            super().__init__(
                debugOut=debugOut,
                noProto=noProto,
                connectNow=connectNow,
                noNodes=noNodes,
                timeout=timeout,
            )
        except Exception:
            # myConnect() runs before base init so ensure we don't leak socket
            # resources if StreamInterface setup fails.
            sock = self.socket
            if sock is not None:
                with contextlib.suppress(Exception):
                    self._socket_shutdown(sock)
                with contextlib.suppress(Exception):
                    sock.close()
                self.socket = None
            raise

    def __repr__(self) -> str:
        """Return a concise string representation of the TCPInterface instance including hostname and relevant flags.

        Returns
        -------
        str
            A representation showing the hostname and any active options:
            `debugOut`, `noProto`, `connectNow=False` (when configured), socket
            state, a non-default `portNumber`, and `noNodes`.
        """
        rep = f"TCPInterface({self.hostname!r}"
        if self.debugOut is not None:
            rep += f", debugOut={self.debugOut!r}"
        if self.noProto:
            rep += ", noProto=True"
        if not self._connect_now:
            rep += ", connectNow=False"
        if self.socket is None:
            rep += ", socket=None"
        if self.portNumber != DEFAULT_TCP_PORT:
            rep += f", portNumber={self.portNumber!r}"
        if self.noNodes:
            rep += ", noNodes=True"
        rep += ")"
        return rep

    def _socket_shutdown(self, sock: socket.socket | None = None) -> None:
        """Initiate a bidirectional shutdown of the specified socket if one exists.

        When ``sock`` is omitted, operates on ``self.socket``.
        """
        sock_to_shutdown = self.socket if sock is None else sock
        if sock_to_shutdown is not None:
            sock_to_shutdown.shutdown(socket.SHUT_RDWR)

    def _close_socket_if_current(self, sock: socket.socket | None) -> bool:
        """Best-effort socket teardown and conditional state clear.

        Returns
        -------
        bool
            `True` if this call cleared `self.socket` (because it still matched
            `sock`), otherwise `False`.
        """
        if sock is None:
            return False
        with contextlib.suppress(Exception):
            self._socket_shutdown(sock)
        with contextlib.suppress(Exception):
            sock.close()
        if self.socket is sock:
            self.socket = None
            return True
        return False

    def myConnect(self) -> None:
        """Establish a TCP connection to the instance hostname and port.

        Stores the resulting connected socket on self.socket.
        """
        logger.debug("Connecting to %s", self.hostname)
        server_address = (self.hostname, self.portNumber)
        if self._connect_timeout is None:
            connected_socket = socket.create_connection(server_address)
        else:
            connected_socket = socket.create_connection(
                server_address, timeout=self._connect_timeout
            )
        connected_socket.settimeout(None)
        self.socket = connected_socket
        self._fatal_disconnect = False

    @staticmethod
    def _notify_pending_sender_failure(pending_entry: Any, reason: str) -> bool:
        """Best-effort notify a pending sender entry about reconnect queue drop.

        Supports common waiter/future-like interfaces when present. Returns
        True when any notifier was invoked.
        """
        # Future-like objects.
        set_exception = getattr(pending_entry, "set_exception", None)
        if callable(set_exception):
            set_exception(ConnectionError(reason))
            return True

        # Custom waiter patterns.
        set_failed = getattr(pending_entry, "set_failed", None)
        if callable(set_failed):
            set_failed(ConnectionError(reason))
            return True

        signal_set = getattr(pending_entry, "set", None)
        if callable(signal_set):
            signal_set()
            return True
        return False

    def close(self) -> None:
        """Close the TCP connection and stop the reader thread.

        Requests reader shutdown, calls the base-class close logic, and tears down the
        underlying socket (ignoring shutdown/close errors). After socket teardown,
        attempts to join the reader thread for up to 2.0 seconds and logs a
        warning if the thread does not exit in time.
        """
        logger.debug("Closing TCP stream")
        # Request shutdown using StreamInterface shared cleanup and then perform
        # TCP-specific socket teardown before joining the reader thread.
        self._shared_close()
        # Sometimes the socket read might be blocked in the reader thread.
        # Therefore we force the shutdown by closing the socket here
        self._close_socket_if_current(self.socket)

        # Join after socket teardown so a blocking recv() can exit promptly.
        self._join_reader_thread()

    def connect(self) -> None:
        """Ensure socket availability, then run shared StreamInterface startup."""
        if self.socket is None and not self._wantExit and not self._fatal_disconnect:
            self.myConnect()
        super().connect()

    def _write_bytes(self, b: bytes) -> None:
        """Send the full byte sequence over the TCP socket.

        Attempts to transmit all bytes; if an OSError occurs, logs a warning, shuts
        down and closes the socket, and clears the stored socket reference.

        Parameters
        ----------
        b : bytes
            Bytes to send.

        Raises
        ------
        ConnectionError
            If the TCP socket is missing or disconnected.
        OSError
            If the underlying socket write fails.
        """
        sock = self.socket
        if sock is None:
            raise ConnectionError(self.SOCKET_NOT_CONNECTED_ERROR)
        try:
            # sendall() guarantees full payload transmission or raises.
            sock.sendall(b)
        except OSError as ex:
            logger.warning(
                "TCP write failed (%d bytes), resetting socket: %s", len(b), ex
            )
            if self._close_socket_if_current(sock):
                logger.debug(
                    "Reconnect deferred to reader/reconnect path for %s",
                    self.hostname,
                )
            raise

    def _compute_reconnect_delay(self) -> float:
        """Compute exponential reconnect backoff delay in seconds."""
        exponent = max(0, self._reconnect_attempts - 1)
        delay = self._reconnect_base_delay * (self._reconnect_backoff**exponent)
        return min(self._reconnect_max_delay, delay)

    def _sleep_reconnect_delay(self, delay: float) -> bool:
        """Sleep reconnect delay with frequent shutdown checks.

        Returns
        -------
        bool
            `True` if the full delay elapsed, `False` if interrupted by shutdown.
        """
        deadline = time.monotonic() + delay
        while True:
            if self._wantExit or self._fatal_disconnect:
                return False
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return True
            time.sleep(min(self._reconnect_sleep_slice, remaining))

    def _on_fatal_disconnect(self, reason: str) -> None:
        """Mark the interface as fatally disconnected and stop reconnect attempts."""
        if self._fatal_disconnect:
            return
        self._fatal_disconnect = True
        self._wantExit = True
        logger.error(
            "Stopping TCP reconnect for %s after %d attempts: %s",
            self.hostname,
            self._reconnect_attempts,
            reason,
        )

    def _attempt_reconnect(self) -> bool:
        """Attempt to re-establish the TCP socket and rerun protocol startup.

        Returns
        -------
        bool
            `True` if reconnect and post-connect startup succeeded, `False` otherwise.
        """
        if not self._reconnect_lock.acquire(  # pylint: disable=consider-using-with
            blocking=False
        ):
            logger.debug("Reconnect already in progress for %s", self.hostname)
            return False
        try:
            if (
                self._wantExit
                or self._fatal_disconnect
                or self._reconnect_attempts >= self._max_reconnect_attempts
            ):
                if self._reconnect_attempts >= self._max_reconnect_attempts:
                    self._on_fatal_disconnect("reconnect retry limit reached")
                return False

            self._reconnect_attempts += 1
            delay = (
                0.0
                if self._reconnect_attempts == 1
                else self._compute_reconnect_delay()
            )
            logger.debug(
                "Reconnect attempt %d/%d for %s in %.1fs",
                self._reconnect_attempts,
                self._max_reconnect_attempts,
                self.hostname,
                delay,
            )
            if delay > 0.0 and not self._sleep_reconnect_delay(delay):
                return False
            reconnect_ok = False
            connect_failed = False
            try:
                self.myConnect()
            except OSError as connect_ex:
                logger.warning("Reconnect to %s failed: %s", self.hostname, connect_ex)
                connect_failed = True

            if not connect_failed:
                if self.socket is None:
                    logger.warning(
                        "myConnect() returned without setting socket for %s",
                        self.hostname,
                    )
                elif self._wantExit:
                    # close() may race while we reconnect; tear down the new socket.
                    reconnect_sock = self.socket
                    self._close_socket_if_current(reconnect_sock)
                else:
                    # _start_config() can call _send_to_radio(), which drains self.queue and may
                    # block on queue space. During reader-thread reconnect this can deadlock
                    # because queue updates are also processed by the reader thread.
                    if threading.current_thread() is getattr(self, "_rxThread", None):
                        dropped = 0
                        notified = 0
                        drop_reason = (
                            f"Queued send dropped during reconnect for {self.hostname}"
                        )
                        pending_entries: list[Any] = []
                        with self._queue_lock:
                            pending_queue = getattr(self, "queue", None)
                            if isinstance(pending_queue, dict) and pending_queue:
                                dropped = len(pending_queue)
                                pending_entries = list(pending_queue.values())
                                pending_queue.clear()
                        for pending_entry in pending_entries:
                            with contextlib.suppress(Exception):
                                if self._notify_pending_sender_failure(
                                    pending_entry, drop_reason
                                ):
                                    notified += 1
                        if dropped > 0:
                            logger.warning(
                                "Dropped %d queued packet(s) before reconnect config on %s (notified=%d)",
                                dropped,
                                self.hostname,
                                notified,
                            )

                    try:
                        self._start_config()
                    # Keep reader thread alive on unexpected post-reconnect errors.
                    except Exception as config_ex:  # noqa: BLE001
                        logger.warning(
                            "Post-reconnect config for %s failed: %s",
                            self.hostname,
                            config_ex,
                        )
                        reconnect_sock = self.socket
                        self._close_socket_if_current(reconnect_sock)
                    else:
                        self._reconnect_attempts = 0
                        self._fatal_disconnect = False
                        reconnect_ok = True

            return reconnect_ok
        finally:
            self._reconnect_lock.release()

    # pylint: disable=too-many-return-statements
    # Multiple early returns are intentional here for clear handling of each
    # exit condition: no socket, shutdown requested (checked twice during
    # reconnect wait), reconnect failure, socket not set after reconnect,
    # shutdown during reconnect, successful reconnect, and normal data return.
    def _read_bytes(self, length: int) -> bytes:
        """Read up to `length` bytes from the TCP socket, handling dead connections and automatic reconnection.

        If a socket is present and data is available, returns the received bytes. If the
        socket is detected as disconnected, the method initiates a reconnect sequence and
        returns `b""`. If no socket is available, the method attempts reconnect unless a
        shutdown is already requested.

        Parameters
        ----------
        length : int
            Maximum number of bytes to read.

        Returns
        -------
        bytes
            The received bytes, or `b""` if no data was returned
            because the socket is absent, a reconnect was started, or shutdown was requested.
        """
        sock = self.socket
        if sock is not None:
            try:
                data = sock.recv(length)
            except OSError as ex:
                logger.debug("Socket read error, treating as dead socket: %s", ex)
                data = b""
            # empty byte indicates a disconnected socket,
            # we need to handle it to avoid an infinite loop reading from null socket
            if data == b"":
                logger.debug("dead socket, re-connecting")
                # cleanup and reconnect socket without breaking reader thread
                if self._close_socket_if_current(sock):
                    self._attempt_reconnect()
                else:
                    logger.debug(
                        "Socket changed during read cleanup, skipping reconnect"
                    )
                return b""
            with self._reconnect_lock:
                self._reconnect_attempts = 0
                self._fatal_disconnect = False
            return data

        # Socket may be briefly nulled by a concurrent writer failure; try to
        # recover unless shutdown was explicitly requested.
        if not self._wantExit and not self._fatal_disconnect:
            logger.debug("Socket unavailable, attempting reconnect")
            self._attempt_reconnect()
        return b""
