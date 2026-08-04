"""Request-scoped ACK/NAK behavior for remote admin operations."""

from __future__ import annotations

import threading
import time
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import MagicMock

import pytest

from meshtastic.mesh_interface import MeshInterface
from meshtastic.node_runtime.admin_wait import (
    WAIT_ATTR_NAK,
    _accepts_response_wait_attr,
    _extract_request_id_from_response,
    _extract_request_id_from_sent_packet,
    _set_admin_wait_error,
    _wait_for_admin_ack,
)
from meshtastic.node_runtime.settings_runtime.admin import _NodeAdminCommandRuntime
from meshtastic.node_runtime.settings_runtime.response import (
    _NodeSettingsResponseRuntime,
)
from meshtastic.protobuf import admin_pb2, config_pb2, localonly_pb2, mesh_pb2
from meshtastic.util import Acknowledgment


class _ScopedIfaceDouble:
    """Minimal interface double exposing a real bound scoped-wait helper."""

    def __init__(self) -> None:
        self.localNode = object()
        self.scoped_waits: list[int] = []
        self.legacy_waits = 0
        self._active_wait_request_ids: dict[str, set[int]] = {}

    @staticmethod
    def _extract_request_id_from_sent_packet(packet: object) -> int | None:
        request_id = getattr(packet, "id", None)
        return request_id if isinstance(request_id, int) and request_id > 0 else None

    def _has_active_wait_request(
        self, acknowledgment_attr: str, request_id: int
    ) -> bool:
        active = self._active_wait_request_ids.get(acknowledgment_attr)
        return active is not None and request_id in active

    def _wait_for_ack_nak(self, request_id: int) -> None:
        self.scoped_waits.append(request_id)
        active = self._active_wait_request_ids.get(WAIT_ATTR_NAK)
        if active is not None:
            active.discard(request_id)
            if not active:
                self._active_wait_request_ids.pop(WAIT_ATTR_NAK, None)

    def _send_data_with_wait(self, *_args: object, **_kwargs: object) -> None:
        """Marker for the real-interface pre-registration capability."""

    def waitForAckNak(self) -> None:  # noqa: N802 - compatibility surface
        self.legacy_waits += 1


class _AdminNodeDoubleBase:
    """Shared state for modern and legacy bound admin sender doubles."""

    def __init__(self) -> None:
        self.iface = _ScopedIfaceDouble()
        self.sent_response_wait_attrs: list[str | None] = []
        self.session_key_checks = 0

    def ensureSessionKey(self) -> None:  # noqa: N802 - compatibility surface
        self.session_key_checks += 1

    def onAckNak(self, _packet: dict[str, Any]) -> None:  # noqa: N802
        """ACK callback placeholder used only to select remote wait policy."""


class _RemoteAdminNodeDouble(_AdminNodeDoubleBase):
    """Minimal node double with the current bound ``_send_admin`` signature."""

    def _send_admin(
        self,
        _message: admin_pb2.AdminMessage,
        *,
        onResponse: Any = None,  # noqa: N803 - compatibility surface
        responseWaitAttr: str | None = None,  # noqa: N803 - compatibility surface
    ) -> mesh_pb2.MeshPacket:
        _ = onResponse
        self.sent_response_wait_attrs.append(responseWaitAttr)
        if responseWaitAttr is not None:
            self.iface._active_wait_request_ids.setdefault(responseWaitAttr, set()).add(731)
        return mesh_pb2.MeshPacket(id=731)


