"""Meshtastic unit tests for ota.py."""

import contextlib
import hashlib
import logging
import os
import socket
import tempfile
from collections.abc import Callable, Generator
from unittest.mock import MagicMock, patch

import pytest

from meshtastic.ota import (
    ESP32WiFiOTA,
    OTAError,
    _file_sha256,
)


@pytest.fixture(name="temp_firmware_file")
def fixture_temp_firmware_file() -> Generator[Callable[[bytes], str], None, None]:
    """Create temporary firmware files and clean them up after each test."""
    created_files: list[str] = []

    def _create(data: bytes = b"fake firmware data") -> str:
        with tempfile.NamedTemporaryFile(mode="wb", delete=False) as handle:
            handle.write(data)
            created_files.append(handle.name)
            return handle.name

    yield _create

    for path in created_files:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(path)


@pytest.mark.unit
def test_file_sha256(temp_firmware_file: Callable[[bytes], str]) -> None:
    """Test _file_sha256 calculates correct hash."""
    test_data = b"Hello, World!"
    temp_file = temp_firmware_file(test_data)

    result = _file_sha256(temp_file)
    expected_hash = hashlib.sha256(test_data).hexdigest()
    assert result.hexdigest() == expected_hash


@pytest.mark.unit
def test_file_sha256_large_file(temp_firmware_file: Callable[[bytes], str]) -> None:
    """Test _file_sha256 handles files larger than chunk size."""
    test_data = b"A" * 8192  # More than 4096 bytes
    temp_file = temp_firmware_file(test_data)

    result = _file_sha256(temp_file)
    expected_hash = hashlib.sha256(test_data).hexdigest()
    assert result.hexdigest() == expected_hash


@pytest.mark.unit
def test_esp32_wifi_ota_init_file_not_found() -> None:
    """Test ESP32WiFiOTA raises FileNotFoundError for non-existent file."""
    with pytest.raises(FileNotFoundError, match="does not exist"):
        ESP32WiFiOTA("/nonexistent/firmware.bin", "192.168.1.1")


@pytest.mark.unit
def test_esp32_wifi_ota_init_success(
    temp_firmware_file: Callable[[bytes], str],
) -> None:
    """Test ESP32WiFiOTA initializes correctly with valid file."""
    temp_file = temp_firmware_file(b"fake firmware data")

    ota = ESP32WiFiOTA(temp_file, "192.168.1.1", 3232)
    assert ota._filename == temp_file
    assert ota._hostname == "192.168.1.1"
    assert ota._port == 3232
    assert ota._socket is None
    # Verify hash is calculated
    assert ota._file_hash is not None
    assert len(ota.hashHex()) == 64  # SHA256 hex is 64 chars


@pytest.mark.unit
def test_esp32_wifi_ota_init_default_port(
    temp_firmware_file: Callable[[bytes], str],
) -> None:
    """Test ESP32WiFiOTA uses default port 3232."""
    temp_file = temp_firmware_file(b"fake firmware data")
    ota = ESP32WiFiOTA(temp_file, "192.168.1.1")
    assert ota._port == 3232


@pytest.mark.unit
def test_esp32_wifi_ota_hash_bytes(temp_firmware_file: Callable[[bytes], str]) -> None:
    """Test hash_bytes returns correct bytes."""
    test_data = b"firmware data"
    temp_file = temp_firmware_file(test_data)

    ota = ESP32WiFiOTA(temp_file, "192.168.1.1")
    hash_bytes = ota.hashBytes()
    expected_bytes = hashlib.sha256(test_data).digest()
    assert hash_bytes == expected_bytes
    assert len(hash_bytes) == 32  # SHA256 is 32 bytes


@pytest.mark.unit
def test_esp32_wifi_ota_hash_hex(temp_firmware_file: Callable[[bytes], str]) -> None:
    """Test hash_hex returns correct hex string."""
    test_data = b"firmware data"
    temp_file = temp_firmware_file(test_data)

    ota = ESP32WiFiOTA(temp_file, "192.168.1.1")
    hash_hex = ota.hashHex()
    expected_hex = hashlib.sha256(test_data).hexdigest()
    assert hash_hex == expected_hex
    assert len(hash_hex) == 64  # SHA256 hex is 64 chars
    assert ota.hash_hex() == hash_hex


@pytest.mark.unit
def test_esp32_wifi_ota_read_line_not_connected(
    temp_firmware_file: Callable[[bytes], str],
) -> None:
    """Test _read_line raises ConnectionError when not connected."""
    temp_file = temp_firmware_file(b"firmware")
    ota = ESP32WiFiOTA(temp_file, "192.168.1.1")
    with pytest.raises(ConnectionError, match="Socket not connected"):
        ota._read_line()


