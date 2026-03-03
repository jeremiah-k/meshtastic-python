"""Meshtastic unit tests for tcp_interface.py."""

import re
import threading
import time
from typing import cast
from unittest.mock import MagicMock, patch

import pytest

from ..protobuf import config_pb2
from ..tcp_interface import TCPInterface


@pytest.mark.unit
def test_TCPInterface(capsys: pytest.CaptureFixture[str]) -> None:
    """Test that we can instantiate a TCPInterface."""
    with patch("socket.socket") as mock_socket:
        iface = TCPInterface(hostname="localhost", noProto=True)
        try:
            iface.localNode.localConfig.lora.CopyFrom(config_pb2.Config.LoRaConfig())
            iface.myConnect()
            iface.showInfo()
            iface.localNode.showInfo()
            out, err = capsys.readouterr()
            assert re.search(r"Owner: None \(None\)", out, re.MULTILINE)
            assert re.search(r"Nodes", out, re.MULTILINE)
            assert re.search(r"Preferences", out, re.MULTILINE)
            assert re.search(r"Channels", out, re.MULTILINE)
            assert re.search(r"Primary channel URL", out, re.MULTILINE)
            assert err == ""
            assert mock_socket.called
        finally:
            iface.close()


@pytest.mark.unit
def test_TCPInterface_exception() -> None:
    """Verify TCPInterface.close() handles exceptions from _socket_shutdown.

    Ensures shutdown exceptions are suppressed so close() completes.
    """

    def throw_an_exception(*_args: object) -> None:
        """Raise a ValueError with the message "Fake exception.".

        Raises
        ------
        ValueError
            Always raised with the message "Fake exception.".
        """
        raise ValueError("Fake exception.")

    with patch(
        "meshtastic.tcp_interface.TCPInterface._socket_shutdown"
    ) as mock_shutdown:
        mock_shutdown.side_effect = throw_an_exception
        with patch("socket.socket") as mock_socket:
            iface = TCPInterface(hostname="localhost", noProto=True)
            iface.myConnect()
            iface.close()
            assert mock_socket.called
            assert mock_shutdown.called


@pytest.mark.unit
def test_TCPInterface_myConnect_clears_socket_timeout() -> None:
    """MyConnect should restore blocking mode after connect-timeout use."""
    with patch("meshtastic.tcp_interface.socket.create_connection") as mock_connect:
        connected_socket = MagicMock()
        mock_connect.return_value = connected_socket
        iface = TCPInterface(hostname="localhost", noProto=True, connectNow=False)
        try:
            iface.myConnect()
            connected_socket.settimeout.assert_called_once_with(None)
        finally:
            iface.close()


@pytest.mark.unit
def test_TCPInterface_without_connecting() -> None:
    """Test that we can instantiate a TCPInterface with connectNow as false."""
    with patch("socket.socket"):
        iface = TCPInterface(hostname="localhost", noProto=True, connectNow=False)
        try:
            assert iface.socket is None
        finally:
            iface.close()


@pytest.mark.unit
@pytest.mark.parametrize(
    "connect_timeout",
    [
        pytest.param(0.0, id="zero"),
        pytest.param(-1.0, id="negative"),
        pytest.param(True, id="bool"),
        pytest.param(float("nan"), id="nan"),
        pytest.param(float("inf"), id="inf"),
        pytest.param("invalid-timeout", id="string"),
    ],
)
def test_TCPInterface_rejects_non_positive_connect_timeout(
    connect_timeout: object,
) -> None:
    """Constructor should fail fast for invalid connectTimeout values."""
    with pytest.raises(
        ValueError,
        match=r"connectTimeout must be a positive finite number",
    ):
        TCPInterface(
            hostname="localhost",
            noProto=True,
            connectNow=False,
            connectTimeout=cast(float | None, connect_timeout),
        )


