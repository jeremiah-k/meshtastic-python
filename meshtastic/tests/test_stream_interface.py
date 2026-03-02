"""Meshtastic unit tests for stream_interface.py."""

from unittest.mock import MagicMock, patch

import pytest
import serial  # type: ignore[import-untyped]

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
def test_stream_interface_with_no_proto() -> None:
    """Verify noProto StreamInterface can read and write through an assigned stream."""
    stream = MagicMock()
    test_data = b"hello"
    stream.read.return_value = test_data
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
    """_write_bytes/_read_bytes should fail clearly for a configured-but-closed stream."""
    iface = StreamInterface(noProto=True, connectNow=False)
    try:
        stream = MagicMock()
        stream.is_open = False
        iface.stream = stream

        with pytest.raises(StreamInterface.StreamClosedError):
            iface._write_bytes(b"hello")
        with pytest.raises(StreamInterface.StreamClosedError):
            iface._read_bytes(5)

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


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_shared_close_runs_super_before_stream_close() -> None:
    """_shared_close should keep stream available during super().close() then close it."""
    iface = StreamInterface(noProto=True, connectNow=False)
    stream = MagicMock()
    stream.is_open = True
    iface.stream = stream
    super_close_stream_refs: list[object | None] = []

    def _fake_super_close(self: StreamInterface) -> None:
        super_close_stream_refs.append(self.stream)

    with patch("meshtastic.stream_interface.MeshInterface.close", _fake_super_close):
        iface._shared_close()

    assert super_close_stream_refs == [stream]
    stream.close.assert_called_once()
    assert iface.stream is None


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_connect_skips_wake_bytes_for_own_stream_interfaces() -> None:
    """connect() should not send wake bytes when subclass provides its own stream."""
    iface = StreamInterface(noProto=True, connectNow=False)
    try:
        # Exercise the code path used by stream-owning subclasses (e.g. TCPInterface).
        iface._provides_own_stream = True  # type: ignore[attr-defined]
        iface.stream = None
        iface._rxThread = MagicMock()
        iface._rxThread.is_alive.return_value = False
        iface._rxThread.ident = None
        with (
            patch.object(iface, "_write_bytes") as write_bytes,
            patch.object(iface, "_start_config") as start_config,
        ):
            iface.connect()
        write_bytes.assert_not_called()
        start_config.assert_called_once()
        iface._rxThread.start.assert_called_once()
    finally:
        iface.close()


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_connect_cleans_up_when_start_config_fails() -> None:
    """connect() should tear down reader/connection state if _start_config fails."""
    iface = StreamInterface(noProto=True, connectNow=False)
    try:
        iface._provides_own_stream = True  # type: ignore[attr-defined]
        iface.stream = None
        iface._rxThread = MagicMock()
        iface._rxThread.is_alive.return_value = False
        iface._rxThread.ident = None
        with (
            patch.object(iface, "_start_config", side_effect=RuntimeError("boom")),
            patch.object(iface, "_shared_close") as shared_close,
            patch.object(iface, "_join_reader_thread") as join_reader,
        ):
            with pytest.raises(RuntimeError, match="boom"):
                iface.connect()
        shared_close.assert_called_once()
        join_reader.assert_called_once()
    finally:
        iface.close()


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_connect_cleans_up_when_wait_connected_fails() -> None:
    """connect() should tear down reader/connection state if _wait_connected fails."""
    iface = StreamInterface(noProto=True, connectNow=False)
    try:
        iface._provides_own_stream = True  # type: ignore[attr-defined]
        iface.stream = None
        iface.noProto = False
        iface._rxThread = MagicMock()
        iface._rxThread.is_alive.return_value = False
        iface._rxThread.ident = None
        with (
            patch.object(iface, "_start_config"),
            patch.object(
                iface, "_wait_connected", side_effect=RuntimeError("wait failed")
            ),
            patch.object(iface, "_shared_close") as shared_close,
            patch.object(iface, "_join_reader_thread") as join_reader,
        ):
            with pytest.raises(RuntimeError, match="wait failed"):
                iface.connect()
        shared_close.assert_called_once()
        join_reader.assert_called_once()
    finally:
        iface.noProto = True
        iface.close()


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_write_bytes_retries_until_full_payload_written() -> None:
    """_write_bytes should handle partial writes until full payload is sent."""
    iface = StreamInterface(noProto=True, connectNow=False)
    stream = MagicMock()
    stream.is_open = True
    stream.write.side_effect = [2, 3]
    iface.stream = stream
    try:
        iface._write_bytes(b"hello")
        assert stream.write.call_count == 2
        stream.flush.assert_called_once()
    finally:
        iface.close()


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_write_bytes_raises_on_zero_progress() -> None:
    """_write_bytes should fail fast when stream write reports no progress."""
    iface = StreamInterface(noProto=True, connectNow=False)
    stream = MagicMock()
    stream.is_open = True
    stream.write.return_value = 0
    iface.stream = stream
    try:
        with pytest.raises(StreamInterface.StreamClosedError):
            iface._write_bytes(b"hello")
    finally:
        iface.close()


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_write_bytes_wraps_serial_exception_as_stream_closed() -> None:
    """_write_bytes should normalize serial-layer write failures to StreamClosedError."""
    iface = StreamInterface(noProto=True, connectNow=False)
    stream = MagicMock()
    stream.is_open = True
    stream.write.side_effect = serial.SerialException("boom")
    iface.stream = stream
    try:
        with pytest.raises(StreamInterface.StreamClosedError) as exc_info:
            iface._write_bytes(b"hello")
        assert isinstance(exc_info.value.__cause__, serial.SerialException)
    finally:
        iface.close()


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_reader_resyncs_when_start1_repeats_before_start2() -> None:
    """Reader should recover from START1,START1,START2 framing without dropping packet."""
    iface = StreamInterface(noProto=True, connectNow=False)
    try:
        read_bytes = iter(
            [
                bytes([START1]),
                bytes([START1]),
                bytes([START2]),
                b"\x00",
                b"\x01",
                b"X",
            ]
        )

        def _fake_read_bytes(_length: int) -> bytes:
            try:
                return next(read_bytes)
            except StopIteration:
                iface._wantExit = True
                return b""

        with (
            patch.object(iface, "_read_bytes", side_effect=_fake_read_bytes),
            patch.object(iface, "_handle_from_radio") as handle_from_radio,
            patch.object(iface, "_disconnected"),
            patch("meshtastic.stream_interface.time.sleep", lambda _seconds: None),
        ):
            iface._reader()

        handle_from_radio.assert_called_once_with(b"X")
    finally:
        iface.close()
