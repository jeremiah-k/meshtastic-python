"""Meshtastic unit tests for stream_interface.py."""

import logging
from unittest.mock import MagicMock, patch

import pytest

from ..stream_interface import MAX_TO_FROM_RADIO_SIZE, START1, START2, StreamInterface


@pytest.mark.unit
def test_stream_interface() -> None:
    """Verify that creating a StreamInterface without protocol configuration raises an error.

    Raises
    ------
    StreamInterface.StreamInterfaceError
        when a StreamInterface is instantiated without a protocol.
    """
    with pytest.raises(
        StreamInterface.StreamInterfaceError, match=r"StreamInterface is now abstract"
    ):
        StreamInterface()


# Note: This takes a bit, so moving from unit to slow
@pytest.mark.unitslow
@pytest.mark.usefixtures("reset_mt_config")
def test_stream_interface_with_no_proto(caplog: pytest.LogCaptureFixture) -> None:
    """Verify noProto StreamInterface can read and write through an assigned stream."""
    stream = MagicMock()
    test_data = b"hello"
    stream.read.return_value = test_data
    with caplog.at_level(logging.DEBUG):
        iface = StreamInterface(noProto=True, connectNow=False)
        try:
            iface.stream = stream
            iface._write_bytes(test_data)
            data = iface._read_bytes(len(test_data))
            assert data == test_data
        finally:
            iface.close()


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_send_to_radio_impl_frames_payload() -> None:
    """Test that _send_to_radio_impl writes a properly framed payload."""
    iface = StreamInterface(noProto=True, connectNow=False)
    try:
        payload = b"hello"
        to_radio = MagicMock()
        to_radio.SerializeToString.return_value = payload

        with patch.object(iface, "_write_bytes") as write_bytes:
            iface._send_to_radio_impl(to_radio)

            length_high = (len(payload) >> 8) & 0xFF
            length_low = len(payload) & 0xFF
            expected = bytes([START1, START2, length_high, length_low]) + payload
            write_bytes.assert_called_once_with(expected)
    finally:
        iface.close()


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_send_to_radio_impl_rejects_oversized_payload() -> None:
    """_send_to_radio_impl should raise PayloadTooLargeError for payloads > MAX_TO_FROM_RADIO_SIZE."""
    iface = StreamInterface(noProto=True, connectNow=False)
    try:
        to_radio = MagicMock()
        to_radio.SerializeToString.return_value = b"\x00" * (MAX_TO_FROM_RADIO_SIZE + 1)

        with pytest.raises(StreamInterface.PayloadTooLargeError):
            iface._send_to_radio_impl(to_radio)
    finally:
        iface.close()


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_write_and_read_skip_when_stream_not_open() -> None:
    """_write_bytes/_read_bytes should ignore a configured-but-closed stream."""
    iface = StreamInterface(noProto=True, connectNow=False)
    try:
        stream = MagicMock()
        stream.is_open = False
        iface.stream = stream

        iface._write_bytes(b"hello")
        assert iface._read_bytes(5) is None

        stream.write.assert_not_called()
        stream.flush.assert_not_called()
        stream.read.assert_not_called()
    finally:
        iface.close()


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_close_closes_stream_even_without_reader_thread() -> None:
    """close() should release stream resources even when connectNow=False."""
    iface = StreamInterface(noProto=True, connectNow=False)
    stream = MagicMock()
    stream.is_open = True
    iface.stream = stream

    iface.close()

    stream.close.assert_called_once()
    assert iface.stream is None