@pytest.mark.unit
def test_TCPInterface_accepts_none_connect_timeout() -> None:
    """None connectTimeout should defer to socket's default timeout behavior."""
    with patch("meshtastic.tcp_interface.socket.create_connection") as mock_connect:
        connected_socket = MagicMock()
        mock_connect.return_value = connected_socket
        iface = TCPInterface(
            hostname="localhost",
            noProto=True,
            connectNow=False,
            connectTimeout=None,
        )

        iface.myConnect()

        mock_connect.assert_called_once_with(("localhost", 4403))
        connected_socket.settimeout.assert_called_once_with(None)
        iface.close()


@pytest.mark.unit
def test_TCPInterface_write_uses_sendall() -> None:
    """Test that _write_bytes uses sendall to avoid partial writes."""
    with patch("socket.socket"):
        iface = TCPInterface(hostname="localhost", noProto=True, connectNow=False)
        mock_socket = MagicMock()
        iface.socket = mock_socket

        iface._write_bytes(b"abc")

        mock_socket.sendall.assert_called_once_with(b"abc")
        iface.close()


@pytest.mark.unit
def test_TCPInterface_write_raises_when_socket_missing() -> None:
    """_write_bytes should fail fast when no TCP socket is connected."""
    with patch("socket.socket"):
        iface = TCPInterface(hostname="localhost", noProto=True, connectNow=False)
        try:
            iface.socket = None
            with pytest.raises(ConnectionError, match="closed or not connected"):
                iface._write_bytes(b"abc")
        finally:
            iface.close()


@pytest.mark.unit
def test_TCPInterface_write_reraises_socket_errors() -> None:
    """_write_bytes should propagate socket write failures after cleanup."""
    with patch("socket.socket"):
        iface = TCPInterface(hostname="localhost", noProto=True, connectNow=False)
        try:
            mock_socket = MagicMock()
            mock_socket.sendall.side_effect = OSError("boom")
            iface.socket = mock_socket

            with pytest.raises(OSError, match="boom"):
                iface._write_bytes(b"abc")

            assert iface.socket is None
        finally:
            iface.close()


@pytest.mark.unit
def test_TCPInterface_read_empty_does_not_reconnect_when_closing() -> None:
    """Test that _read_bytes avoids reconnect attempts during intentional shutdown."""
    with patch("socket.socket"):
        iface = TCPInterface(hostname="localhost", noProto=True, connectNow=False)
        try:
            mock_socket = MagicMock()
            mock_socket.recv.return_value = b""
            iface.socket = mock_socket
            iface._wantExit = True

            with (
                patch.object(iface, "myConnect") as mock_connect,
                patch.object(iface, "_start_config") as mock_start_config,
                patch("meshtastic.tcp_interface.time.sleep") as mock_sleep,
            ):
                data = iface._read_bytes(1)

            assert data == b""
            mock_connect.assert_not_called()
            mock_start_config.assert_not_called()
            mock_sleep.assert_not_called()
            mock_socket.close.assert_called_once()
            assert iface.socket is None
        finally:
            iface.close()


@pytest.mark.unit
def test_TCPInterface_connect_fails_fast_when_shutting_down() -> None:
    """connect() should fail fast when shutdown/fatal flags block a new connect."""
    with patch("socket.socket"):
        iface = TCPInterface(hostname="localhost", noProto=True, connectNow=False)
        iface.socket = None
        iface._wantExit = True
        try:
            with (
                patch.object(iface, "myConnect") as mock_connect,
                patch("meshtastic.tcp_interface.StreamInterface.connect") as mock_super,
            ):
                with pytest.raises(
                    ConnectionError,
                    match=r"Cannot connect to localhost: interface is shutting down",
                ):
                    iface.connect()
            mock_connect.assert_not_called()
            mock_super.assert_not_called()
        finally:
            iface.close()


