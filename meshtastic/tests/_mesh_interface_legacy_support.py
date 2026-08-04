"""Shared helpers for the decomposed legacy MeshInterface test modules."""

import threading
import time
import types
from collections.abc import Callable
from typing import Any, cast
from unittest.mock import MagicMock

import google.protobuf.json_format
import pytest
from google.protobuf.message import Message

import meshtastic.mesh_interface as mesh_interface_module
from meshtastic.mesh_interface import MeshInterface
from meshtastic.mesh_interface_runtime import receive_pipeline as receive_pipeline_module
from meshtastic.protobuf import mesh_pb2, portnums_pb2

from .. import ResponseHandler


def _start_wait_thread(
    wait_call: Callable[[], None],
) -> tuple[threading.Thread, list[BaseException]]:
    """Start a waiter in a background thread and capture raised exceptions.

    Parameters
    ----------
    wait_call : Callable[[], None]
        Blocking waiter to run in the background thread.

    Returns
    -------
    tuple[threading.Thread, list[BaseException]]
        Started daemon thread and the mutable exception collector populated by it.
    """
    errors: list[BaseException] = []

    def _run_wait() -> None:
        try:
            wait_call()
        except Exception as exc:  # noqa: BLE001 - asserted by caller
            errors.append(exc)

    thread = threading.Thread(target=_run_wait, daemon=True)
    thread.start()
    return thread, errors


def _wait_for_scoped_wait_registration(
    iface: MeshInterface,
    *,
    acknowledgment_attr: str,
    request_id: int,
    timeout_seconds: float = 1.0,
) -> None:
    """Wait until a request-scoped acknowledgment waiter is registered.

    Parameters
    ----------
    iface : MeshInterface
        Interface whose request-wait registry is inspected.
    acknowledgment_attr : str
        Acknowledgment state attribute associated with the waiter.
    request_id : int
        Request identifier expected in the active-wait registry.
    timeout_seconds : float
        Maximum registration wait in seconds.

    Raises
    ------
    Failed
        If the request-scoped waiter is not registered before the deadline.
    """
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        with iface._response_handlers_lock:  # noqa: SLF001
            if request_id in iface._active_wait_request_ids.get(  # noqa: SLF001
                acknowledgment_attr, set()
            ):
                return
        time.sleep(0.001)
    pytest.fail(
        "Timed out waiting for scoped waiter registration: "
        f"{acknowledgment_attr}#{request_id}"
    )


def _inline_queue_work(monkeypatch: pytest.MonkeyPatch) -> None:
    """Execute queued publish callbacks inline for deterministic packet tests.

    Parameters
    ----------
    monkeypatch : pytest.MonkeyPatch
        Patch fixture used to replace the publishing queue hook.
    """
    monkeypatch.setattr(
        mesh_interface_module.publishingThread,  # type: ignore[attr-defined]
        "queueWork",
        lambda callback: callback(),
    )


def _install_protocol_stub(
    monkeypatch: pytest.MonkeyPatch,
    *,
    portnum: portnums_pb2.PortNum.ValueType,
    name: str,
    protobuf_factory: object,
    on_receive: Callable[[MeshInterface, dict[str, Any]], None] | MagicMock,
) -> None:
    """Install a protocol stub for a decode-failure test case.

    Parameters
    ----------
    monkeypatch : pytest.MonkeyPatch
        Patch fixture used to replace the protocol registry.
    portnum : portnums_pb2.PortNum.ValueType
        Port number mapped to the protocol stub.
    name : str
        Human-readable protocol name.
    protobuf_factory : object
        Factory or sentinel exposed by the protocol stub.
    on_receive : Callable[[MeshInterface, dict[str, Any]], None] | MagicMock
        Receive callback exposed by the protocol stub.
    """
    fake_protocol = types.SimpleNamespace(
        name=name,
        protobufFactory=protobuf_factory,
        onReceive=on_receive,
    )
    monkeypatch.setattr(
        receive_pipeline_module,
        "protocols",
        {portnum: fake_protocol},
    )


def _make_decoded_packet(
    *,
    from_node: int = 1,
    to_node: int = 2,
    portnum: portnums_pb2.PortNum.ValueType,
    request_id: int,
    payload: bytes,
) -> mesh_pb2.MeshPacket:
    """Build a mesh packet with decoded payload fields pre-populated.

    Parameters
    ----------
    from_node : int
        Source node number.
    to_node : int
        Destination node number.
    portnum : portnums_pb2.PortNum.ValueType
        Decoded application port number.
    request_id : int
        Request identifier stored in the decoded payload.
    payload : bytes
        Serialized decoded payload.

    Returns
    -------
    mesh_pb2.MeshPacket
        Packet containing the requested decoded fields.
    """
    packet = mesh_pb2.MeshPacket()
    setattr(packet, "from", from_node)
    packet.to = to_node
    packet.decoded.portnum = portnum
    packet.decoded.request_id = request_id
    packet.decoded.payload = payload
    return packet


def _register_response_capture(
    iface: MeshInterface, request_id: int
) -> list[dict[str, Any]]:
    """Register a response handler that captures callback packets.

    Parameters
    ----------
    iface : MeshInterface
        Interface receiving the response handler.
    request_id : int
        Request identifier mapped to the handler.

    Returns
    -------
    list[dict[str, Any]]
        Mutable list populated whenever the callback is invoked.
    """
    callback_calls: list[dict[str, Any]] = []

    def _response_callback(packet: dict[str, Any]) -> None:
        callback_calls.append(packet)

    with iface._response_handlers_lock:  # noqa: SLF001
        iface.responseHandlers[request_id] = ResponseHandler(
            callback=_response_callback, ackPermitted=True
        )
    return callback_calls


def _patch_message_to_dict_position_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make ``MessageToDict`` fail for position messages.

    Parameters
    ----------
    monkeypatch : pytest.MonkeyPatch
        Patch fixture used to install the conversion failure shim.
    """
    original_message_to_dict = google.protobuf.json_format.MessageToDict

    def _message_to_dict_with_position_failure(
        message: Message,
        *args: object,
        **kwargs: object,
    ) -> dict[str, Any]:
        if isinstance(message, mesh_pb2.Position):
            raise TypeError("position dict conversion failed")  # noqa: TRY003
        message_to_dict = cast(Callable[..., dict[str, Any]], original_message_to_dict)
        return message_to_dict(message, *args, **kwargs)

    monkeypatch.setattr(
        google.protobuf.json_format,
        "MessageToDict",
        _message_to_dict_with_position_failure,
    )
