"""Meshtastic unit tests for node.py."""

import logging
import threading
from collections.abc import Callable
from typing import Any, cast
from unittest.mock import MagicMock

import pytest
from pytest import CaptureFixture, LogCaptureFixture

from .. import node as node_module
from ..mesh_interface import MeshInterface
from ..node import Node
from ..protobuf import (
    admin_pb2,
    config_pb2,
    mesh_pb2,
)
from ..util import Acknowledgment

from ._node_legacy_support import (
    _MetadataLockProbeIface,
    _TrackingLock,
)


@pytest.mark.unit
def test_onRequestGetMetadata_handles_routing_error_and_ack_only(
    autospec_local_node_iface: Callable[[type[Any]], MagicMock],
) -> None:
    """OnRequestGetMetadata should NAK on routing error and avoid recursive retries."""
    iface = autospec_local_node_iface(MeshInterface)
    iface._acknowledgment = Acknowledgment()
    anode = Node(iface, "!12345678", noProto=True)
    anode.getMetadata = MagicMock()  # type: ignore[method-assign]

    anode.onRequestGetMetadata(
        {"decoded": {"portnum": "ROUTING_APP", "routing": {"errorReason": "NO_PATH"}}}
    )
    assert iface._acknowledgment.receivedNak is True

    iface._acknowledgment = Acknowledgment()
    anode.onRequestGetMetadata(
        {"decoded": {"portnum": "ROUTING_APP", "routing": {"errorReason": "NONE"}}}
    )
    anode.getMetadata.assert_not_called()


@pytest.mark.unit
def test_onRequestGetMetadata_handles_non_routing_error_reason(
    autospec_local_node_iface: Callable[[type[Any]], MagicMock],
) -> None:
    """OnRequestGetMetadata should mark NAK for decoded routing errors outside ROUTING_APP."""
    iface = autospec_local_node_iface(MeshInterface)
    iface._acknowledgment = Acknowledgment()
    anode = Node(iface, "!12345678", noProto=True)

    anode.onRequestGetMetadata(
        {
            "decoded": {
                "portnum": "ADMIN_APP",
                "routing": {"errorReason": "TIMEOUT"},
            }
        }
    )

    assert iface._acknowledgment.receivedNak is True


@pytest.mark.unit
def test_onRequestGetMetadata_logs_valid_and_fallback_enum_values(
    autospec_local_node_iface: Callable[[type[Any]], MagicMock],
    caplog: LogCaptureFixture,
) -> None:
    """OnRequestGetMetadata should handle both valid and unknown enum values."""
    iface = autospec_local_node_iface(MeshInterface)
    iface._acknowledgment = Acknowledgment()
    anode = Node(iface, "!12345678", noProto=True)
    anode._timeout = MagicMock()

    valid_raw = admin_pb2.AdminMessage()
    valid_resp = valid_raw.get_device_metadata_response
    valid_resp.firmware_version = "fw"
    valid_resp.device_state_version = 1
    valid_resp.role = config_pb2.Config.DeviceConfig.Role.CLIENT
    valid_resp.position_flags = 0
    valid_resp.hw_model = mesh_pb2.HardwareModel.TBEAM
    valid_resp.hasPKC = True
    with caplog.at_level(logging.INFO):
        anode.onRequestGetMetadata(
            {"decoded": {"portnum": "ADMIN_APP", "admin": {"raw": valid_raw}}}
        )
    assert iface._acknowledgment.receivedAck is True
    assert iface.metadata.firmware_version == "fw"
    anode._timeout.reset.assert_called()

    iface._acknowledgment = Acknowledgment()
    unknown_raw = admin_pb2.AdminMessage()
    unknown_resp = unknown_raw.get_device_metadata_response
    unknown_resp.firmware_version = "fw2"
    unknown_resp.device_state_version = 2
    unknown_resp.role = cast(config_pb2.Config.DeviceConfig.Role.ValueType, 999)
    unknown_resp.position_flags = 0
    unknown_resp.hw_model = cast(mesh_pb2.HardwareModel.ValueType, 999)
    unknown_resp.hasPKC = False
    unknown_resp.excluded_modules = 1
    anode.onRequestGetMetadata(
        {"decoded": {"portnum": "ADMIN_APP", "admin": {"raw": unknown_raw}}}
    )
    assert iface._acknowledgment.receivedAck is True
    assert iface.metadata.firmware_version == "fw2"