def _wait_until(predicate: Any, *, timeout: float = 1.0) -> None:
    """Wait until a test predicate becomes true or fail deterministically."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.001)
    pytest.fail("timed out waiting for test condition")


@pytest.mark.unit
def test_remote_admin_command_registers_and_uses_request_scoped_ack_wait() -> None:
    """Remote admin commands should bind their wait to the packet they sent."""
    node = _RemoteAdminNodeDouble()
    runtime = _NodeAdminCommandRuntime(node)  # type: ignore[arg-type]

    request = runtime._send_command(  # noqa: SLF001 - runtime contract under test
        admin_pb2.AdminMessage(reboot_seconds=1),
        ensure_session_key=True,
        use_remote_ack_callback=True,
    )

    assert request is not None
    assert request.id == 731
    assert node.session_key_checks == 1
    assert node.sent_response_wait_attrs == [WAIT_ATTR_NAK]
    assert node.iface.scoped_waits == [731]
    assert node.iface.legacy_waits == 0


class _LegacyBoundAdminNodeDouble(_AdminNodeDoubleBase):
    """Bound admin sender that preserves the pre-scope private signature."""

    def _send_admin(
        self,
        _message: admin_pb2.AdminMessage,
        *,
        onResponse: Any = None,  # noqa: N803 - compatibility surface
    ) -> mesh_pb2.MeshPacket:
        _ = onResponse
        self.sent_response_wait_attrs.append(None)
        return mesh_pb2.MeshPacket(id=732)


@pytest.mark.unit
def test_bound_legacy_admin_sender_falls_back_without_private_scope_keyword() -> None:
    """Bound compatibility overrides need not accept the new private keyword."""
    node = _LegacyBoundAdminNodeDouble()
    runtime = _NodeAdminCommandRuntime(node)  # type: ignore[arg-type]

    request = runtime._send_command(  # noqa: SLF001 - runtime contract under test
        admin_pb2.AdminMessage(reboot_seconds=1),
        ensure_session_key=False,
        use_remote_ack_callback=True,
    )

    assert request is not None
    assert request.id == 732
    assert node.sent_response_wait_attrs == [None]
    assert node.iface.scoped_waits == []
    assert node.iface.legacy_waits == 1
    assert node.iface._active_wait_request_ids == {}


@pytest.mark.unit
def test_admin_wait_falls_back_to_legacy_interface_contract() -> None:
    """Minimal interfaces without a real scoped helper should retain legacy waits."""
    iface = SimpleNamespace(waitForAckNak=MagicMock())
    node = SimpleNamespace(iface=iface)

    _wait_for_admin_ack(
        cast(Any, node),
        mesh_pb2.MeshPacket(id=99),
    )

    iface.waitForAckNak.assert_called_once_with()


@pytest.mark.unit
def test_active_wait_scope_query_tracks_registration_and_retirement() -> None:
    """Active-wait membership should be queried through the lock-owning runtime."""
    with MeshInterface(noProto=True) as iface:
        request_id = 100
        iface._clear_wait_error(WAIT_ATTR_NAK, request_id=request_id)  # noqa: SLF001

        assert iface._has_active_wait_request(WAIT_ATTR_NAK, request_id)  # noqa: SLF001

        iface._retire_wait_request(WAIT_ATTR_NAK, request_id=request_id)  # noqa: SLF001
        assert not iface._has_active_wait_request(WAIT_ATTR_NAK, request_id)  # noqa: SLF001


@pytest.mark.unit
def test_request_scoped_admin_waits_do_not_crosstalk() -> None:
    """An ACK for one request must not release a different admin waiter."""
    with MeshInterface(noProto=True) as iface:
        iface._timeout.expireTimeout = 30.0  # noqa: SLF001
        iface._timeout.sleepInterval = 0.001  # noqa: SLF001
        request_a = 101
        request_b = 202
        iface._clear_wait_error(WAIT_ATTR_NAK, request_id=request_a)  # noqa: SLF001
        iface._clear_wait_error(WAIT_ATTR_NAK, request_id=request_b)  # noqa: SLF001

        completed: list[int] = []
        errors: list[BaseException] = []
        lock = threading.Lock()

        def _wait(request_id: int) -> None:
            try:
                iface._wait_for_ack_nak(request_id)  # noqa: SLF001
                with lock:
                    completed.append(request_id)
            except BaseException as exc:  # noqa: BLE001 - captured for test assertion
                with lock:
                    errors.append(exc)

        thread_a = threading.Thread(target=_wait, args=(request_a,), daemon=True)
        thread_b = threading.Thread(target=_wait, args=(request_b,), daemon=True)
        thread_a.start()
        thread_b.start()

        iface._mark_wait_acknowledged(WAIT_ATTR_NAK, request_id=request_a)  # noqa: SLF001
        _wait_until(lambda: request_a in completed)
        assert request_b not in completed
        assert thread_b.is_alive()

        iface._mark_wait_acknowledged(WAIT_ATTR_NAK, request_id=request_b)  # noqa: SLF001
        thread_a.join(timeout=1.0)
        thread_b.join(timeout=1.0)

        assert not thread_a.is_alive()
        assert not thread_b.is_alive()
        assert errors == []
        assert sorted(completed) == [request_a, request_b]
        assert WAIT_ATTR_NAK not in iface._active_wait_request_ids  # noqa: SLF001


@pytest.mark.unit
def test_scoped_admin_nak_only_fails_matching_request() -> None:
    """A request-scoped NAK should fail its owner without poisoning another wait."""
    with MeshInterface(noProto=True) as iface:
        iface._timeout.expireTimeout = 30.0  # noqa: SLF001
        iface._timeout.sleepInterval = 0.001  # noqa: SLF001
        request_ok = 303
        request_nak = 404
        iface._clear_wait_error(WAIT_ATTR_NAK, request_id=request_ok)  # noqa: SLF001
        iface._clear_wait_error(WAIT_ATTR_NAK, request_id=request_nak)  # noqa: SLF001

        ok_complete = threading.Event()
        nak_error: list[BaseException] = []

        def _wait_ok() -> None:
            iface._wait_for_ack_nak(request_ok)  # noqa: SLF001
            ok_complete.set()

        def _wait_nak() -> None:
            try:
                iface._wait_for_ack_nak(request_nak)  # noqa: SLF001
            except BaseException as exc:  # noqa: BLE001 - captured for assertion
                nak_error.append(exc)

        ok_thread = threading.Thread(target=_wait_ok, daemon=True)
        nak_thread = threading.Thread(target=_wait_nak, daemon=True)
        ok_thread.start()
        nak_thread.start()

        iface._set_wait_error(  # noqa: SLF001
            WAIT_ATTR_NAK,
            "Routing error on response: NO_RESPONSE",
            request_id=request_nak,
        )
        nak_thread.join(timeout=1.0)
        assert not nak_thread.is_alive()
        assert len(nak_error) == 1
        assert "NO_RESPONSE" in str(nak_error[0])
        assert not ok_complete.is_set()
        assert ok_thread.is_alive()

        iface._mark_wait_acknowledged(WAIT_ATTR_NAK, request_id=request_ok)  # noqa: SLF001
        ok_thread.join(timeout=1.0)
        assert not ok_thread.is_alive()
        assert ok_complete.is_set()


@pytest.mark.unit
def test_settings_response_marks_scoped_request_completion() -> None:
    """Successful settings callbacks should release only their request-scoped waiter."""
    iface = SimpleNamespace(
        _acknowledgment=Acknowledgment(),
        _extract_request_id_from_packet=lambda _packet: 515,
        _mark_wait_acknowledged=MagicMock(),
        _set_wait_error=MagicMock(),
    )
    node = SimpleNamespace(
        iface=iface,
        localConfig=localonly_pb2.LocalConfig(),
        moduleConfig=localonly_pb2.LocalModuleConfig(),
    )
    raw = admin_pb2.AdminMessage()
    raw.get_config_response.device.role = config_pb2.Config.DeviceConfig.Role.CLIENT
    packet: dict[str, Any] = {
        "decoded": {
            "admin": {
                "getConfigResponse": {"device": {}},
                "raw": raw,
            }
        }
    }

    _NodeSettingsResponseRuntime(node).handleSettingsResponse(packet)  # type: ignore[arg-type]

    assert iface._acknowledgment.receivedAck is True
    iface._mark_wait_acknowledged.assert_called_once_with(
        WAIT_ATTR_NAK,
        request_id=515,
    )
    iface._set_wait_error.assert_not_called()


@pytest.mark.unit
def test_settings_routing_error_records_scoped_request_failure() -> None:
    """Settings routing errors should be attached to the response request id."""
    iface = SimpleNamespace(
        _acknowledgment=Acknowledgment(),
        _extract_request_id_from_packet=lambda _packet: 616,
        _mark_wait_acknowledged=MagicMock(),
        _set_wait_error=MagicMock(),
    )
    node = SimpleNamespace(
        iface=iface,
        localConfig=localonly_pb2.LocalConfig(),
        moduleConfig=localonly_pb2.LocalModuleConfig(),
    )
    packet = {"decoded": {"routing": {"errorReason": "NO_RESPONSE"}}}

    _NodeSettingsResponseRuntime(node).handleSettingsResponse(packet)  # type: ignore[arg-type]

    assert iface._acknowledgment.receivedNak is True
    assert iface._set_wait_error.call_args_list == [
        ((WAIT_ATTR_NAK, "Routing error on response: NO_RESPONSE"), {}),
        (
            (WAIT_ATTR_NAK, "Routing error on response: NO_RESPONSE"),
            {"request_id": 616},
        ),
    ]
    iface._mark_wait_acknowledged.assert_not_called()


@pytest.mark.unit
def test_admin_send_helper_preserves_instance_level_transport_monkeypatch() -> None:
    """Legacy instance-level ``_send_admin`` doubles must not receive private kwargs."""
    calls: list[dict[str, Any]] = []

    def _send_admin(
        _message: admin_pb2.AdminMessage,
        *,
        onResponse: Any = None,  # noqa: N803 - compatibility surface under test
    ) -> mesh_pb2.MeshPacket:
        calls.append({"onResponse": onResponse})
        return mesh_pb2.MeshPacket(id=700)

    iface = SimpleNamespace(waitForAckNak=MagicMock())
    node = SimpleNamespace(iface=iface, _send_admin=_send_admin)

    from meshtastic.node_runtime.admin_wait import _send_admin_with_ack_scope

    request = _send_admin_with_ack_scope(
        node,  # type: ignore[arg-type]
        admin_pb2.AdminMessage(reboot_seconds=1),
        scope_ack=True,
        onResponse=object(),
    )

    assert request is not None
    assert request.id == 700
    assert len(calls) == 1
    assert set(calls[0]) == {"onResponse"}


@pytest.mark.unit
def test_fast_admin_ack_is_not_lost_between_send_and_wait(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A synchronous ACK during transport send must satisfy the later scoped wait."""
    from types import MethodType

    from meshtastic.node import Node

    with MeshInterface(noProto=True) as iface:
        iface.myInfo = mesh_pb2.MyNodeInfo(my_node_num=1)
        iface.localNode.nodeNum = 1
        iface._get_or_create_by_num = MethodType(  # type: ignore[method-assign]  # noqa: SLF001
            lambda _self, _node_num: {},
            iface,
        )
        remote = Node(iface, 2, noProto=False, timeout=0.2)
        observed_active_scope: list[bool] = []

        def _send_packet(
            packet: mesh_pb2.MeshPacket,
            *_args: object,
            **_kwargs: object,
        ) -> mesh_pb2.MeshPacket:
            observed_active_scope.append(
                packet.id
                in iface._active_wait_request_ids.get(WAIT_ATTR_NAK, set())  # noqa: SLF001
            )
            remote.onAckNak(
                {
                    "decoded": {
                        "requestId": packet.id,
                        "routing": {"errorReason": "NONE"},
                    },
                    "from": remote.nodeNum,
                }
            )
            return packet

        monkeypatch.setattr(iface, "_send_packet", _send_packet)

        request = remote._admin_command_runtime._send_command(  # noqa: SLF001
            admin_pb2.AdminMessage(reboot_seconds=1),
            ensure_session_key=False,
            use_remote_ack_callback=True,
        )

        assert request is not None
        assert observed_active_scope == [True]
        assert iface._active_wait_request_ids.get(WAIT_ATTR_NAK) is None  # noqa: SLF001
        assert request.id in iface._retired_wait_request_ids[WAIT_ATTR_NAK]  # noqa: SLF001