@pytest.mark.unit
def test_TCPInterface_connect_fails_fast_after_fatal_disconnect() -> None:
    """connect() should fail fast after reconnect is disabled by fatal disconnect."""
    with patch("socket.socket"):
        iface = TCPInterface(hostname="localhost", noProto=True, connectNow=False)
        iface.socket = None
        iface._wantExit = False
        iface._fatal_disconnect = True
        try:
            with (
                patch.object(iface, "myConnect") as mock_connect,
                patch("meshtastic.tcp_interface.StreamInterface.connect") as mock_super,
            ):
                with pytest.raises(
                    ConnectionError,
                    match=r"Cannot connect to localhost: reconnect disabled after fatal disconnect",
                ):
                    iface.connect()
            mock_connect.assert_not_called()
            mock_super.assert_not_called()
        finally:
            iface.close()


@pytest.mark.unit
def test_TCPInterface_attempt_reconnect_reader_thread_clears_queue() -> None:
    """Ensure reader-thread reconnect clears queued packets before _start_config().

    This locks in the deadlock-avoidance behavior documented in _attempt_reconnect:
    when reconnect runs on the reader thread, pending packets are dropped so
    _start_config() cannot block waiting on queue progress that depends on the same
    thread.
    """
    with patch("socket.socket"):
        iface = TCPInterface(hostname="localhost", noProto=True, connectNow=False)
        try:
            iface._rxThread = threading.current_thread()
            iface.queue[1] = True
            iface.queue[2] = True
            mock_socket = MagicMock()

            with (
                patch.object(iface, "myConnect") as mock_connect,
                patch.object(iface, "_start_config") as mock_start_config,
            ):

                def _connect_side_effect() -> None:
                    iface.socket = mock_socket

                def _start_config_side_effect() -> None:
                    assert len(iface.queue) == 0

                mock_connect.side_effect = _connect_side_effect
                mock_start_config.side_effect = _start_config_side_effect

                assert iface._attempt_reconnect() is True

            mock_connect.assert_called_once()
            mock_start_config.assert_called_once()
            assert len(iface.queue) == 0
        finally:
            iface.close()


@pytest.mark.unit
def test_TCPInterface_attempt_reconnect_does_not_wait_connected() -> None:
    """Reconnect should run startup without calling _wait_connected().

    _attempt_reconnect() is used from the background reader thread path, so it
    must not introduce a wait on protocol responses that are processed by that
    same thread.
    """
    with patch("socket.socket"):
        iface = TCPInterface(hostname="localhost", noProto=True, connectNow=False)
        try:
            mock_socket = MagicMock()
            with (
                patch.object(iface, "myConnect") as mock_connect,
                patch.object(iface, "_start_config") as mock_start_config,
                patch.object(iface, "_wait_connected") as mock_wait_connected,
            ):

                def _connect_side_effect() -> None:
                    iface.socket = mock_socket

                mock_connect.side_effect = _connect_side_effect

                assert iface._attempt_reconnect() is True

            mock_connect.assert_called_once()
            mock_start_config.assert_called_once()
            mock_wait_connected.assert_not_called()
        finally:
            iface.close()


@pytest.mark.unit
def test_TCPInterface_compute_reconnect_delay_exponential_backoff() -> None:
    """Test _compute_reconnect_delay implements exponential backoff correctly."""
    with patch("socket.socket"):
        iface = TCPInterface(hostname="localhost", noProto=True, connectNow=False)
        try:
            # Test defaults: base=1.0, backoff=1.6, max=30.0
            # Attempt 1: exponent = max(0, 1-1) = 0, delay = 1.0 * 1.6^0 = 1.0
            iface._reconnect_attempts = 1
            assert iface._compute_reconnect_delay() == 1.0

            # Attempt 2: exponent = max(0, 2-1) = 1, delay = 1.0 * 1.6^1 = 1.6
            iface._reconnect_attempts = 2
            assert iface._compute_reconnect_delay() == 1.6

            # Attempt 3: exponent = 2, delay = 1.0 * 1.6^2 = 2.56
            iface._reconnect_attempts = 3
            assert abs(iface._compute_reconnect_delay() - 2.56) < 0.001

            # Attempt 4: exponent = 3, delay = 1.0 * 1.6^3 = 4.096
            iface._reconnect_attempts = 4
            assert abs(iface._compute_reconnect_delay() - 4.096) < 0.001

            # Attempt 8: should cap at max_delay (30.0)
            iface._reconnect_attempts = 8
            # Without cap: 1.0 * 1.6^7 = 26.8435456
            # With cap: min(30.0, 26.8435456) = 26.8435456
            expected = 1.0 * (1.6**7)
            assert abs(iface._compute_reconnect_delay() - expected) < 0.001

            # Attempt 10: would exceed max_delay without cap
            iface._reconnect_attempts = 10
            # Without cap: 1.0 * 1.6^9 = 68.719476736
            # With cap: min(30.0, 68.719476736) = 30.0
            assert iface._compute_reconnect_delay() == 30.0
        finally:
            iface.close()


