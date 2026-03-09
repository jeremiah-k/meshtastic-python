"""Meshtastic ESP32 Unified OTA."""

import os
import hashlib
import logging
import socket
from typing import Callable, Protocol

logger = logging.getLogger(__name__)
OTA_SOCKET_TIMEOUT_SECONDS = 15
OTA_CHUNK_SIZE_BYTES = 1024
FILE_HASH_READ_CHUNK_SIZE_BYTES = 4096
OTA_PROGRESS_LOG_PERCENT_STEP: float = 5.0
MISSING_FIRMWARE_ERROR: str = "Firmware file {filename} does not exist"
EMPTY_FIRMWARE_ERROR: str = "Firmware file {filename} is empty"
READ_FIRMWARE_ERROR: str = "Unable to read firmware file {filename}: {error}"
FIRMWARE_CHANGED_ERROR: str = (
    "Firmware file {filename} changed after OTA session initialization."
)
OTA_TRANSPORT_ERROR: str = "OTA transport to {host}:{port} failed: {error}"


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

    with open(filename, "rb") as firmware:
        for byte_block in iter(
            lambda: firmware.read(FILE_HASH_READ_CHUNK_SIZE_BYTES), b""
        ):
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

        self._size = 0
        self._file_hash: _SHA256Digest = hashlib.sha256()
        self._refresh_firmware_metadata()

    def _refresh_firmware_metadata(self) -> tuple[int, _SHA256Digest]:
        """Refresh cached firmware size/hash from disk and validate non-empty file.

        Returns
        -------
        tuple[int, _SHA256Digest]
            The firmware size in bytes and corresponding SHA-256 digest.
        """
        file_hash = hashlib.sha256()
        size = 0
        try:
            with open(self._filename, "rb") as firmware:
                for block in iter(
                    lambda: firmware.read(FILE_HASH_READ_CHUNK_SIZE_BYTES), b""
                ):
                    size += len(block)
                    file_hash.update(block)
        except FileNotFoundError as exc:
            raise OTAError(MISSING_FIRMWARE_ERROR.format(filename=self._filename)) from exc
        except OSError as exc:
            raise OTAError(
                READ_FIRMWARE_ERROR.format(filename=self._filename, error=exc)
            ) from exc
        if size == 0:
            raise OTAError(EMPTY_FIRMWARE_ERROR.format(filename=self._filename))
        self._size = size
        self._file_hash = file_hash
        return size, file_hash

    def _assert_session_firmware_unchanged(
        self, *, current_size: int, current_hash: _SHA256Digest
    ) -> None:
        """Ensure OTA session metadata still matches the file snapshot being uploaded."""
        if (
            current_size != self._size
            or current_hash.digest() != self._file_hash.digest()
        ):
            raise OTAError(FIRMWARE_CHANGED_ERROR.format(filename=self._filename))

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
        image = bytearray()
        file_hash = hashlib.sha256()
        try:
            with open(self._filename, "rb") as firmware:
                disk_size = os.fstat(firmware.fileno()).st_size
                if disk_size == 0:
                    raise OTAError(EMPTY_FIRMWARE_ERROR.format(filename=self._filename))
                if disk_size != self._size:
                    raise OTAError(FIRMWARE_CHANGED_ERROR.format(filename=self._filename))

                bytes_remaining = self._size
                while bytes_remaining > 0:
                    block = firmware.read(
                        min(FILE_HASH_READ_CHUNK_SIZE_BYTES, bytes_remaining)
                    )
                    if not block:
                        break
                    image.extend(block)
                    file_hash.update(block)
                    bytes_remaining -= len(block)

                if firmware.read(1):
                    raise OTAError(FIRMWARE_CHANGED_ERROR.format(filename=self._filename))
        except FileNotFoundError as exc:
            raise OTAError(MISSING_FIRMWARE_ERROR.format(filename=self._filename)) from exc
        except OSError as exc:
            raise OTAError(
                READ_FIRMWARE_ERROR.format(filename=self._filename, error=exc)
            ) from exc

        firmware_image = memoryview(image)
        size = len(firmware_image)
        if size == 0:
            raise OTAError(EMPTY_FIRMWARE_ERROR.format(filename=self._filename))
        self._assert_session_firmware_unchanged(
            current_size=size, current_hash=file_hash
        )

        if OTA_PROGRESS_LOG_PERCENT_STEP <= 0:
            raise ValueError("OTA_PROGRESS_LOG_PERCENT_STEP must be > 0")
        next_progress_log_percent = OTA_PROGRESS_LOG_PERCENT_STEP

        logger.info(
            "Starting OTA update with %s (%d bytes, hash %s)",
            self._filename,
            size,
            file_hash.hexdigest(),
        )

        try:
            self._socket = socket.create_connection(
                (self._hostname, self._port),
                timeout=OTA_SOCKET_TIMEOUT_SECONDS,
            )
            logger.debug("Connected to %s:%d", self._hostname, self._port)

            # Send start command
            self._socket.sendall(f"OTA {size} {file_hash.hexdigest()}\n".encode())

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

            sent_bytes = 0
            for offset in range(0, size, OTA_CHUNK_SIZE_BYTES):
                chunk = firmware_image[offset : offset + OTA_CHUNK_SIZE_BYTES]
                self._socket.sendall(chunk)
                sent_bytes += len(chunk)

                if progress_callback is not None:
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
                            next_progress_log_percent += OTA_PROGRESS_LOG_PERCENT_STEP

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
        except (ConnectionError, OSError) as exc:
            raise OTAError(
                OTA_TRANSPORT_ERROR.format(
                    host=self._hostname,
                    port=self._port,
                    error=exc,
                )
            ) from exc
        finally:
            if self._socket:
                self._socket.close()
                self._socket = None