@pytest.mark.unit
def test_metadata_response_marks_scoped_request_completion() -> None:
    """Successful metadata payloads should complete their own request-scoped wait."""
    from meshtastic.node_runtime.response_runtime import _NodeMetadataResponseRuntime

    iface = SimpleNamespace(
        _acknowledgment=Acknowledgment(),
        _extract_request_id_from_packet=lambda _packet: 717,
        _mark_wait_acknowledged=MagicMock(),
        _set_wait_error=MagicMock(),
    )
    node = SimpleNamespace(
        iface=iface,
        _set_metadata_snapshot=MagicMock(),
        _emit_metadata_line=MagicMock(),
        position_flags_list=MagicMock(return_value=[]),
        excluded_modules_list=MagicMock(return_value=[]),
        _timeout=SimpleNamespace(reset=MagicMock()),
        _signal_metadata_stdout_event=MagicMock(),
    )
    raw = admin_pb2.AdminMessage()
    metadata = raw.get_device_metadata_response
    metadata.firmware_version = "2.8.0"
    metadata.device_state_version = 1
    metadata.role = config_pb2.Config.DeviceConfig.Role.CLIENT
    metadata.hw_model = mesh_pb2.HardwareModel.PORTDUINO
    packet: dict[str, Any] = {
        "decoded": {
            "portnum": "ADMIN_APP",
            "admin": {"raw": raw},
        }
    }

    _NodeMetadataResponseRuntime(node).handleMetadataResponse(packet)  # type: ignore[arg-type]

    assert iface._acknowledgment.receivedAck is True
    iface._mark_wait_acknowledged.assert_called_once_with(
        WAIT_ATTR_NAK,
        request_id=717,
    )
    iface._set_wait_error.assert_not_called()
    node._set_metadata_snapshot.assert_called_once()
    node._signal_metadata_stdout_event.assert_called_once_with()