@pytest.mark.unit
def test_TCPInterface_compute_reconnect_delay_custom_parameters() -> None:
    """Test _compute_reconnect_delay with custom backoff parameters."""
    with patch("socket.socket"):
        iface = TCPInterface(hostname="localhost", noProto=True, connectNow=False)
        try:
            # Customize parameters
            iface._reconnect_base_delay = 2.0
            iface._reconnect_backoff = 2.0
            iface._reconnect_max_delay = 60.0

            # Attempt 1: exponent = 0, delay = 2.0 * 2.0^0 = 2.0
            iface._reconnect_attempts = 1
            assert iface._compute_reconnect_delay() == 2.0

            # Attempt 2: exponent = 1, delay = 2.0 * 2.0^1 = 4.0
            iface._reconnect_attempts = 2
            assert iface._compute_reconnect_delay() == 4.0

            # Attempt 3: exponent = 2, delay = 2.0 * 2.0^2 = 8.0
            iface._reconnect_attempts = 3
            assert iface._compute_reconnect_delay() == 8.0

            # Attempt 5: exponent = 4, delay = 2.0 * 2.0^4 = 32.0
            iface._reconnect_attempts = 5
            assert iface._compute_reconnect_delay() == 32.0

            # Attempt 6: should cap at max_delay (60.0)
            iface._reconnect_attempts = 6
            # Without cap: 2.0 * 2.0^5 = 64.0
            # With cap: min(60.0, 64.0) = 60.0
            assert iface._compute_reconnect_delay() == 60.0
        finally:
            iface.close()


@pytest.mark.unit
def test_TCPInterface_attempt_reconnect_max_attempts_triggers_fatal() -> None:
    """Test that exceeding max reconnect attempts triggers fatal disconnect."""
    with patch("socket.socket"):
        iface = TCPInterface(hostname="localhost", noProto=True, connectNow=False)
        try:
            # Set up to trigger max attempts (already at max)
            iface._reconnect_attempts = iface._max_reconnect_attempts
            assert iface._fatal_disconnect is False

            result = iface._attempt_reconnect()

            # Should return False and mark fatal disconnect
            assert result is False
            assert iface._fatal_disconnect is True
            assert iface._wantExit is True
        finally:
            iface.close()


@pytest.mark.unit
def test_TCPInterface_sleep_reconnect_delay_interrupted_by_shutdown() -> None:
    """Test that _sleep_reconnect_delay returns False when shutdown is requested."""
    with patch("socket.socket"):
        iface = TCPInterface(hostname="localhost", noProto=True, connectNow=False)
        try:
            iface._wantExit = False
            iface._fatal_disconnect = False

            # Start a delay and trigger shutdown during it
            def trigger_shutdown_during_sleep() -> None:
                time.sleep(0.1)  # Small delay before triggering
                iface._wantExit = True

            thread = threading.Thread(target=trigger_shutdown_during_sleep)
            thread.start()

            # Should return False when interrupted
            result = iface._sleep_reconnect_delay(10.0)
            thread.join(timeout=1.0)
            assert (
                not thread.is_alive()
            ), "Thread should complete after _wantExit is set"

            assert result is False
        finally:
            iface.close()


