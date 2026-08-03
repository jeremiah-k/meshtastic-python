"""Regression tests for serial recovery from malformed bootstrap frames."""

import threading
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from google.protobuf.message import DecodeError

from meshtastic.mesh_interface import MeshInterface
from meshtastic.mesh_interface_runtime.receive_pipeline import ReceivePipeline
from meshtastic.serial_interface import (
    SERIAL_BOOTSTRAP_DECODE_ERROR_RETRY_THRESHOLD,
    SerialInterface,
)
from meshtastic.stream_interface import StreamInterface


@pytest.mark.unit
def test_receive_pipeline_records_malformed_bootstrap_frame() -> None:
    with MeshInterface(noProto=True) as interface:
        pipeline = ReceivePipeline(interface)

        with pytest.raises(DecodeError):
            pipeline._parse_from_radio_bytes(b"\x80")  # noqa: SLF001

        assert interface._bootstrap_decode_error_count_snapshot() == 1  # noqa: SLF001


@pytest.mark.unit
def test_serial_wait_aborts_after_repeated_bootstrap_decode_errors() -> None:
    interface = object.__new__(SerialInterface)
    interface._wantExit = False
    interface.stream = SimpleNamespace(is_open=True)
    interface._rxThread = MagicMock()
    interface._rxThread.is_alive.return_value = True
    interface._bootstrap_decode_error_count_snapshot = MagicMock(
        return_value=SERIAL_BOOTSTRAP_DECODE_ERROR_RETRY_THRESHOLD
    )

    reason = interface._connect_wait_should_abort()

    assert reason is not None
    assert "Corrupt protocol frames during serial connection bootstrap" in reason


@pytest.mark.unit
def test_serial_wait_prioritizes_transport_shutdown_over_decode_retry() -> None:
    interface = object.__new__(SerialInterface)
    interface._wantExit = True
    interface.stream = SimpleNamespace(is_open=True)
    interface._rxThread = MagicMock()
    interface._rxThread.is_alive.return_value = True
    interface._bootstrap_decode_error_count_snapshot = MagicMock(
        return_value=SERIAL_BOOTSTRAP_DECODE_ERROR_RETRY_THRESHOLD
    )

    assert (
        interface._connect_wait_should_abort()
        == "Connection cancelled while waiting for completion"
    )
    interface._bootstrap_decode_error_count_snapshot.assert_not_called()


@pytest.mark.unit
def test_serial_connect_retries_bootstrap_decode_failure() -> None:
    interface = object.__new__(SerialInterface)
    interface.devPath = "/dev/ttyUSB0"
    interface._dev_path_auto_detected = False
    interface._connect_lock = threading.Lock()
    stream = MagicMock()
    stream.is_open = True
    interface.stream = stream
    transient_error = MeshInterface.MeshInterfaceError(
        "Corrupt protocol frames during serial connection bootstrap (2 decode errors)"
    )

    with (
        patch.object(
            StreamInterface, "connect", side_effect=[transient_error, None]
        ) as connect,
        patch.object(interface, "_open_serial_stream", return_value=stream),
        patch("meshtastic.serial_interface.time.sleep") as sleep,
    ):
        interface.connect()

    assert connect.call_count == 2
    stream.close.assert_called_once_with()
    sleep.assert_called_once()


@pytest.mark.unit
def test_prepare_for_connect_clears_previous_decode_failures() -> None:
    with MeshInterface(noProto=True) as interface:
        for _ in range(4):
            interface._record_bootstrap_decode_error()  # noqa: SLF001

        interface._prepare_for_connect()  # noqa: SLF001

        assert interface._bootstrap_decode_error_count_snapshot() == 0  # noqa: SLF001