@pytest.mark.unit
@pytest.mark.parametrize(
    ("packet", "error_fragment"),
    [
        ({"decoded": None}, "missing decoded"),
        (
            {
                "decoded": {
                    "portnum": "ROUTING_APP",
                    "routing": None,
                }
            },
            "missing routing",
        ),
        (
            {
                "decoded": {
                    "portnum": "ROUTING_APP",
                    "routing": {"errorReason": 1},
                }
            },
            "invalid routing.errorReason",
        ),
        ({"decoded": {"portnum": "ADMIN_APP"}}, "missing admin"),
        (
            {"decoded": {"portnum": "ADMIN_APP", "admin": {}}},
            "missing admin.raw",
        ),
        (
            {
                "decoded": {
                    "portnum": "ADMIN_APP",
                    "routing": {"errorReason": 1},
                }
            },
            "invalid routing.errorReason",
        ),
    ],
)
def test_malformed_metadata_response_records_request_scoped_failure(
    packet: dict[str, Any], error_fragment: str
) -> None:
    """Every terminal malformed metadata response should fail its scoped waiter."""
    from meshtastic.node_runtime.response_runtime import _NodeMetadataResponseRuntime

    request_id = 818
    packet.setdefault("decoded", {})
    if isinstance(packet.get("decoded"), dict):
        packet["decoded"]["requestId"] = request_id
    iface = SimpleNamespace(
        _acknowledgment=Acknowledgment(),
        _extract_request_id_from_packet=lambda _packet: request_id,
        _mark_wait_acknowledged=MagicMock(),
        _set_wait_error=MagicMock(),
    )
    node = SimpleNamespace(
        iface=iface,
        _signal_metadata_stdout_event=MagicMock(),
    )

    _NodeMetadataResponseRuntime(cast(Any, node)).handleMetadataResponse(packet)

    assert iface._acknowledgment.receivedNak is True
    assert iface._set_wait_error.call_args_list == [
        ((WAIT_ATTR_NAK, f"Received malformed metadata response ({error_fragment})."), {}),
        (
            (WAIT_ATTR_NAK, f"Received malformed metadata response ({error_fragment})."),
            {"request_id": request_id},
        ),
    ]
    iface._mark_wait_acknowledged.assert_not_called()
    node._signal_metadata_stdout_event.assert_called_once_with()


