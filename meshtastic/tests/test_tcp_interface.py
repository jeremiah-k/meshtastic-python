"""Meshtastic unit tests for tcp_interface.py."""

import re
from unittest.mock import MagicMock, patch

import pytest

from ..protobuf import config_pb2
from ..tcp_interface import TCPInterface


@pytest.mark.unit
def test_TCPInterface(capsys):
    """Test that we can instantiate a TCPInterface.

    Parameters
    ----------
    capsys : _type_
        _description_
    """
    with patch("socket.socket") as mock_socket:
        iface = TCPInterface(hostname="localhost", noProto=True)
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
        iface.close()


@pytest.mark.unit
def test_TCPInterface_exception():
    """Test that we can instantiate a TCPInterface.

    Raises
    ------
    ValueError
        _description_
    """

    def throw_an_exception():
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
def test_TCPInterface_without_connecting():
    """Test that we can instantiate a TCPInterface with connectNow as false."""
    with patch("socket.socket"):
        iface = TCPInterface(hostname="localhost", noProto=True, connectNow=False)
        assert iface.socket is None


@pytest.mark.unit
def test_TCPInterface_write_uses_sendall():
    """Test that _writeBytes uses sendall to avoid partial writes."""
    with patch("socket.socket"):
        iface = TCPInterface(hostname="localhost", noProto=True, connectNow=False)
        mock_socket = MagicMock()
        iface.socket = mock_socket

        iface._writeBytes(b"abc")

        mock_socket.sendall.assert_called_once_with(b"abc")
        iface.close()


@pytest.mark.unit
def test_TCPInterface_read_empty_does_not_reconnect_when_closing():
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
        iface.close()
