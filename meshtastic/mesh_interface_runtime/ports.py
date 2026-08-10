"""Narrow collaborator ports for MeshInterface runtime components."""

from __future__ import annotations

import threading
from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING, Any

from meshtastic.mesh_interface_runtime.queue_send import _QueueSendRuntime
from meshtastic.mesh_interface_runtime.request_wait import _RequestWaitRuntime
from meshtastic.protobuf import mesh_pb2
from meshtastic.util import Acknowledgment, Timeout

if TYPE_CHECKING:
    from meshtastic.mesh_interface import MeshInterface
    from meshtastic.node import Node
    from meshtastic.region_presets import RegionPresetInfo


def _interface_error_type(interface: Any) -> type[Exception]:
    """Resolve the MeshInterface error type without depending on mock internals.

    Parameters
    ----------
    interface : Any
        Real interface or compatibility test double.

    Returns
    -------
    type[Exception]
        Concrete interface error type.

    Raises
    ------
    TypeError
        If the supplied collaborator exposes no concrete exception type.
    """
    candidates = (
        getattr(interface, "MeshInterfaceError", None),
        getattr(interface.__class__, "MeshInterfaceError", None),
    )
    for candidate in candidates:
        if isinstance(candidate, type) and issubclass(candidate, Exception):
            return candidate
    raise TypeError("Interface collaborator exposes no MeshInterfaceError type")


class _InterfacePortBase:
    """Provide facade capabilities shared by MeshInterface runtime ports."""

    def __init__(self, interface: "MeshInterface") -> None:
        self._interface = interface

    @property
    def facade(self) -> "MeshInterface":
        """Return the facade when a public compatibility contract requires identity."""
        return self._interface

    @property
    def error_type(self) -> type[Exception]:
        """Return the interface-specific exception type."""
        return _interface_error_type(self._interface)

    @property
    def node_db_lock(self) -> threading.RLock:
        """Return the lock protecting the node database."""
        return self._interface._node_db_lock  # noqa: SLF001

    @property
    def local_node(self) -> "Node":
        """Return the local Node facade."""
        return self._interface.localNode

    @property
    def my_info(self) -> mesh_pb2.MyNodeInfo | None:
        """Return local node identity information."""
        return self._interface.myInfo

    @property
    def nodes(self) -> dict[str, dict[str, Any]] | None:
        """Return the node database indexed by node ID."""
        return self._interface.nodes

    @property
    def nodes_by_num(self) -> dict[int, dict[str, Any]] | None:
        """Return the node database indexed by node number."""
        return self._interface.nodesByNum


class _NoProtoInterfacePort(_InterfacePortBase):
    """Add the protocol-disable capability used by send and node-view ports."""

    @property
    def no_proto(self) -> bool:
        """Return whether protobuf protocol traffic is disabled."""
        return bool(getattr(self._interface, "noProto", False))


class _SendPipelinePort(_NoProtoInterfacePort):
    """Expose only MeshInterface capabilities required by the send pipeline."""

    @property
    def request_wait_runtime(self) -> _RequestWaitRuntime:
        """Return request/response wait state."""
        return self._interface._request_wait_runtime  # noqa: SLF001

    @property
    def queue_send_runtime(self) -> _QueueSendRuntime:
        """Return the firmware TX-queue coordinator."""
        return self._interface._queue_send_runtime  # noqa: SLF001

    @property
    def config_id(self) -> int | None:
        """Return the current configuration identifier."""
        return self._interface.configId

    @property
    def acknowledgment(self) -> Acknowledgment:
        """Return the historical unscoped ACK/NAK state."""
        return self._interface._acknowledgment  # noqa: SLF001

    @property
    def timeout(self) -> Timeout:
        """Return the interface timeout helper."""
        return self._interface._timeout  # noqa: SLF001

    def generate_packet_id(self) -> int:
        """Generate a packet ID through the facade's compatibility seam."""
        return self._interface._generate_packet_id()  # noqa: SLF001

    def send_packet(
        self,
        packet: mesh_pb2.MeshPacket,
        destination_id: int | str,
        *,
        want_ack: bool,
        hop_limit: int | None,
        pki_encrypted: bool | None,
        public_key: bytes | None,
    ) -> mesh_pb2.MeshPacket:
        """Send a packet through the facade seam used by existing monkeypatches."""
        return self._interface._send_packet(  # noqa: SLF001
            packet,
            destination_id,
            wantAck=want_ack,
            hopLimit=hop_limit,
            pkiEncrypted=pki_encrypted,
            publicKey=public_key,
        )

    def wait_connected(self) -> None:
        """Wait until the interface connection is ready."""
        self._interface._wait_connected()  # noqa: SLF001

    def send_to_radio(self, to_radio: mesh_pb2.ToRadio) -> None:
        """Send a ToRadio message through the facade compatibility seam."""
        self._interface._send_to_radio(to_radio)  # noqa: SLF001

    def send_to_radio_impl(self, to_radio: mesh_pb2.ToRadio) -> None:
        """Invoke the transport-specific ToRadio implementation."""
        self._interface._send_to_radio_impl(to_radio)  # noqa: SLF001

    def wait_for_initial_config(self) -> bool:
        """Wait for interface identity and node database bootstrap state."""
        return self.timeout.waitForSet(self._interface, attrs=("myInfo", "nodes"))