@pytest.mark.unit
def test_onRequestGetMetadata_updates_metadata_under_node_db_lock() -> None:
    """OnRequestGetMetadata should update iface.metadata while holding iface._node_db_lock."""
    lock = _TrackingLock()
    iface = _MetadataLockProbeIface(lock, include_acknowledgment=True)
    anode = Node(cast(Any, iface), "!12345678", noProto=True)
    anode._timeout = MagicMock()

    raw = admin_pb2.AdminMessage()
    response = raw.get_device_metadata_response
    response.firmware_version = "2.7.19"
    response.device_state_version = 25
    response.role = config_pb2.Config.DeviceConfig.Role.CLIENT
    response.position_flags = 0
    response.hw_model = mesh_pb2.HardwareModel.PORTDUINO
    response.hasPKC = True

    anode.onRequestGetMetadata(
        {"decoded": {"portnum": "ADMIN_APP", "admin": {"raw": raw}}}
    )

    assert lock.enter_count == 1
    assert lock.is_held is False
    assert iface.metadata_assignment_lock_state is True
    metadata = iface.metadata
    assert metadata is not None
    assert metadata.firmware_version == "2.7.19"


@pytest.mark.unit
def test_set_metadata_snapshot_stores_detached_copy_under_lock() -> None:
    """_set_metadata_snapshot should store a detached metadata copy while holding node DB lock."""
    lock = _TrackingLock()
    iface = _MetadataLockProbeIface(lock)
    anode = Node(cast(Any, iface), "!12345678", noProto=True)
    metadata_snapshot = mesh_pb2.DeviceMetadata(
        firmware_version="2.7.19",
        device_state_version=25,
    )

    anode._set_metadata_snapshot(metadata_snapshot)

    assert lock.enter_count == 1
    assert lock.is_held is False
    assert iface.metadata_assignment_lock_state is True
    metadata = iface.metadata
    assert isinstance(metadata, mesh_pb2.DeviceMetadata)
    assert metadata is not metadata_snapshot
    assert metadata.firmware_version == "2.7.19"
    metadata_snapshot.firmware_version = "mutated-locally"
    assert metadata.firmware_version == "2.7.19"


@pytest.mark.unit
def test_onRequestGetMetadata_emits_stdout_when_redirected(
    autospec_local_node_iface: Callable[[type[Any]], MagicMock],
    capsys: CaptureFixture[str],
) -> None:
    """Metadata response should still emit stdout lines for legacy redirect parsers."""
    iface = autospec_local_node_iface(MeshInterface)
    iface._acknowledgment = Acknowledgment()
    anode = Node(iface, "!12345678", noProto=True)
    anode._timeout = MagicMock()

    raw = admin_pb2.AdminMessage()
    resp = raw.get_device_metadata_response
    resp.firmware_version = "2.7.18"
    resp.device_state_version = 24
    resp.role = config_pb2.Config.DeviceConfig.Role.CLIENT
    resp.position_flags = 0
    resp.hw_model = mesh_pb2.HardwareModel.PORTDUINO
    resp.hasPKC = True

    anode.onRequestGetMetadata(
        {"decoded": {"portnum": "ADMIN_APP", "admin": {"raw": raw}}}
    )

    out, _err = capsys.readouterr()
    assert "firmware_version: 2.7.18" in out


@pytest.mark.unit
def test_emit_cached_metadata_returns_false_without_firmware_version(
    autospec_local_node_iface: Callable[[type[Any]], MagicMock],
) -> None:
    """_emit_cached_metadata_for_stdout should return False when firmware_version is missing."""
    iface = autospec_local_node_iface(MeshInterface)
    iface.metadata = mesh_pb2.DeviceMetadata()
    anode = Node(iface, "!12345678", noProto=True)

    assert anode._emit_cached_metadata_for_stdout() is False


