"""Process-managed meshtasticd simulator harness for firmware smoke tests.

The harness launches native ``meshtasticd`` processes in simulator mode and
bridges their ``SIMULATOR_APP`` packets according to an explicit topology. It
is additive to the container-backed integration harnesses in ``bin/``: native
simradio jobs exercise daily/alpha/beta firmware packages, while the existing
Docker jobs retain a pinned regression baseline.
"""

from __future__ import annotations

import contextlib
import logging
import os
import platform
import shutil
import signal
import socket
import subprocess
import tempfile
import threading
import time
from collections.abc import Collection, Mapping
from pathlib import Path
from types import MappingProxyType
from typing import Any, BinaryIO

from pubsub import pub

from meshtastic import BROADCAST_NUM
from meshtastic.mesh_interface import MeshInterface
from meshtastic.protobuf import mesh_pb2, portnums_pb2
from meshtastic.tcp_interface import TCPInterface

from .simradio_helpers import connect_iface

logger = logging.getLogger(__name__)

HW_ID_OFFSET = 16
DEFAULT_BASE_PORT = 4404
DEFAULT_RSSI = -50
DEFAULT_SNR = 10.0
BOOT_TIMEOUT_SECONDS = 30.0
PROCESS_EXIT_TIMEOUT_SECONDS = 5.0
PORT_RELEASE_SETTLE_SECONDS = 0.25
TEXT_MESSAGE_MIN_INTERVAL_SECONDS = 2.25
REBOOT_POLL_INTERVAL_SECONDS = 0.1
MAX_LOG_TAIL_BYTES = 16_384

CHAIN_TOPOLOGY: Mapping[int, frozenset[int]] = MappingProxyType(
    {
        0: frozenset({1}),
        1: frozenset({0, 2}),
        2: frozenset({1}),
    }
)


def find_meshtasticd() -> str | None:
    """Return an executable meshtasticd path from the environment or PATH."""
    configured = os.environ.get("MESHTASTICD_BIN")
    if configured:
        candidate = Path(configured).expanduser()
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate.resolve())
    return shutil.which("meshtasticd")


def is_compatible_host() -> bool:
    """Return whether native meshtasticd simulator tests support this host."""
    return platform.system() == "Linux"