@pytest.mark.unit
def test_esp32_wifi_ota_read_line_connection_closed(
    temp_firmware_file: Callable[[bytes], str],
) -> None:
    """Test _read_line raises ConnectionError when connection closed."""
    temp_file = temp_firmware_file(b"firmware")
    ota = ESP32WiFiOTA(temp_file, "192.168.1.1")
    mock_socket = MagicMock()
    # Simulate connection closed
    mock_socket.recv.return_value = b""
    ota._socket = mock_socket

    with pytest.raises(ConnectionError, match="Connection closed"):
        ota._read_line()


@pytest.mark.unit
def test_esp32_wifi_ota_read_line_success(
    temp_firmware_file: Callable[[bytes], str],
) -> None:
    """Test _read_line successfully reads a line."""
    temp_file = temp_firmware_file(b"firmware")
    ota = ESP32WiFiOTA(temp_file, "192.168.1.1")
    mock_socket = MagicMock()
    # Simulate receiving "OK\n"
    mock_socket.recv.side_effect = [b"O", b"K", b"\n"]
    ota._socket = mock_socket

    result = ota._read_line()
    assert result == "OK"


@pytest.mark.unit
@patch("meshtastic.ota.socket.socket")
def test_esp32_wifi_ota_update_success(
    mock_socket_class: MagicMock,
    temp_firmware_file: Callable[[bytes], str],
) -> None:
    """Test update() with successful OTA."""
    test_data = b"A" * 1024  # 1KB of data
    temp_file = temp_firmware_file(test_data)

    # Setup mock socket
    mock_socket = MagicMock()
    mock_socket_class.return_value = mock_socket

    ota = ESP32WiFiOTA(temp_file, "192.168.1.1")

    # Mock _read_line to return appropriate responses
    # First call: ERASING, Second call: OK (ready), Third call: OK (complete)
    with patch.object(ota, "_read_line") as mock_read_line:
        mock_read_line.side_effect = [
            "ERASING",  # Device is erasing flash
            "OK",  # Device ready for firmware
            "OK",  # Device finished successfully
        ]

        ota.update()

        # Verify socket was created and connected
        mock_socket_class.assert_called_once_with(socket.AF_INET, socket.SOCK_STREAM)
        mock_socket.settimeout.assert_called_once_with(15)
        mock_socket.connect.assert_called_once_with(("192.168.1.1", 3232))

        # Verify start command was sent
        start_cmd = f"OTA {len(test_data)} {ota.hashHex()}\n".encode("utf-8")
        mock_socket.sendall.assert_any_call(start_cmd)

        # Verify firmware was sent (at least one chunk)
        assert mock_socket.sendall.call_count >= 2

        # Verify socket was closed
        mock_socket.close.assert_called_once()


@pytest.mark.unit
@patch("meshtastic.ota.socket.socket")
def test_esp32_wifi_ota_update_with_progress_callback(
    mock_socket_class: MagicMock,
    temp_firmware_file: Callable[[bytes], str],
) -> None:
    """Test update() with progress callback."""
    test_data = b"A" * 1024  # 1KB of data
    temp_file = temp_firmware_file(test_data)

    # Setup mock socket
    mock_socket = MagicMock()
    mock_socket_class.return_value = mock_socket

    ota = ESP32WiFiOTA(temp_file, "192.168.1.1")

    # Track progress callback calls
    progress_calls = []

    def progress_callback(sent: int, total: int) -> None:
        progress_calls.append((sent, total))

    # Mock _read_line
    with patch.object(ota, "_read_line") as mock_read_line:
        mock_read_line.side_effect = [
            "OK",  # Device ready
            "OK",  # Device finished
        ]

        ota.update(progress_callback=progress_callback)

        # Verify progress callback was called
        assert len(progress_calls) > 0
        # First call should show some progress
        assert progress_calls[0][0] > 0
        # Total should be the firmware size
        assert progress_calls[0][1] == len(test_data)
        # Last call should show all bytes sent
        assert progress_calls[-1][0] == len(test_data)


@pytest.mark.unit
@patch("meshtastic.ota.socket.socket")
def test_esp32_wifi_ota_update_device_error_on_start(
    mock_socket_class: MagicMock,
    temp_firmware_file: Callable[[bytes], str],
) -> None:
    """Test update() raises OTAError when device reports error during start."""
    temp_file = temp_firmware_file(b"firmware")
    mock_socket = MagicMock()
    mock_socket_class.return_value = mock_socket

    ota = ESP32WiFiOTA(temp_file, "192.168.1.1")

    with patch.object(ota, "_read_line") as mock_read_line:
        mock_read_line.return_value = "ERR BAD_HASH"

        with pytest.raises(OTAError, match="Device reported error"):
            ota.update()