@pytest.mark.unit
def test_TCPInterface_sleep_reconnect_delay_interrupted_by_fatal_disconnect() -> None:
    """Test that _sleep_reconnect_delay returns False on fatal disconnect."""
    with patch("socket.socket"):
        iface = TCPInterface(hostname="localhost", noProto=True, connectNow=False)
        try:
            iface._wantExit = False
            iface._fatal_disconnect = False

            # Start a delay and trigger fatal disconnect during it
            def trigger_fatal_during_sleep() -> None:
                time.sleep(0.1)  # Small delay before triggering
                iface._fatal_disconnect = True

            thread = threading.Thread(target=trigger_fatal_during_sleep)
            thread.start()

            # Should return False when interrupted
            result = iface._sleep_reconnect_delay(10.0)
            thread.join(timeout=1.0)
            assert (
                not thread.is_alive()
            ), "Thread should complete after _fatal_disconnect is set"

            assert result is False
        finally:
            iface.close()


@pytest.mark.unit
def test_TCPInterface_sleep_reconnect_delay_completes_full_delay() -> None:
    """Test that _sleep_reconnect_delay returns True when delay completes."""
    with patch("socket.socket"):
        iface = TCPInterface(hostname="localhost", noProto=True, connectNow=False)
        try:
            iface._wantExit = False
            iface._fatal_disconnect = False

            # Small delay should complete
            result = iface._sleep_reconnect_delay(0.3)

            assert result is True
        finally:
            iface.close()


@pytest.mark.unit
def test_TCPInterface_attempt_reconnect_already_in_progress() -> None:
    """Test that _attempt_reconnect returns False if already in progress."""
    with patch("socket.socket"):
        iface = TCPInterface(hostname="localhost", noProto=True, connectNow=False)
        try:
            # Mark reconnect as in progress
            iface._reconnect_attempt_in_progress = True

            result = iface._attempt_reconnect()

            # Should return False without attempting
            assert result is False
        finally:
            iface.close()


@pytest.mark.unit
def test_TCPInterface_attempt_reconnect_aborted_by_shutdown() -> None:
    """Test that _attempt_reconnect returns False when shutdown is requested."""
    with patch("socket.socket"):
        iface = TCPInterface(hostname="localhost", noProto=True, connectNow=False)
        try:
            iface._wantExit = True
            iface._reconnect_attempt_in_progress = False

            result = iface._attempt_reconnect()

            # Should return False without attempting
            assert result is False
        finally:
            iface.close()


@pytest.mark.unit
def test_TCPInterface_attempt_reconnect_aborted_by_fatal_disconnect() -> None:
    """Test that _attempt_reconnect returns False on fatal disconnect."""
    with patch("socket.socket"):
        iface = TCPInterface(hostname="localhost", noProto=True, connectNow=False)
        try:
            iface._wantExit = False
            iface._fatal_disconnect = True
            iface._reconnect_attempt_in_progress = False

            result = iface._attempt_reconnect()

            # Should return False without attempting
            assert result is False
        finally:
            iface.close()


@pytest.mark.unit
def test_TCPInterface_on_fatal_disconnect_idempotent() -> None:
    """Test that _on_fatal_disconnect is idempotent."""
    with patch("socket.socket"):
        iface = TCPInterface(hostname="localhost", noProto=True, connectNow=False)
        try:
            iface._fatal_disconnect = False
            iface._wantExit = False
            iface._reconnect_attempts = 5

            # First call should set flags
            iface._on_fatal_disconnect("test reason")
            assert iface._fatal_disconnect is True
            assert iface._wantExit is True

            # Second call should be no-op
            iface._on_fatal_disconnect("another reason")
            assert iface._fatal_disconnect is True
            assert iface._wantExit is True
        finally:
            iface.close()
