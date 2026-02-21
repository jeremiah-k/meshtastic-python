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

from meshtastic.mesh_interface import MeshInterface
from meshtastic.stream_interface import StreamInterface

DEFAULT_TCP_PORT = 4403
logger = logging.getLogger(__name__)


class TCPInterface(StreamInterface):
    """Interface class for meshtastic devices over a TCP link."""

    def __init__(
        self,
        hostname: str,
        debugOut=None,
        noProto: bool = False,
        connectNow: bool = True,
        portNumber: int = DEFAULT_TCP_PORT,
        noNodes: bool = False,
        timeout: float = 300.0,
    ):
        """Initialize a TCPInterface for a meshtastic device and optionally establish a TCP connection.

        Parameters
        ----------
        hostname : str
            Hostname or IP address of the device to connect to.
        debugOut : TextIOWrapper | None
            Optional debug output/stream; passed to the base class. (Default value = None)
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
        """

        self.stream = None
        self._provides_own_stream = True

        self.hostname: str = hostname
        self.portNumber: int = portNumber

        self.socket: socket.socket | None = None

        if connectNow:
            self.myConnect()
        else:
            self.socket = None

        super().__init__(
            debugOut=debugOut,
            noProto=noProto,
            connectNow=connectNow,
            noNodes=noNodes,
            timeout=timeout,
        )

    def __repr__(self) -> str:
        """Return a concise string representation of the TCPInterface instance including hostname and relevant flags.

        Returns
        -------
        str
            A representation showing the hostname and any active options:
            `debugOut`, `noProto`, `connectNow=False` (when not connected), a
            non-default `portNumber`, and `noNodes`.
        """
        rep = f"TCPInterface({self.hostname!r}"
        if self.debugOut is not None:
            rep += f", debugOut={self.debugOut!r}"
        if self.noProto:
            rep += ", noProto=True"
        if self.socket is None:
            rep += ", connectNow=False"
        if self.portNumber != DEFAULT_TCP_PORT:
            rep += f", portNumber={self.portNumber!r}"
        if self.noNodes:
            rep += ", noNodes=True"
        rep += ")"
        return rep

    def _socket_shutdown(self) -> None:
        """Initiates a bidirectional shutdown of the underlying socket if one exists.

        Does nothing when no socket is present.
        """
        if self.socket is not None:
            self.socket.shutdown(socket.SHUT_RDWR)

    def myConnect(self) -> None:
        """Establishes a TCP connection to the instance hostname and port.

        Stores the resulting connected socket on self.socket.
        """
        logger.debug("Connecting to %s", self.hostname)
        server_address = (self.hostname, self.portNumber)
        self.socket = socket.create_connection(server_address)

    def close(self) -> None:
        """Close the TCP connection and stop the reader thread.

        Requests reader shutdown, calls the base-class close logic, and tears down the
        underlying socket (ignoring shutdown/close errors). After socket teardown,
        attempts to join the reader thread for up to 2.0 seconds and logs a
        warning if the thread does not exit in time.
        """
        logger.debug("Closing TCP stream")
        # Request reader shutdown before parent close() to prevent reconnect-on-close behavior.
        self._wantExit = True
        MeshInterface.close(self)
        # Sometimes the socket read might be blocked in the reader thread.
        # Therefore we force the shutdown by closing the socket here
        if self.socket is not None:
            with contextlib.suppress(
                Exception
            ):  # Ignore errors in shutdown, because we might have a race with the server
                self._socket_shutdown()
            with contextlib.suppress(
                Exception
            ):  # Ignore close races if the socket is already torn down
                self.socket.close()

        self.socket = None

        # Join after socket teardown so a blocking recv() can exit promptly.
        rx_thread = getattr(self, "_rxThread", None)
        if (
            rx_thread is not None
            and rx_thread.is_alive()
            and rx_thread != threading.current_thread()
        ):
            rx_thread.join(timeout=2.0)
            if rx_thread.is_alive():
                logger.warning("Reader thread did not exit within shutdown timeout")

    def _writeBytes(self, b: bytes) -> None:
        """Send the full byte sequence over the TCP socket.

        Attempts to transmit all bytes; if an OSError occurs, logs a warning, shuts
        down and closes the socket, and clears the stored socket reference.

        Parameters
        ----------
        b : bytes
            Bytes to send.
        """
        if self.socket is not None:
            try:
                # sendall() guarantees full payload transmission or raises.
                self.socket.sendall(b)
            except OSError as ex:
                logger.warning(
                    "TCP write failed (%d bytes), resetting socket: %s", len(b), ex
                )
                with contextlib.suppress(Exception):
                    self._socket_shutdown()
                with contextlib.suppress(Exception):
                    self.socket.close()
                self.socket = None

    def _readBytes(self, length: int) -> bytes | None:
        """Read up to `length` bytes from the TCP socket, handling dead connections and automatic reconnection.

        If a socket is present and data is available, returns the received bytes. If the
        socket is detected as disconnected, the method initiates a reconnect sequence and
        returns `None`. If no socket is available or a shutdown is requested, sets the
        reader to exit and returns `None`.

        Parameters
        ----------
        length : int
            Maximum number of bytes to read.

        Returns
        -------
        bytes | None
            The received bytes, or `None` if no data was returned
            because the socket is absent, a reconnect was started, or shutdown was
            requested.
        """
        if self.socket is not None:
            try:
                data = self.socket.recv(length)
            except OSError as ex:
                logger.debug("Socket read error, treating as dead socket: %s", ex)
                data = b""
            # empty byte indicates a disconnected socket,
            # we need to handle it to avoid an infinite loop reading from null socket
            if data == b"":
                logger.debug("dead socket, re-connecting")
                # cleanup and reconnect socket without breaking reader thread
                with contextlib.suppress(Exception):
                    self._socket_shutdown()
                with contextlib.suppress(Exception):
                    self.socket.close()
                self.socket = None
                if self._wantExit:
                    return None
                time.sleep(1)
                if self._wantExit:
                    return None
                self.myConnect()
                if self._wantExit:
                    # close() may race while we reconnect; tear down the new socket
                    with contextlib.suppress(Exception):
                        self._socket_shutdown()
                    with contextlib.suppress(Exception):
                        self.socket.close()
                    self.socket = None
                    return None
                self._startConfig()
                return None
            return data

        # no socket, break reader thread
        self._wantExit = True
        return None