@pytest.mark.unit
def test_emit_cached_metadata_uses_fallback_values_for_unknown_enums(
    autospec_local_node_iface: Callable[[type[Any]], MagicMock],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """_emit_cached_metadata_for_stdout should emit numeric fallback values for unknown enum members."""
    iface = autospec_local_node_iface(MeshInterface)
    iface.metadata = mesh_pb2.DeviceMetadata(
        firmware_version="2.7.18",
        device_state_version=24,
        role=cast(config_pb2.Config.DeviceConfig.Role.ValueType, 999),
        position_flags=0,
        hw_model=cast(mesh_pb2.HardwareModel.ValueType, 999),
        hasPKC=False,
        excluded_modules=1,
    )
    anode = Node(iface, "!12345678", noProto=True)
    emitted: list[str] = []
    monkeypatch.setattr(anode, "_emit_metadata_line", emitted.append)

    assert anode._emit_cached_metadata_for_stdout() is True
    assert "role: 999" in emitted
    assert "hw_model: 999" in emitted
    assert any(line.startswith("excluded_modules:") for line in emitted)


@pytest.mark.unit
def test_emit_cached_metadata_reads_metadata_under_node_db_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """_emit_cached_metadata_for_stdout should emit snapshot lines after lock release."""
    original_metadata = mesh_pb2.DeviceMetadata(
        firmware_version="2.7.18",
        device_state_version=24,
        role=config_pb2.Config.DeviceConfig.Role.CLIENT,
        position_flags=0,
        hw_model=mesh_pb2.HardwareModel.PORTDUINO,
        hasPKC=True,
    )

    metadata_read_lock_states: list[bool] = []

    def _mutate_metadata_after_unlock() -> None:
        original_metadata.firmware_version = "mutated-after-unlock"
        original_metadata.device_state_version = 99
        original_metadata.role = config_pb2.Config.DeviceConfig.Role.CLIENT_HIDDEN
        original_metadata.hw_model = mesh_pb2.HardwareModel.UNSET
        original_metadata.hasPKC = False

    lock = _TrackingLock(on_exit=_mutate_metadata_after_unlock)
    iface = _MetadataLockProbeIface(
        lock,
        metadata=original_metadata,
        metadata_read_lock_states=metadata_read_lock_states,
    )
    anode = Node(cast(Any, iface), "!12345678", noProto=True)
    emitted: list[tuple[str, bool]] = []

    def _record_emit(line: str) -> None:
        emitted.append((line, iface._node_db_lock.is_held))

    monkeypatch.setattr(anode, "_emit_metadata_line", _record_emit)

    assert anode._emit_cached_metadata_for_stdout() is True
    assert emitted
    assert metadata_read_lock_states
    assert any(metadata_read_lock_states)
    assert iface._node_db_lock.enter_count == 1
    assert all(not is_held for _line, is_held in emitted)
    assert any("firmware_version: 2.7.18" in line for line, _ in emitted)
    assert not any(
        "firmware_version: mutated-after-unlock" in line for line, _ in emitted
    )
    assert iface.metadata is original_metadata
    assert iface.metadata is not None  # type narrowing for LSP
    assert iface.metadata.firmware_version == "mutated-after-unlock"


@pytest.mark.unit
def test_get_metadata_snapshot_returns_none_for_non_proto_metadata(
    autospec_local_node_iface: Callable[[type[Any]], MagicMock],
) -> None:
    """_get_metadata_snapshot should return None when iface.metadata is not DeviceMetadata."""
    iface = autospec_local_node_iface(MeshInterface)
    iface._node_db_lock = _TrackingLock()
    iface.metadata = {"firmware_version": "not-a-protobuf"}
    anode = Node(iface, "!12345678", noProto=True)

    assert anode._get_metadata_snapshot() is None


@pytest.mark.unit
def test_getMetadata_waits_for_redirected_stdout_callback_output(
    autospec_local_node_iface: Callable[[type[Any]], MagicMock],
    capsys: CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """GetMetadata should keep redirected stdout active until metadata callback emits."""
    iface = autospec_local_node_iface(MeshInterface)
    iface._acknowledgment = Acknowledgment()
    iface.waitForAckNak = MagicMock()
    anode = Node(iface, "!12345678", noProto=True)
    anode._emit_cached_metadata_for_stdout = MagicMock(return_value=True)  # type: ignore[method-assign]
    monkeypatch.setattr(node_module, "METADATA_STDOUT_COMPAT_WAIT_SECONDS", 0.5)
    timers: list[threading.Timer] = []

    def _fake_send_admin(
        _msg: admin_pb2.AdminMessage,
        wantResponse: bool = False,
        onResponse: Callable[[dict[str, Any]], Any] | None = None,
        adminIndex: int = 0,
    ) -> None:
        _ = (wantResponse, adminIndex)
        assert onResponse is not None
        raw = admin_pb2.AdminMessage()
        response = raw.get_device_metadata_response
        response.firmware_version = "2.7.18"
        response.device_state_version = 24
        response.role = config_pb2.Config.DeviceConfig.Role.CLIENT
        response.position_flags = 0
        response.hw_model = mesh_pb2.HardwareModel.PORTDUINO
        response.hasPKC = True

        timer = threading.Timer(
            0.05,
            lambda: onResponse(
                {
                    "decoded": {
                        "portnum": "ADMIN_APP",
                        "admin": {"raw": raw},
                    }
                }
            ),
        )
        timer.daemon = True
        timer.start()
        timers.append(timer)

    anode._send_admin = _fake_send_admin  # type: ignore[assignment]
    anode.getMetadata()
    for timer in timers:
        timer.join(timeout=1.0)
        assert not timer.is_alive()

    out, _err = capsys.readouterr()
    assert "firmware_version: 2.7.18" in out
    anode._emit_cached_metadata_for_stdout.assert_not_called()


@pytest.mark.unit
def test_getMetadata_emits_cached_metadata_when_callback_never_arrives(
    autospec_local_node_iface: Callable[[type[Any]], MagicMock],
    capsys: CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """GetMetadata should emit cached interface metadata for redirected stdout parsers."""
    iface = autospec_local_node_iface(MeshInterface)
    iface._acknowledgment = Acknowledgment()
    iface.waitForAckNak = MagicMock()
    iface.metadata = mesh_pb2.DeviceMetadata(
        firmware_version="2.7.18",
        device_state_version=24,
        role=config_pb2.Config.DeviceConfig.Role.CLIENT_MUTE,
        position_flags=0,
        hw_model=mesh_pb2.HardwareModel.PORTDUINO,
        hasPKC=True,
    )
    anode = Node(iface, "!12345678", noProto=True)

    monkeypatch.setattr(node_module, "METADATA_STDOUT_COMPAT_WAIT_SECONDS", 0.01)

    def _fake_send_admin(
        _msg: admin_pb2.AdminMessage,
        wantResponse: bool = False,
        onResponse: Callable[[dict[str, Any]], Any] | None = None,
        adminIndex: int = 0,
    ) -> None:
        _ = (wantResponse, onResponse, adminIndex)

    anode._send_admin = _fake_send_admin  # type: ignore[assignment]
    anode.getMetadata()

    out, _err = capsys.readouterr()
    assert "firmware_version: 2.7.18" in out


@pytest.mark.unit
def test_on_response_request_settings_warns_for_unrecognized_payload_shape(
    mock_serial_interface: MagicMock,
    caplog: LogCaptureFixture,
) -> None:
    """OnResponseRequestSettings should warn and return for unsupported response payloads."""
    anode = Node(mock_serial_interface, "!12345678", noProto=True)
    anode.iface._acknowledgment = Acknowledgment()

    with caplog.at_level(logging.WARNING):
        anode.onResponseRequestSettings(
            {"decoded": {"admin": {"raw": admin_pb2.AdminMessage()}}}
        )

    assert "Did not receive a valid config response" in caplog.text