@pytest.mark.unit
def test_overlapping_remote_admin_commands_correlate_ack_and_nak_by_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Overlapping remote admin calls must resolve only from their own responses."""
    from types import MethodType

    from meshtastic.node import Node

    with MeshInterface(noProto=True) as iface:
        iface.myInfo = mesh_pb2.MyNodeInfo(my_node_num=1)
        iface.localNode.nodeNum = 1
        iface._get_or_create_by_num = MethodType(  # type: ignore[method-assign]  # noqa: SLF001
            lambda _self, _node_num: {},
            iface,
        )
        remote = Node(iface, 2, noProto=False, timeout=1.0)
        sent_packets: list[mesh_pb2.MeshPacket] = []
        sent_lock = threading.Lock()

        def _send_packet(
            packet: mesh_pb2.MeshPacket,
            *_args: object,
            **_kwargs: object,
        ) -> mesh_pb2.MeshPacket:
            snapshot = mesh_pb2.MeshPacket()
            snapshot.CopyFrom(packet)
            with sent_lock:
                sent_packets.append(snapshot)
            return packet

        monkeypatch.setattr(iface, "_send_packet", _send_packet)

        results: dict[int, str] = {}
        errors: dict[int, BaseException] = {}

        def _run(label: int) -> None:
            try:
                remote._admin_command_runtime._send_command(  # noqa: SLF001
                    admin_pb2.AdminMessage(reboot_seconds=label),
                    ensure_session_key=False,
                    use_remote_ack_callback=True,
                )
                results[label] = "ack"
            except BaseException as exc:  # noqa: BLE001 - captured for assertion
                errors[label] = exc

        thread_a = threading.Thread(target=_run, args=(1,), daemon=True)
        thread_b = threading.Thread(target=_run, args=(2,), daemon=True)
        thread_a.start()
        thread_b.start()
        _wait_until(lambda: len(sent_packets) == 2)

        with sent_lock:
            request_ids_by_seconds: dict[int, int] = {}
            for packet in sent_packets:
                admin_message = admin_pb2.AdminMessage()
                admin_message.ParseFromString(packet.decoded.payload)
                request_ids_by_seconds[admin_message.reboot_seconds] = packet.id
        assert set(request_ids_by_seconds) == {1, 2}
        assert len(set(request_ids_by_seconds.values())) == 2
        assert set(request_ids_by_seconds.values()).issubset(
            iface._active_wait_request_ids[WAIT_ATTR_NAK]  # noqa: SLF001
        )

        request_a = request_ids_by_seconds[1]
        request_b = request_ids_by_seconds[2]
        iface._request_wait_runtime.correlate_inbound_response(  # noqa: SLF001
            packet_dict={
                "decoded": {
                    "requestId": request_b,
                    "routing": {"errorReason": "NO_RESPONSE"},
                },
                "from": remote.nodeNum,
            },
            skip_response_callback_for_decode_failure=False,
            extract_request_id=iface._extract_request_id_from_packet,  # noqa: SLF001
        )
        _wait_until(lambda: len(errors) == 1)
        assert len(results) == 0
        assert set(errors) == {2}
        assert thread_a.is_alive()

        iface._request_wait_runtime.correlate_inbound_response(  # noqa: SLF001
            packet_dict={
                "decoded": {
                    "requestId": request_a,
                    "routing": {"errorReason": "NONE"},
                },
                "from": remote.nodeNum,
            },
            skip_response_callback_for_decode_failure=False,
            extract_request_id=iface._extract_request_id_from_packet,  # noqa: SLF001
        )

        thread_a.join(timeout=1.0)
        thread_b.join(timeout=1.0)
        assert not thread_a.is_alive()
        assert not thread_b.is_alive()
        assert results == {1: "ack"}
        assert set(errors) == {2}
        assert "NO_RESPONSE" in str(errors[2])
        assert iface._active_wait_request_ids.get(WAIT_ATTR_NAK) is None  # noqa: SLF001


@pytest.mark.unit
def test_admin_wait_request_id_helpers_handle_missing_legacy_hooks() -> None:
    """Request-id extraction should degrade safely on minimal compatibility doubles."""
    node = SimpleNamespace(iface=SimpleNamespace())

    assert _extract_request_id_from_sent_packet(cast(Any, node), None) is None
    assert (
        _extract_request_id_from_sent_packet(
            cast(Any, node),
            mesh_pb2.MeshPacket(id=0),
        )
        is None
    )
    assert _extract_request_id_from_response(cast(Any, node), {}) is None


@pytest.mark.unit
def test_admin_wait_error_noops_without_wait_error_hook() -> None:
    """Minimal interface doubles without scoped error storage should remain supported."""
    node = SimpleNamespace(iface=SimpleNamespace())

    _set_admin_wait_error(
        cast(Any, node),
        "ignored",
        request_id=123,
    )


@pytest.mark.unit
def test_admin_sender_signature_probe_handles_uninspectable_callable() -> None:
    """Compatibility send callables with invalid signatures should disable private scoping."""
    class _UninspectableCallable:
        @property
        def __signature__(self) -> object:
            raise ValueError("no signature")

        def __call__(self, *_args: object, **_kwargs: object) -> None:
            return None

    assert _accepts_response_wait_attr(_UninspectableCallable()) is False


@pytest.mark.unit
def test_request_scoped_admin_wait_timeout_retires_request() -> None:
    """A timed-out scoped ACK wait should retire its request bookkeeping."""
    request_id = 909
    with MeshInterface(noProto=True) as iface:
        iface._timeout.expireTimeout = 0.01  # noqa: SLF001
        iface._timeout.sleepInterval = 0.001  # noqa: SLF001
        iface._clear_wait_error(WAIT_ATTR_NAK, request_id=request_id)  # noqa: SLF001

        with pytest.raises(
            MeshInterface.MeshInterfaceError,
            match="Timed out waiting for an acknowledgment",
        ):
            iface._wait_for_ack_nak(request_id)  # noqa: SLF001

        assert request_id not in iface._active_wait_request_ids.get(  # noqa: SLF001
            WAIT_ATTR_NAK, set()
        )
        assert request_id in iface._retired_wait_request_ids[WAIT_ATTR_NAK]  # noqa: SLF001


@pytest.mark.unit
def test_settings_response_missing_expected_field_records_scoped_failure() -> None:
    """A structurally valid settings envelope missing its field should NAK its request."""
    iface = SimpleNamespace(
        _acknowledgment=Acknowledgment(),
        _extract_request_id_from_packet=lambda _packet: 818,
        _mark_wait_acknowledged=MagicMock(),
        _set_wait_error=MagicMock(),
    )
    node = SimpleNamespace(
        iface=iface,
        localConfig=localonly_pb2.LocalConfig(),
        moduleConfig=localonly_pb2.LocalModuleConfig(),
    )
    raw = admin_pb2.AdminMessage()
    packet: dict[str, Any] = {
        "decoded": {
            "admin": {
                "getConfigResponse": {"device": {}},
                "raw": raw,
            }
        }
    }

    _NodeSettingsResponseRuntime(node).handleSettingsResponse(packet)  # type: ignore[arg-type]

    assert iface._acknowledgment.receivedNak is True
    assert iface._set_wait_error.call_args_list == [
        (
            (WAIT_ATTR_NAK, "Received settings response without expected field 'device'."),
            {},
        ),
        (
            (WAIT_ATTR_NAK, "Received settings response without expected field 'device'."),
            {"request_id": 818},
        ),
    ]
    iface._mark_wait_acknowledged.assert_not_called()
