"""Meshtastic unit tests for stream_interface.py."""

import logging
from unittest.mock import MagicMock, patch

import pytest

from ..stream_interface import START1, START2, StreamInterface

# import re


@pytest.mark.unit
def test_StreamInterface():
    """Verify that creating a StreamInterface without protocol configuration raises an error.

    Raises
    ------
    StreamInterface.StreamInterfaceError
        when a StreamInterface is instantiated without a protocol.
    """
    with pytest.raises(StreamInterface.StreamInterfaceError):
        StreamInterface()


# Note: This takes a bit, so moving from unit to slow
@pytest.mark.unitslow
@pytest.mark.usefixtures("reset_mt_config")
def test_StreamInterface_with_noProto(caplog):
    """Verify that a StreamInterface can be instantiated with noProto and, when assigned a stream, can write and read bytes to and from that stream.

    Parameters
    ----------
    caplog : _type_
        _description_
    """
    stream = MagicMock()
    test_data = b"hello"
    stream.read.return_value = test_data
    with caplog.at_level(logging.DEBUG):
        iface = StreamInterface(noProto=True, connectNow=False)
        try:
            iface.stream = stream
            iface._writeBytes(test_data)
            data = iface._readBytes(len(test_data))
            assert data == test_data
        finally:
            iface.close()


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_sendToRadioImpl_frames_payload() -> None:
    """Test that _sendToRadioImpl writes a properly framed payload."""
    iface = StreamInterface(noProto=True, connectNow=False)
    try:
        to_radio = MagicMock()
        to_radio.SerializeToString.return_value = b"hello"

        with patch.object(iface, "_writeBytes") as write_bytes:
            iface._sendToRadioImpl(to_radio)

            expected = bytes([START1, START2, 0x00, 0x05]) + b"hello"
            write_bytes.assert_called_once_with(expected)
    finally:
        iface.close()
