"""Meshtastic unit tests for tcp_interface.py."""

import re
import threading
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

    def throw_an_exception() -> None:
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
def test_TCPInterface_without_connecting() -> None:
    """Test that we can instantiate a TCPInterface with connectNow as false."""
    with patch("socket.socket"):
        iface = TCPInterface(hostname="localhost", noProto=True, connectNow=False)
        assert iface.socket is None


@pytest.mark.unit
def test_TCPInterface_write_uses_sendall() -> None:
    """Test that _writeBytes uses sendall to avoid partial writes."""
    with patch("socket.socket"):
        iface = TCPInterface(hostname="localhost", noProto=True, connectNow=False)
        mock_socket = MagicMock()
        iface.socket = mock_socket

        iface._writeBytes(b"abc")

        mock_socket.sendall.assert_called_once_with(b"abc")
        iface.close()


@pytest.mark.unit
def test_TCPInterface_read_empty_does_not_reconnect_when_closing() -> None:
    """Test that _readBytes avoids reconnect attempts during intentional shutdown."""
    with patch("socket.socket"):
        iface = TCPInterface(hostname="localhost", noProto=True, connectNow=False)
        mock_socket = MagicMock()
        mock_socket.recv.return_value = b""
        iface.socket = mock_socket
        iface._wantExit = True

        with (
            patch.object(iface, "myConnect") as mock_connect,
            patch.object(iface, "_startConfig") as mock_start_config,
            patch("meshtastic.tcp_interface.time.sleep") as mock_sleep,
        ):
            data = iface._readBytes(1)

        assert data is None
        mock_connect.assert_not_called()
        mock_start_config.assert_not_called()
        mock_sleep.assert_not_called()
        mock_socket.close.assert_called_once()
        assert iface.socket is None
        iface.close()


@pytest.mark.unit
def test_TCPInterface_attempt_reconnect_reader_thread_clears_queue() -> None:
    """Ensure reader-thread reconnect clears queued packets before _startConfig().

    This locks in the deadlock-avoidance behavior documented in _attempt_reconnect:
    when reconnect runs on the reader thread, pending packets are dropped so
    _startConfig() cannot block waiting on queue progress that depends on the same
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
                patch.object(iface, "_startConfig") as mock_start_config,
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
    """Reconnect should run startup without calling _waitConnected().

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
                patch.object(iface, "_startConfig") as mock_start_config,
                patch.object(iface, "_waitConnected") as mock_wait_connected,
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