@pytest.mark.unit
@patch("meshtastic.ota.socket.socket")
def test_esp32_wifi_ota_update_device_error_on_finish(
    mock_socket_class: MagicMock,
    temp_firmware_file: Callable[[bytes], str],
) -> None:
    """Test update() raises OTAError when device reports error after firmware sent."""
    temp_file = temp_firmware_file(b"firmware")
    mock_socket = MagicMock()
    mock_socket_class.return_value = mock_socket

    ota = ESP32WiFiOTA(temp_file, "192.168.1.1")

    with patch.object(ota, "_read_line") as mock_read_line:
        mock_read_line.side_effect = [
            "OK",  # Device ready
            "ERR FLASH_ERR",  # Error after firmware sent
        ]

        with pytest.raises(OTAError, match="OTA update failed"):
            ota.update()


@pytest.mark.unit
@patch("meshtastic.ota.socket.socket")
def test_esp32_wifi_ota_update_socket_cleanup_on_error(
    mock_socket_class: MagicMock,
    temp_firmware_file: Callable[[bytes], str],
) -> None:
    """Test that socket is properly cleaned up on error."""
    temp_file = temp_firmware_file(b"firmware")
    mock_socket = MagicMock()
    mock_socket_class.return_value = mock_socket

    ota = ESP32WiFiOTA(temp_file, "192.168.1.1")

    # Simulate connection error
    mock_socket.connect.side_effect = ConnectionRefusedError("Connection refused")

    with pytest.raises(ConnectionRefusedError):
        ota.update()

    # Verify socket was closed even on error
    mock_socket.close.assert_called_once()
    assert ota._socket is None


@pytest.mark.unit
@patch("meshtastic.ota.socket.socket")
def test_esp32_wifi_ota_update_large_firmware(
    mock_socket_class: MagicMock,
    temp_firmware_file: Callable[[bytes], str],
) -> None:
    """Test update() correctly chunks large firmware files."""
    # Create a file larger than chunk_size (1024)
    test_data = b"B" * 3000
    temp_file = temp_firmware_file(test_data)

    mock_socket = MagicMock()
    mock_socket_class.return_value = mock_socket

    ota = ESP32WiFiOTA(temp_file, "192.168.1.1")

    with patch.object(ota, "_read_line") as mock_read_line:
        mock_read_line.side_effect = [
            "OK",  # Device ready
            "OK",  # Device finished
        ]

        ota.update()

        # Verify that all data was sent in chunks
        # 3000 bytes should be sent in ~3 chunks of 1024 bytes
        sendall_calls = [
            call
            for call in mock_socket.sendall.call_args_list
            if call[0][0] != f"OTA {len(test_data)} {ota.hashHex()}\n".encode("utf-8")
        ]
        # Calculate total data sent (excluding the start command)
        total_sent = sum(len(call[0][0]) for call in sendall_calls)
        assert total_sent == len(test_data)


@pytest.mark.unit
@patch("meshtastic.ota.socket.socket")
def test_esp32_wifi_ota_update_unexpected_response_warning(
    mock_socket_class: MagicMock,
    caplog: pytest.LogCaptureFixture,
    temp_firmware_file: Callable[[bytes], str],
) -> None:
    """Test update() logs warning on unexpected response during startup."""
    temp_file = temp_firmware_file(b"firmware")
    mock_socket = MagicMock()
    mock_socket_class.return_value = mock_socket

    ota = ESP32WiFiOTA(temp_file, "192.168.1.1")

    with patch.object(ota, "_read_line") as mock_read_line:
        mock_read_line.side_effect = [
            "UNKNOWN",  # Unexpected response
            "OK",  # Then proceed
            "OK",  # Device finished
        ]

        with caplog.at_level(logging.WARNING):
            ota.update()

            # Check that warning was logged for unexpected response
            assert "Unexpected response" in caplog.text


@pytest.mark.unit
@patch("meshtastic.ota.socket.socket")
def test_esp32_wifi_ota_update_unexpected_final_response(
    mock_socket_class: MagicMock,
    caplog: pytest.LogCaptureFixture,
    temp_firmware_file: Callable[[bytes], str],
) -> None:
    """Test update() logs warning on unexpected final response after firmware upload."""
    temp_file = temp_firmware_file(b"firmware")
    mock_socket = MagicMock()
    mock_socket_class.return_value = mock_socket

    ota = ESP32WiFiOTA(temp_file, "192.168.1.1")

    with patch.object(ota, "_read_line") as mock_read_line:
        mock_read_line.side_effect = [
            "OK",  # Device ready for firmware
            "UNKNOWN",  # Unexpected final response (not OK, not ERR, not ACK)
            "OK",  # Then succeed
        ]

        with caplog.at_level(logging.WARNING):
            ota.update()

            # Check that warning was logged for unexpected final response
            assert "Unexpected final response" in caplog.text
