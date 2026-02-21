"""TCPInterface class for interfacing with a TCP endpoint."""

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
        """Open a connection to a specified IP address/hostname.

        Keyword Arguments:
            hostname {string} -- Hostname/IP address of the device to connect to
            timeout -- How long to wait for replies (default: 300 seconds)

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
        """Shutdown the socket.
        Note: Broke out this line so the exception could be unit tested.
        """
        if self.socket is not None:
            self.socket.shutdown(socket.SHUT_RDWR)

    def myConnect(self) -> None:
        """Connect to socket."""
        logger.debug("Connecting to %s", self.hostname)
        server_address = (self.hostname, self.portNumber)
        self.socket = socket.create_connection(server_address)

    def close(self) -> None:
        """Close a connection to the device."""
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
        """Write an array of bytes to our stream and flush."""
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
        """Read an array of bytes from our stream."""
        if self.socket is not None:
            data = self.socket.recv(length)
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
