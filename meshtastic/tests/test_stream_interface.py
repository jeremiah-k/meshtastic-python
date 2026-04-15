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
def test_read_bytes_wraps_stream_exceptions_as_stream_closed() -> None:
    """_read_bytes should normalize backend read errors to StreamClosedError."""
    iface = StreamInterface(noProto=True, connectNow=False)
    try:
        stream = MagicMock()
        stream.is_open = True
        stream.read.side_effect = serial.SerialException("boom")
        iface.stream = stream

        with pytest.raises(StreamInterface.StreamClosedError) as exc_info:
            iface._read_bytes(5)
        assert isinstance(exc_info.value.__cause__, serial.SerialException)
    finally:
        iface.close()


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_read_bytes_propagates_unexpected_type_error() -> None:
    """_read_bytes should not swallow unexpected backend TypeError values."""
    iface = StreamInterface(noProto=True, connectNow=False)
    try:
        stream = MagicMock()
        stream.is_open = True
        stream.read.side_effect = TypeError("unexpected type bug")
        iface.stream = stream

        with pytest.raises(TypeError, match="unexpected type bug"):
            iface._read_bytes(5)
        stream.close.assert_not_called()
        assert iface.stream is stream
    finally:
        iface.close()


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_read_bytes_wraps_transport_fd_type_error() -> None:
    """_read_bytes should normalize transport fd-state TypeError to StreamClosedError."""
    iface = StreamInterface(noProto=True, connectNow=False)
    try:
        stream = MagicMock()
        stream.is_open = True
        stream.read.side_effect = TypeError(
            "'NoneType' object cannot be interpreted as an integer"
        )
        iface.stream = stream

        with pytest.raises(StreamInterface.StreamClosedError) as exc_info:
            iface._read_bytes(5)
        assert isinstance(exc_info.value.__cause__, TypeError)
    finally:
        iface.close()


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_write_bytes_propagates_type_error() -> None:
    """_write_bytes should not swallow backend TypeError programming errors."""
    iface = StreamInterface(noProto=True, connectNow=False)
    try:
        stream = MagicMock()
        stream.is_open = True
        stream.write.side_effect = TypeError("fd is None")
        iface.stream = stream

        with pytest.raises(TypeError, match="fd is None"):
            iface._write_bytes(b"xxx")
        stream.close.assert_not_called()
        assert iface.stream is stream
    finally:
        iface.close()


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_read_bytes_raises_stream_closed_when_backend_returns_none() -> None:
    """_read_bytes should treat None from backend read as closed stream."""
    iface = StreamInterface(noProto=True, connectNow=False)
    try:
        stream = MagicMock()
        stream.is_open = True
        stream.read.return_value = None
        iface.stream = stream

        with pytest.raises(StreamInterface.StreamClosedError):
            iface._read_bytes(5)
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
def test_close_stream_safely_suppresses_type_error() -> None:
    """_close_stream_safely() should swallow backend TypeError during close."""
    iface = StreamInterface(noProto=True, connectNow=False)
    try:
        stream = MagicMock()
        stream.close.side_effect = TypeError("fd is None")
        iface.stream = stream

        iface._close_stream_safely()

        stream.close.assert_called_once()
        assert iface.stream is None
    finally:
        iface.close()


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
def test_connect_ignores_requests_while_stream_close_in_progress() -> None:
    """connect() should not restart reader state while _shared_close is in progress."""
    iface = StreamInterface(noProto=True, connectNow=False)
    try:
        iface._provides_own_stream = True  # type: ignore[attr-defined]
        iface.stream = None
        iface._stream_close_in_progress = True
        iface._rxThread = MagicMock()
        iface._rxThread.is_alive.return_value = False
        iface._rxThread.ident = None
        with patch.object(iface, "_start_config") as start_config:
            iface.connect()
        start_config.assert_not_called()
        iface._rxThread.start.assert_not_called()
    finally:
        iface._stream_close_in_progress = False
        iface.close()


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_shared_close_resets_close_in_progress_on_super_close_error() -> None:
    """_shared_close should clear in-progress guard and close stream even if super close fails."""
    iface = StreamInterface(noProto=True, connectNow=False)
    stream = MagicMock()
    stream.is_open = True
    iface.stream = stream

    with patch(
        "meshtastic.stream_interface.MeshInterface.close",
        side_effect=RuntimeError("close failed"),
    ):
        with pytest.raises(RuntimeError, match="close failed"):
            iface._shared_close()

    assert iface._stream_close_in_progress is False
    stream.close.assert_called_once()
    assert iface.stream is None


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_close_joins_reader_thread_when_shared_close_raises() -> None:
    """close() should always attempt to join reader thread even on shared-close errors."""
    iface = StreamInterface(noProto=True, connectNow=False)
    with (
        patch.object(iface, "_shared_close", side_effect=RuntimeError("close failed")),
        patch.object(iface, "_join_reader_thread") as join_reader,
    ):
        with pytest.raises(RuntimeError, match="close failed"):
            iface.close()
    join_reader.assert_called_once()


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
def test_constructor_defers_connect_when_no_stream_available(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """__init__ should defer connect() when connectNow=True and no stream exists."""
    with patch.object(StreamInterface, "connect", autospec=True) as connect_mock:
        with caplog.at_level("DEBUG"):
            iface = StreamInterface(noProto=True, connectNow=True)
        try:
            connect_mock.assert_not_called()
            assert "deferring connect()" in caplog.text
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
        # Force protocol mode so connect() executes _wait_connected() path.
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
def test_write_bytes_times_out_when_no_progress_deadline_expires() -> None:
    """_write_bytes should raise StreamClosedError when progress deadline expires."""
    iface = StreamInterface(noProto=True, connectNow=False)
    stream = MagicMock()
    stream.is_open = True
    iface.stream = stream
    try:
        monotonic_calls = iter([0.0, 9999.0])
        with patch(
            "meshtastic.stream_interface.time.monotonic",
            side_effect=lambda: next(monotonic_calls, 9999.0),
        ):
            with pytest.raises(
                StreamInterface.StreamClosedError,
                match=StreamInterface.StreamClosedError.WRITE_TIMEOUT_MSG,
            ):
                iface._write_bytes(b"hello")
        stream.write.assert_not_called()
    finally:
        iface.close()


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_write_bytes_raises_when_backend_reports_none_written() -> None:
    """_write_bytes should treat a None write result as no progress."""
    iface = StreamInterface(noProto=True, connectNow=False)
    stream = MagicMock()
    stream.is_open = True
    stream.write.return_value = None
    iface.stream = stream
    try:
        with pytest.raises(
            StreamInterface.StreamClosedError,
            match=StreamInterface.StreamClosedError.WRITE_NO_PROGRESS_MSG,
        ):
            iface._write_bytes(b"hello")
    finally:
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


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_connect_resets_closing_for_reconnect() -> None:
    """connect() must reset _closing so a retry after close() can succeed."""
    stream = MagicMock()
    stream.read.return_value = b""
    stream.write.return_value = 1
    stream.is_open = True
    iface = StreamInterface(noProto=True, connectNow=False)
    try:
        iface.stream = stream
        iface.connect()
        assert iface._closing is False
    finally:
        iface.close()


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_connect_resets_closing_after_explicit_close() -> None:
    """After close() sets _closing=True, a subsequent connect() must clear it."""
    stream = MagicMock()
    stream.read.return_value = b""
    stream.write.return_value = 1
    stream.is_open = True
    iface = StreamInterface(noProto=True, connectNow=False)
    try:
        iface.stream = stream
        iface.connect()
        iface.close()
        assert iface._closing is True

        iface.stream = stream
        iface.connect()
        assert iface._closing is False
    finally:
        iface.close()


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_connect_raises_when_reader_thread_alive() -> None:
    """connect() must raise StreamInterfaceError if the reader thread from a prior attempt is still alive."""
    import threading

    iface = StreamInterface(noProto=True, connectNow=False)
    alive_event = threading.Event()
    blocking_thread = threading.Thread(
        target=alive_event.wait, kwargs={"timeout": 5}, daemon=True
    )
    blocking_thread.start()
    try:
        iface._rxThread = blocking_thread
        with pytest.raises(
            StreamInterface.StreamInterfaceError,
            match="Cannot reconnect: reader thread from previous attempt is still alive",
        ):
            iface.connect()
    finally:
        alive_event.set()
        blocking_thread.join(timeout=3)
        iface.close()


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_connect_succeeds_when_reader_thread_dead() -> None:
    """connect() should proceed normally when the previous reader thread is dead."""
    import threading

    iface = StreamInterface(noProto=True, connectNow=False)
    dead_thread = threading.Thread(target=lambda: None, daemon=True)
    dead_thread.start()
    dead_thread.join(timeout=3)
    iface._rxThread = dead_thread
    iface._provides_own_stream = True  # type: ignore[attr-defined]
    iface.stream = None
    with (
        patch.object(iface, "_start_config"),
    ):
        iface.connect()
    assert iface._rxThread.is_alive()
    iface.close()