class _ReceivePipelinePort(_InterfacePortBase):
    """Expose state and mutations required by the receive pipeline."""

    @property
    def request_wait_runtime(self) -> _RequestWaitRuntime:
        """Return request/response wait state."""
        return self._interface._request_wait_runtime  # noqa: SLF001

    @property
    def queue_send_runtime(self) -> _QueueSendRuntime:
        """Return the firmware TX-queue coordinator."""
        return self._interface._queue_send_runtime  # noqa: SLF001

    @property
    def config_id(self) -> int | None:
        """Return the current configuration identifier."""
        return self._interface.configId

    @property
    def metadata(self) -> mesh_pb2.DeviceMetadata | None:
        """Return current device metadata."""
        return self._interface.metadata

    def record_bootstrap_decode_error(self) -> int:
        """Record one malformed bootstrap frame when the facade supports it."""
        recorder_name = "_record_bootstrap_decode_error"
        interface = self._interface
        has_recorder = recorder_name in vars(interface) or hasattr(
            type(interface), recorder_name
        )
        recorder = getattr(interface, recorder_name, None) if has_recorder else None
        return recorder() if callable(recorder) else 0

    def set_my_info(self, my_info: mesh_pb2.MyNodeInfo) -> None:
        """Install local node identity and synchronize the local Node number."""
        self._interface.myInfo = my_info
        self._interface.localNode.nodeNum = my_info.my_node_num

    def set_metadata(self, metadata: mesh_pb2.DeviceMetadata) -> None:
        """Install device metadata."""
        self._interface.metadata = metadata

    def set_region_presets(
        self,
        raw_map: mesh_pb2.LoRaRegionPresetMap,
        decoded: Mapping[int, "RegionPresetInfo"],
    ) -> None:
        """Install raw and decoded region-preset compatibility data."""
        self._interface.regionPresetMap = raw_map
        self._interface.regionPresets = decoded

    def set_lockdown_status(self, status: mesh_pb2.LockdownStatus) -> None:
        """Install the current lockdown status."""
        self._interface.lockdownStatus = status

    def restart_config_after_reboot(self) -> None:
        """Run the facade's disconnect and configuration-restart sequence."""
        self._interface._disconnected()  # noqa: SLF001
        self._interface._start_config()  # noqa: SLF001

    def append_local_channel(self, channel: Any) -> None:
        """Append one channel descriptor to the bootstrap channel buffer."""
        self._interface._localChannels.append(channel)  # noqa: SLF001

    def local_channels_snapshot(self) -> list[Any]:
        """Return a shallow snapshot of bootstrap channel descriptors."""
        return list(self._interface._localChannels)  # noqa: SLF001

    def handle_log_line(self, line: str) -> None:
        """Forward one device log line to the facade log handler."""
        self._interface._handle_log_line(line)  # noqa: SLF001

    def complete_config(self, local_channels: list[Any]) -> None:
        """Install bootstrap channels and mark the interface connected."""
        self._interface.localNode.setChannels(local_channels)
        self._interface._connected()  # noqa: SLF001

    def invoke_receive_callback(
        self,
        callback: Callable[[Any, dict[str, Any]], Any],
        packet: dict[str, Any],
    ) -> None:
        """Invoke a protocol callback with the historical facade argument."""
        callback(self._interface, packet)

    def extract_request_id(self, packet: dict[str, Any]) -> int | None:
        """Extract a request ID through the facade's compatibility seam."""
        return self._interface._extract_request_id_from_packet(packet)  # noqa: SLF001


class _NodeViewPort(_NoProtoInterfacePort):
    """Expose MeshInterface state needed by node lookup and presentation."""

    @property
    def metadata(self) -> mesh_pb2.DeviceMetadata | None:
        """Return device metadata."""
        return self._interface.metadata

    @property
    def debug_out(self) -> Any:
        """Return the historical debug-output sink."""
        return self._interface.debugOut
