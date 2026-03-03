"""Meshtastic ESP32 Unified OTA."""

import hashlib
import logging
import os
import socket
from typing import Any, Callable, Protocol

logger = logging.getLogger(__name__)

FILE_HASH_READ_CHUNK_SIZE_BYTES = 4096
OTA_SOCKET_TIMEOUT_SECONDS = 15
OTA_CHUNK_SIZE_BYTES = 1024


class _SHA256Digest(Protocol):
    """Minimal digest protocol returned by hashlib.sha256()."""

    def update(self, data: Any, /) -> None:
        """Update the digest with bytes."""
        raise NotImplementedError

    def digest(self) -> bytes:
        """Return raw digest bytes."""
        raise NotImplementedError

    def hexdigest(self) -> str:
        """Return digest as hexadecimal string."""
        raise NotImplementedError


def _file_sha256(filename: str) -> _SHA256Digest:
    """Calculate SHA256 hash of a file."""
    sha256_hash = hashlib.sha256()

    with open(filename, "rb") as f:
        for byte_block in iter(lambda: f.read(FILE_HASH_READ_CHUNK_SIZE_BYTES), b""):
            sha256_hash.update(byte_block)

    return sha256_hash


class OTAError(Exception):
    """Exception for OTA errors."""


class ESP32WiFiOTA:
    """ESP32 WiFi Unified OTA updates."""

    def __init__(self, filename: str, hostname: str, port: int = 3232) -> None:
        self._filename = filename
        self._hostname = hostname
        self._port = port
        self._socket: socket.socket | None = None

        if not os.path.exists(self._filename):
            raise FileNotFoundError(f"File {self._filename} does not exist")

        self._file_hash: _SHA256Digest = _file_sha256(self._filename)

    def _read_line(self) -> str:
        """Read a line from the socket."""
        if not self._socket:
            raise ConnectionError("Socket not connected")

        line = b""
        while not line.endswith(b"\n"):
            char = self._socket.recv(1)

            if not char:
                raise ConnectionError("Connection closed while waiting for response")

            line += char

        return line.decode("utf-8").strip()

    def hashBytes(self) -> bytes:
        """Return the hash as bytes."""
        return self._file_hash.digest()

    def hashHex(self) -> str:
        """Return the hash as a hex string."""
        return self._file_hash.hexdigest()

    # COMPAT_STABLE_SHIM: snake_case alias for historical callers.
    def hash_bytes(self) -> bytes:
        """Compatibility alias for hashBytes()."""
        return self.hashBytes()

    # COMPAT_STABLE_SHIM: snake_case alias for historical callers.
    def hash_hex(self) -> str:
        """Compatibility alias for hashHex()."""
        return self.hashHex()

    def update(
        self, progress_callback: Callable[[int, int], None] | None = None
    ) -> None:
        """Perform the OTA update.

        Parameters
        ----------
        progress_callback : Callable[[int, int], None] | None
            Optional callback invoked with ``(sent_bytes, total_bytes)`` after
            each chunk. When omitted, progress is printed to stdout.
        """
        with open(self._filename, "rb") as f:
            data = f.read()
        size = len(data)

        logger.info(
            f"Starting OTA update with {self._filename} ({size} bytes, hash {self.hashHex()})"
        )

        self._socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._socket.settimeout(OTA_SOCKET_TIMEOUT_SECONDS)
        try:
            self._socket.connect((self._hostname, self._port))
            logger.debug(f"Connected to {self._hostname}:{self._port}")

            # Send start command
            self._socket.sendall(f"OTA {size} {self.hashHex()}\n".encode("utf-8"))

            # Wait for OK from the device
            while True:
                response = self._read_line()
                if response == "OK":
                    break

                if response == "ERASING":
                    logger.info("Device is erasing flash...")
                elif response.startswith("ERR "):
                    raise OTAError(f"Device reported error: {response}")
                else:
                    logger.warning(f"Unexpected response: {response}")

            # Stream firmware
            sent_bytes = 0
            while sent_bytes < size:
                chunk = data[sent_bytes : sent_bytes + OTA_CHUNK_SIZE_BYTES]
                self._socket.sendall(chunk)
                sent_bytes += len(chunk)

                if progress_callback:
                    progress_callback(sent_bytes, size)
                else:
                    print(
                        f"[{sent_bytes / size * 100:5.1f}%] Sent {sent_bytes} of {size} bytes...",
                        end="\r",
                    )

            if not progress_callback:
                print()

            # Wait for OK from device
            logger.info("Firmware sent, waiting for verification...")
            while True:
                response = self._read_line()
                if response == "OK":
                    logger.info("OTA update completed successfully!")
                    break

                if response.startswith("ERR "):
                    raise OTAError(f"OTA update failed: {response}")
                elif response != "ACK":
                    logger.warning(f"Unexpected final response: {response}")

        finally:
            if self._socket:
                self._socket.close()
                self._socket = None
