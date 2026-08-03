"""Shared helpers for the decomposed legacy Node test modules."""

import base64
from collections.abc import Callable, Mapping
from types import TracebackType
from typing import Any, Literal, Protocol

from meshtastic.node import Node
from meshtastic.protobuf import admin_pb2, apponly_pb2, mesh_pb2
from meshtastic.util import Acknowledgment


class _FakeSendAdminProtocol(Protocol):
    """Callable protocol for fake ``_send_admin`` helpers."""

    def __call__(
        self,
        msg: admin_pb2.AdminMessage,
        wantResponse: bool = False,
        onResponse: Callable[[dict[str, Any]], Any] | None = None,
        adminIndex: int | None = None,
        responseWaitAttr: str | None = None,
    ) -> mesh_pb2.MeshPacket | None: ...


class _DropChannelsOnEnterCountLock:
    """Lock stub that clears ``node.channels`` on a specific acquisition count."""

    def __init__(self, node: Node, trigger_enter: int) -> None:
        self.node = node
        self.trigger_enter = trigger_enter
        self.enters = 0

    def __enter__(self) -> "_DropChannelsOnEnterCountLock":
        self.enters += 1
        if self.enters == self.trigger_enter:
            self.node.channels = None
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> Literal[False]:
        _ = (exc_type, exc, tb)
        return False


class _TrackingLock:
    """Lock stub that records how many times it was acquired."""

    def __init__(self, on_exit: Callable[[], None] | None = None) -> None:
        self.enter_count = 0
        self.is_held = False
        self._on_exit = on_exit

    def __enter__(self) -> "_TrackingLock":
        if self.is_held:
            raise AssertionError("_TrackingLock does not allow nested acquisition")
        self.enter_count += 1
        self.is_held = True
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> Literal[False]:
        _ = (exc_type, exc, tb)
        self.is_held = False
        if self._on_exit is not None:
            self._on_exit()
        return False


class _MetadataLockProbeIface:
    """Minimal interface stub that records metadata read/write lock state."""

    def __init__(
        self,
        node_db_lock: _TrackingLock,
        *,
        metadata: mesh_pb2.DeviceMetadata | None = None,
        metadata_read_lock_states: list[bool] | None = None,
        include_acknowledgment: bool = False,
    ) -> None:
        self._node_db_lock = node_db_lock
        self._metadata = metadata
        self._metadata_read_lock_states = metadata_read_lock_states
        self.metadata_assignment_lock_state: bool | None = None
        if include_acknowledgment:
            self._acknowledgment = Acknowledgment()

    @property
    def metadata(self) -> mesh_pb2.DeviceMetadata | None:
        """Return metadata while optionally recording lock-held read state."""
        if self._metadata_read_lock_states is not None:
            self._metadata_read_lock_states.append(self._node_db_lock.is_held)
        return self._metadata

    @metadata.setter
    def metadata(self, value: mesh_pb2.DeviceMetadata | None) -> None:
        """Store metadata while recording lock-held write state."""
        self.metadata_assignment_lock_state = self._node_db_lock.is_held
        self._metadata = value


def _make_fake_send_admin(
    *,
    sent_messages: list[admin_pb2.AdminMessage] | None = None,
    captured: dict[str, object] | None = None,
    expected_want_response: bool | None = None,
    response_payload: dict[str, Any] | None = None,
    return_packet: mesh_pb2.MeshPacket | None = None,
) -> _FakeSendAdminProtocol:
    """Create a configurable fake for Node._send_admin."""

    def _fake_send_admin(
        msg: admin_pb2.AdminMessage,
        wantResponse: bool = False,
        onResponse: Callable[[dict[str, Any]], Any] | None = None,
        adminIndex: int | None = None,
        responseWaitAttr: str | None = None,
    ) -> mesh_pb2.MeshPacket | None:
        if sent_messages is not None:
            sent_messages.append(msg)
        if captured is not None:
            captured["msg"] = msg
            captured["wantResponse"] = wantResponse
            captured["onResponse"] = onResponse
            captured["adminIndex"] = adminIndex
            captured["responseWaitAttr"] = responseWaitAttr
        if expected_want_response is not None:
            assert wantResponse is expected_want_response
        if response_payload is not None:
            assert onResponse is not None
            onResponse(response_payload)
        return return_packet

    return _fake_send_admin


class _MockCallLike(Protocol):
    """Protocol for mock call objects exposing positional/keyword arguments."""

    @property
    def args(self) -> tuple[object, ...]:
        """Positional call arguments."""
        raise NotImplementedError

    @property
    def kwargs(self) -> Mapping[str, object]:
        """Keyword call arguments."""
        raise NotImplementedError


def _get_mock_call_arg(
    call: _MockCallLike, *, name: str, positional_index: int
) -> object | None:
    """Resolve a mock call argument regardless of positional/keyword call style."""
    if len(call.args) > positional_index:
        return call.args[positional_index]
    return call.kwargs.get(name)


def _decode_channel_set_from_url(url: str) -> apponly_pb2.ChannelSet:
    """Decode and parse a ChannelSet from a Meshtastic URL."""
    encoded = url.split("#")[-1]
    missing_padding = len(encoded) % 4
    if missing_padding:
        encoded += "=" * (4 - missing_padding)
    raw = base64.urlsafe_b64decode(encoded)
    channel_set = apponly_pb2.ChannelSet()
    channel_set.ParseFromString(raw)
    return channel_set


def _encode_channel_set_to_url(channel_set: apponly_pb2.ChannelSet) -> str:
    """Encode a ChannelSet as a Meshtastic URL."""
    encoded = base64.urlsafe_b64encode(channel_set.SerializeToString()).decode("ascii")
    return f"https://meshtastic.org/e/#{encoded.rstrip('=')}"