def _ensure_port_available(port: int) -> None:
    """Fail before launch when another process already owns ``port``."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.bind(("127.0.0.1", port))
    except OSError as exc:
        raise RuntimeError(f"localhost:{port} is already in use") from exc


def _copy_channel_topology(
    topology: Mapping[int, Collection[int]] | None,
    node_count: int,
) -> dict[int, frozenset[int]] | None:
    """Validate and defensively copy an optional directed receiver topology."""
    if topology is None:
        return None
    copied: dict[int, frozenset[int]] = {}
    for transmitter, receivers in topology.items():
        if transmitter < 0 or transmitter >= node_count:
            raise ValueError(f"Topology transmitter index out of range: {transmitter}")
        normalized_receivers = frozenset(receivers)
        if transmitter in normalized_receivers:
            raise ValueError(f"Topology node {transmitter} cannot receive its own packet")
        invalid = sorted(
            receiver
            for receiver in normalized_receivers
            if receiver < 0 or receiver >= node_count
        )
        if invalid:
            raise ValueError(
                f"Topology receiver indices out of range for node {transmitter}: {invalid}"
            )
        copied[transmitter] = normalized_receivers
    return copied


def _inject_simulator_packet(
    iface: TCPInterface,
    to_radio: mesh_pb2.ToRadio,
) -> None:
    """Inject a simulator packet across fork and current-upstream internals."""
    sender = getattr(iface, "_send_to_radio", None)
    if not callable(sender):
        # Upstream's first simradio harness still uses this pre-refactor
        # internal spelling. Keeping the fallback here isolates that divergence
        # without expanding either library's public compatibility surface.
        sender = getattr(iface, "_sendToRadio", None)
    if not callable(sender):
        raise RuntimeError("TCPInterface has no simulator injection method")
    sender(to_radio)


class SimNode:
    """Own one meshtasticd process, filesystem, logs, and TCP interface."""

    def __init__(
        self,
        node_id: int,
        *,
        base_port: int = DEFAULT_BASE_PORT,
        log_root: Path | None = None,
    ) -> None:
        if node_id < 0:
            raise ValueError("node_id must not be negative")
        if base_port <= 0 or base_port + node_id > 65_535:
            raise ValueError("simradio node port must stay within 1..65535")
        self.node_id = node_id
        self.hw_id = node_id + HW_ID_OFFSET
        self.port = base_port + node_id
        self.log_root = log_root
        self.process: subprocess.Popen[bytes] | None = None
        self.workdir: Path | None = None
        self.iface: TCPInterface | None = None
        self._temporary_directory: tempfile.TemporaryDirectory[str] | None = None
        self._log_files: list[BinaryIO] = []

    @property
    def node_num(self) -> int:
        """Return the firmware node number, falling back to the configured HW ID."""
        iface = self.iface
        if iface is not None and iface.myInfo is not None:
            return iface.myInfo.my_node_num
        return self.hw_id

    def start(self, binary: str) -> None:
        """Launch a freshly erased meshtasticd simulator process."""
        if self.process is not None or self._temporary_directory is not None:
            raise RuntimeError(f"simradio node {self.node_id} is already started")
        _ensure_port_available(self.port)
        try:
            self._temporary_directory = tempfile.TemporaryDirectory(
                prefix=f"meshtasticd-simradio-{self.node_id}-"
            )
            self.workdir = Path(self._temporary_directory.name)
            vfs_directory = self.workdir / "vfs"
            vfs_directory.mkdir()
            stdout_path = self.workdir / "meshtasticd.log"
            stderr_path = self.workdir / "meshtasticd.err"
            stdout_file = stdout_path.open("wb", buffering=0)
            self._log_files.append(stdout_file)
            stderr_file = stderr_path.open("wb", buffering=0)
            self._log_files.append(stderr_file)
            self.process = subprocess.Popen(  # pylint: disable=consider-using-with
                [
                    binary,
                    "-s",
                    "-h",
                    str(self.hw_id),
                    "-p",
                    str(self.port),
                    "-d",
                    str(vfs_directory),
                    "-e",
                ],
                stdout=stdout_file,
                stderr=stderr_file,
                start_new_session=True,
            )
            if self.process.poll() is not None:
                raise RuntimeError(
                    f"meshtasticd node {self.node_id} exited with "
                    f"status {self.process.returncode} during startup"
                )
        except Exception as exc:
            diagnostics = self.diagnostics()
            self.close(send_exit=False)
            raise RuntimeError(
                f"meshtasticd node {self.node_id} failed to start on port "
                f"{self.port}: {exc}\n{diagnostics}"
            ) from exc

    def connect(self, timeout: float = BOOT_TIMEOUT_SECONDS) -> TCPInterface:
        """Retry and retain the sole TCPInterface connection to this simulator."""
        self.disconnect()
        process = self.process
        if process is None:
            raise RuntimeError(f"simradio node {self.node_id} is not started")
        if process.poll() is not None:
            raise RuntimeError(
                f"meshtasticd node {self.node_id} exited with status "
                f"{process.returncode}\n{self.diagnostics()}"
            )
        try:
            iface = connect_iface(self.port, wait_timeout=timeout)
        except Exception as exc:
            raise RuntimeError(
                f"meshtasticd node {self.node_id} did not become ready on port "
                f"{self.port}: {exc}\n{self.diagnostics()}"
            ) from exc
        self.iface = iface
        return iface

    def disconnect(self) -> None:
        """Close only the Python interface while leaving firmware running."""
        iface = self.iface
        self.iface = None
        if iface is not None:
            with contextlib.suppress(Exception):
                iface.close()

    def boot_count(self) -> int:
        """Return the number of simulator boots recorded in the process log."""
        workdir = self.workdir
        if workdir is None:
            return 0
        try:
            output = (workdir / "meshtasticd.log").read_text(
                encoding="utf-8", errors="replace"
            )
        except OSError:
            return 0
        return output.count(f"Using config file {self.port}")

    def wait_for_reboot(
        self,
        previous_boot_count: int,
        *,
        timeout: float = BOOT_TIMEOUT_SECONDS,
    ) -> None:
        """Wait until the owned simulator records a boot after ``previous_boot_count``.

        Portduino reboots in-process, so a successful TCP connection alone does
        not prove that a delayed reboot has occurred.  Its stable
        ``Using config file <port>`` startup marker does.
        """
        if previous_boot_count < 0:
            raise ValueError("previous_boot_count must not be negative")
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        process = self.process
        if process is None:
            raise RuntimeError(f"simradio node {self.node_id} is not started")
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise RuntimeError(
                    f"meshtasticd node {self.node_id} exited with status "
                    f"{process.returncode} while waiting for reboot\n"
                    f"{self.diagnostics()}"
                )
            if self.boot_count() > previous_boot_count:
                return
            time.sleep(REBOOT_POLL_INTERVAL_SECONDS)
        raise RuntimeError(
            f"meshtasticd node {self.node_id} did not reboot within {timeout:.1f}s; "
            f"boot count remained {self.boot_count()}\n{self.diagnostics()}"
        )

    def diagnostics(self) -> str:
        """Return bounded stdout/stderr tails for startup and CI failures."""
        workdir = self.workdir
        if workdir is None:
            return "simradio diagnostics unavailable: no work directory"
        sections: list[str] = []
        for filename in ("meshtasticd.log", "meshtasticd.err"):
            path = workdir / filename
            try:
                raw = path.read_bytes()[-MAX_LOG_TAIL_BYTES:]
            except OSError as exc:
                sections.append(f"[{filename}] unavailable: {exc}")
                continue
            sections.append(
                f"[{filename} tail]\n{raw.decode('utf-8', errors='replace')}"
            )
        return "\n".join(sections)

    def close(self, *, send_exit: bool = True) -> None:
        """Close the interface, terminate the process group, and archive logs."""
        iface = self.iface
        self.iface = None
        if iface is not None:
            if send_exit:
                with contextlib.suppress(Exception):
                    iface.localNode.exitSimulator()
            with contextlib.suppress(Exception):
                iface.close()

        process = self.process
        self.process = None
        if process is not None and process.poll() is None:
            with contextlib.suppress(ProcessLookupError, OSError):
                os.killpg(os.getpgid(process.pid), signal.SIGTERM)
            try:
                process.wait(timeout=PROCESS_EXIT_TIMEOUT_SECONDS)
            except subprocess.TimeoutExpired:
                with contextlib.suppress(ProcessLookupError, OSError):
                    os.killpg(os.getpgid(process.pid), signal.SIGKILL)
                with contextlib.suppress(subprocess.TimeoutExpired):
                    process.wait(timeout=PROCESS_EXIT_TIMEOUT_SECONDS)

        for log_file in self._log_files:
            with contextlib.suppress(Exception):
                log_file.close()
        self._log_files.clear()
        self._archive_logs()
        temporary_directory = self._temporary_directory
        self._temporary_directory = None
        self.workdir = None
        if temporary_directory is not None:
            try:
                temporary_directory.cleanup()
            except OSError:
                logger.exception(
                    "Failed to remove simradio work directory for node %d",
                    self.node_id,
                )
        if process is not None:
            time.sleep(PORT_RELEASE_SETTLE_SECONDS)

    def _archive_logs(self) -> None:
        """Copy process logs to the configured persistent CI artifact directory."""
        if self.log_root is None or self.workdir is None:
            return
        destination = self.log_root / f"node-{self.node_id}" / self.workdir.name
        try:
            destination.mkdir(parents=True, exist_ok=True)
            for filename in ("meshtasticd.log", "meshtasticd.err"):
                source = self.workdir / filename
                if source.exists():
                    shutil.copy2(source, destination / filename)
        except OSError:
            logger.exception(
                "Failed to archive simradio logs for node %d to %s",
                self.node_id,
                destination,
            )


class SimMesh:
    """Manage simulator nodes and bridge packets through a directed topology."""

    def __init__(
        self,
        node_count: int = 1,
        *,
        topology: Mapping[int, Collection[int]] | None = None,
        base_port: int = DEFAULT_BASE_PORT,
        log_root: Path | None = None,
    ) -> None:
        if node_count <= 0:
            raise ValueError("node_count must be positive")
        if base_port <= 0 or base_port + node_count - 1 > 65_535:
            raise ValueError("simradio port range must stay within 1..65535")
        self.node_count = node_count
        self.topology = _copy_channel_topology(topology, node_count)
        self.base_port = base_port
        self.nodes = [
            SimNode(index, base_port=base_port, log_root=log_root)
            for index in range(node_count)
        ]
        self._port_to_index: dict[int, int] = {}
        self._bridge_lock = threading.Lock()
        self._text_send_lock = threading.Lock()
        self._last_text_send_at: dict[int, float] = {}
        self._subscribed = False
        self._started = False

    def start(self) -> None:
        """Start and connect all nodes, cleaning up atomically on failure."""
        if self._started:
            raise RuntimeError("simradio mesh is already started")
        binary = find_meshtasticd()
        if binary is None:
            raise RuntimeError(
                "meshtasticd not found; set MESHTASTICD_BIN or install it on PATH"
            )
        with self._text_send_lock:
            self._last_text_send_at.clear()
        try:
            for node in self.nodes:
                node.start(binary)
            for node in self.nodes:
                node.connect()
                self._port_to_index[node.port] = node.node_id
            pub.subscribe(self._on_sim_packet, "meshtastic.receive.simulator")
            self._subscribed = True
            self._started = True
            if self.node_count > 1:
                self.trigger_node_info_exchange()
        except Exception:
            self.stop()
            raise

    def stop(self) -> None:
        """Unsubscribe and close every node, including partially started meshes."""
        if self._subscribed:
            with contextlib.suppress(Exception):
                pub.unsubscribe(self._on_sim_packet, "meshtastic.receive.simulator")
            self._subscribed = False
        self._started = False
        with self._bridge_lock:
            self._port_to_index.clear()
        for node in reversed(self.nodes):
            try:
                node.close()
            except Exception:  # pylint: disable=broad-except
                logger.exception("Failed to close simradio node %d", node.node_id)

    def reconnect_all(self) -> None:
        """Reconnect every harness interface after reboot-capable config changes."""
        for node in self.nodes:
            node.connect()
        if self.node_count > 1:
            self.trigger_node_info_exchange()

    def get_node(self, index: int) -> SimNode:
        """Return one simulator node by zero-based index."""
        return self.nodes[index]

    def get_iface(self, index: int) -> TCPInterface:
        """Return a connected interface for one simulator node."""
        iface = self.nodes[index].iface
        if iface is None:
            raise RuntimeError(f"simradio node {index} is not connected")
        return iface

    def send_text(
        self,
        sender_index: int,
        text: str,
        **kwargs: Any,
    ) -> mesh_pb2.MeshPacket:
        """Send text while respecting firmware's PhoneAPI text throttle.

        The mesh fixture is module-scoped, so a test may begin immediately
        after the previous test's text send.  Some firmware channels reject a
        second ``TEXT_MESSAGE_APP`` packet inside its two-second window instead
        of queueing it.  Pace sends per originating node with a small scheduling
        margin so test ordering and
        packet propagation speed cannot decide whether the next packet is
        accepted.
        """
        iface = self.get_iface(sender_index)
        with self._text_send_lock:
            now = time.monotonic()
            last_send = self._last_text_send_at.get(sender_index)
            if last_send is not None:
                remaining = TEXT_MESSAGE_MIN_INTERVAL_SECONDS - (now - last_send)
                if remaining > 0:
                    time.sleep(remaining)
            result = iface.sendText(text, **kwargs)
            self._last_text_send_at[sender_index] = time.monotonic()
        return result

    def trigger_node_info_exchange(self) -> None:
        """Request broadcast NodeInfo responses to accelerate DB convergence."""
        for node in self.nodes:
            iface = node.iface
            if iface is None:
                continue
            user = mesh_pb2.User(
                id=f"!{node.node_num:08x}",
                long_name=f"Simradio Node {node.node_id}",
                short_name=f"S{node.node_id:03d}"[-4:],
                hw_model=mesh_pb2.HardwareModel.PORTDUINO,
            )
            try:
                iface.sendData(
                    user,
                    destinationId=BROADCAST_NUM,
                    portNum=portnums_pb2.PortNum.NODEINFO_APP,
                    wantAck=False,
                    wantResponse=True,
                )
            except Exception as exc:  # pylint: disable=broad-except
                logger.debug(
                    "NodeInfo trigger for simradio node %d failed: %s",
                    node.node_id,
                    exc,
                )

    def node_db_counts(self) -> list[int]:
        """Return current node-database sizes for diagnostics."""
        return [
            len(node.iface.nodes)
            if node.iface is not None and node.iface.nodes is not None
            else 0
            for node in self.nodes
        ]

    def wait_for_convergence(self, timeout: float = 30.0) -> bool:
        """Wait until every node database contains the complete simulated mesh."""
        if self.node_count <= 1:
            return True
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if all(count >= self.node_count for count in self.node_db_counts()):
                return True
            time.sleep(0.5)
        logger.warning(
            "Simradio mesh did not converge in %.1fs; node DB counts=%s",
            timeout,
            self.node_db_counts(),
        )
        return False

    def _receiver_indices(self, transmitter_index: int) -> tuple[int, ...]:
        """Return deterministic receivers for a transmitter node."""
        if self.topology is not None:
            return tuple(sorted(self.topology.get(transmitter_index, frozenset())))
        return tuple(
            index for index in range(self.node_count) if index != transmitter_index
        )

    def _on_sim_packet(
        self,
        packet: dict[str, Any],
        interface: MeshInterface,
    ) -> None:
        """Forward one SIMULATOR_APP packet to topology-selected receivers."""
        transmitter_port = getattr(interface, "portNumber", None)
        if not isinstance(transmitter_port, int):
            return
        transmitter_index = self._port_to_index.get(transmitter_port)
        if transmitter_index is None:
            return
        receiver_indices = self._receiver_indices(transmitter_index)
        if not receiver_indices:
            return
        decoded = packet.get("decoded")
        if not isinstance(decoded, dict):
            logger.warning("Dropping simulator packet without decoded mapping")
            return
        payload = decoded.get("payload", b"")
        if hasattr(payload, "SerializeToString"):
            payload = payload.SerializeToString()
        if not isinstance(payload, bytes):
            try:
                payload = bytes(payload)
            except (TypeError, ValueError):
                logger.warning("Dropping simulator packet with non-bytes payload")
                return
        if len(payload) > mesh_pb2.Constants.DATA_PAYLOAD_LEN:
            logger.warning("Dropping oversized simulator payload: %d bytes", len(payload))
            return
        try:
            base_packet = _build_mesh_packet(packet, payload)
        except (TypeError, ValueError) as exc:
            logger.warning("Dropping malformed simulator packet: %s", exc)
            return
        with self._bridge_lock:
            if not self._started:
                return
            receivers = tuple(
                (receiver_index, self.nodes[receiver_index].iface)
                for receiver_index in receiver_indices
            )

        for receiver_index, receiver_iface in receivers:
            if receiver_iface is None:
                continue
            received_packet = mesh_pb2.MeshPacket()
            received_packet.CopyFrom(base_packet)
            received_packet.rx_rssi = DEFAULT_RSSI
            received_packet.rx_snr = DEFAULT_SNR
            to_radio = mesh_pb2.ToRadio()
            to_radio.packet.CopyFrom(received_packet)
            try:
                _inject_simulator_packet(receiver_iface, to_radio)
            except Exception as exc:  # pylint: disable=broad-except
                logger.error(
                    "Failed forwarding simulator packet from node %d to node %d: %s",
                    transmitter_index,
                    receiver_index,
                    exc,
                )

    def __enter__(self) -> SimMesh:
        self.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.stop()


def _build_mesh_packet(
    packet: Mapping[str, Any], payload: bytes
) -> mesh_pb2.MeshPacket:
    """Reconstruct the firmware-facing packet used for simulator injection."""
    mesh_packet = mesh_pb2.MeshPacket()
    mesh_packet.decoded.payload = payload
    mesh_packet.decoded.portnum = portnums_pb2.PortNum.SIMULATOR_APP
    mesh_packet.to = int(packet.get("to", BROADCAST_NUM))
    setattr(mesh_packet, "from", int(packet.get("from", 0)))
    mesh_packet.id = int(packet.get("id", 0))
    mesh_packet.want_ack = bool(packet.get("wantAck", False))
    mesh_packet.hop_limit = int(packet.get("hopLimit", 0))
    mesh_packet.hop_start = int(packet.get("hopStart", 0))
    mesh_packet.via_mqtt = bool(packet.get("viaMQTT", False))
    mesh_packet.relay_node = int(packet.get("relayNode", 0))
    mesh_packet.next_hop = int(packet.get("nextHop", 0))
    mesh_packet.channel = int(packet.get("channel", 0))
    decoded = packet.get("decoded")
    if isinstance(decoded, Mapping):
        if "requestId" in decoded:
            mesh_packet.decoded.request_id = int(decoded["requestId"])
        if "wantResponse" in decoded:
            mesh_packet.decoded.want_response = bool(decoded["wantResponse"])
        if "bitfield" in decoded:
            # Data.bitfield is proto3-optional.  Assign even zero so presence is
            # retained: firmware 2.8 uses it to distinguish a valid modern
            # hop_start=0 packet from legacy packets that omitted hop_start.
            mesh_packet.decoded.bitfield = int(decoded["bitfield"])
    return mesh_packet
