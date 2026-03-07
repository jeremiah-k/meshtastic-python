"""Meshtastic ESP32 Unified OTA."""

import hashlib
import logging
import os
import socket
from typing import Callable, Protocol

logger = logging.getLogger(__name__)
OTA_SOCKET_TIMEOUT_SECONDS = 15
OTA_CHUNK_SIZE_BYTES = 1024
FILE_HASH_READ_CHUNK_SIZE_BYTES = 4096
OTA_PROGRESS_LOG_PERCENT_STEP: float = 5.0
EMPTY_FIRMWARE_ERROR: str = "Firmware file {filename} is empty"


class _SHA256Digest(Protocol):
    """Minimal digest protocol returned by hashlib.sha256()."""

    def update(self, data: bytes) -> None:
        """Update the digest with bytes."""

    def digest(self) -> bytes:
        """Return raw digest bytes."""

    def hexdigest(self) -> str:
        """Return digest as hexadecimal string."""


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

        self._size = os.path.getsize(self._filename)
        if self._size == 0:
            raise OTAError(EMPTY_FIRMWARE_ERROR.format(filename=self._filename))

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

    # COMPAT_STABLE_SHIM: historical snake_case alias.
    def hash_bytes(self) -> bytes:
        """Compatibility alias for hashBytes()."""
        return self.hashBytes()

    def hashHex(self) -> str:
        """Return the hash as a hex string."""
        return self._file_hash.hexdigest()

    # COMPAT_STABLE_SHIM: historical snake_case alias.
    def hash_hex(self) -> str:
        """Compatibility alias for hashHex()."""
        return self.hashHex()

    def update(
        self, progress_callback: Callable[[int, int], None] | None = None
    ) -> None:
        """Perform the OTA update.

        Parameters
        ----------
        progress_callback : Callable[[int, int], None] | None, optional
            Callback invoked with ``(bytes_sent, total_bytes)`` during transfer.
            When not provided, progress is logged at INFO in coarse increments.
        """
        size = self._size
        if OTA_PROGRESS_LOG_PERCENT_STEP <= 0:
            raise ValueError("OTA_PROGRESS_LOG_PERCENT_STEP must be > 0")
        next_progress_log_percent = OTA_PROGRESS_LOG_PERCENT_STEP

        logger.info(
            "Starting OTA update with %s (%d bytes, hash %s)",
            self._filename,
            size,
            self.hashHex(),
        )

        self._socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._socket.settimeout(OTA_SOCKET_TIMEOUT_SECONDS)
        try:
            self._socket.connect((self._hostname, self._port))
            logger.debug("Connected to %s:%d", self._hostname, self._port)

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
                    logger.warning("Unexpected response: %s", response)

            # Stream firmware
            sent_bytes = 0
            with open(self._filename, "rb") as f:
                while True:
                    chunk = f.read(OTA_CHUNK_SIZE_BYTES)
                    if not chunk:
                        break
                    self._socket.sendall(chunk)
                    sent_bytes += len(chunk)

                    if progress_callback:
                        progress_callback(sent_bytes, size)
                    else:
                        progress_percent = sent_bytes / size * 100
                        if (
                            sent_bytes == size
                            or progress_percent >= next_progress_log_percent
                        ):
                            logger.info(
                                "OTA progress: %.1f%% (%d/%d bytes)",
                                progress_percent,
                                sent_bytes,
                                size,
                            )
                            while next_progress_log_percent <= progress_percent:
                                next_progress_log_percent += (
                                    OTA_PROGRESS_LOG_PERCENT_STEP
                                )

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
                    logger.warning("Unexpected final response: %s", response)

        finally:
            if self._socket:
                self._socket.close()
                self._socket = None
