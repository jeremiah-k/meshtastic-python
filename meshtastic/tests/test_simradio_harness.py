"""Unit coverage for simradio orchestration without launching firmware."""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from pubsub import pub

from meshtastic.protobuf import mesh_pb2, portnums_pb2
from meshtastic.tcp_interface import TCPInterface

from . import simradio_harness, simradio_helpers
from .simradio_harness import (
    CHAIN_TOPOLOGY,
    SimMesh,
    SimNode,
    _build_mesh_packet,
    _copy_channel_topology,
    _ensure_port_available,
    _inject_simulator_packet,
    find_meshtasticd,
)
from .simradio_helpers import PacketCollector, cli_then_verify, run_cli


@pytest.mark.unit
def test_simradio_topology_is_defensively_copied_and_validated() -> None:
    """Caller mutations must not alter a validated topology snapshot."""
    source = {0: {1}, 1: {0}}
    copied = _copy_channel_topology(source, 2)
    source[0].clear()

    assert copied == {0: frozenset({1}), 1: frozenset({0})}
    with pytest.raises(ValueError, match="cannot receive its own"):
        _copy_channel_topology({0: {0}}, 1)
    with pytest.raises(ValueError, match="receiver indices out of range"):
        _copy_channel_topology({0: {2}}, 2)
    with pytest.raises(ValueError, match="transmitter index out of range"):
        _copy_channel_topology({2: {0}}, 2)


@pytest.mark.unit
def test_simradio_mesh_receiver_selection_is_deterministic() -> None:
    """Full-mesh and chain receiver lists should be stable and exclude self."""
    full = SimMesh(node_count=3)
    chain = SimMesh(node_count=3, topology=CHAIN_TOPOLOGY)

    assert full._receiver_indices(1) == (0, 2)
    assert chain._receiver_indices(0) == (1,)
    assert chain._receiver_indices(1) == (0, 2)
    assert chain._receiver_indices(2) == (1,)


@pytest.mark.unit
def test_build_simradio_mesh_packet_preserves_routing_metadata() -> None:
    """Simulator reconstruction should retain all routing and request fields."""
    packet = _build_mesh_packet(
        {
            "from": 11,
            "to": 22,
            "id": 33,
            "wantAck": True,
            "hopLimit": 4,
            "hopStart": 5,
            "viaMQTT": True,
            "relayNode": 6,
            "nextHop": 7,
            "channel": 2,
            "decoded": {"requestId": 44, "wantResponse": True},
        },
        b"payload",
    )

    assert getattr(packet, "from") == 11
    assert packet.to == 22
    assert packet.id == 33
    assert packet.want_ack is True
    assert packet.hop_limit == 4
    assert packet.hop_start == 5
    assert packet.via_mqtt is True
    assert packet.relay_node == 6
    assert packet.next_hop == 7
    assert packet.channel == 2
    assert packet.decoded.portnum == portnums_pb2.PortNum.SIMULATOR_APP
    assert packet.decoded.payload == b"payload"
    assert packet.decoded.request_id == 44
    assert packet.decoded.want_response is True


@pytest.mark.unit
def test_simradio_bridge_forwards_independent_packet_copies() -> None:
    """Each selected receiver should get an isolated ToRadio message."""
    mesh = SimMesh(node_count=3, topology=CHAIN_TOPOLOGY)
    transmitter = MagicMock(portNumber=mesh.nodes[1].port)
    receiver_a = MagicMock(spec=TCPInterface)
    receiver_c = MagicMock(spec=TCPInterface)
    mesh.nodes[0].iface = receiver_a
    mesh.nodes[2].iface = receiver_c
    mesh._port_to_index[mesh.nodes[1].port] = 1
    mesh._started = True

    mesh._on_sim_packet(
        packet={
            "from": 17,
            "to": 0xFFFFFFFF,
            "id": 99,
            "decoded": {"payload": b"compressed"},
        },
        interface=transmitter,
    )

    receiver_a._send_to_radio.assert_called_once()
    receiver_c._send_to_radio.assert_called_once()
    to_a = receiver_a._send_to_radio.call_args.args[0]
    to_c = receiver_c._send_to_radio.call_args.args[0]
    assert isinstance(to_a, mesh_pb2.ToRadio)
    assert isinstance(to_c, mesh_pb2.ToRadio)
    assert to_a is not to_c
    assert to_a.packet.SerializeToString() == to_c.packet.SerializeToString()


@pytest.mark.unit
def test_simradio_bridge_drops_oversized_payload() -> None:
    """Oversized simulator payloads must never be injected into firmware."""
    mesh = SimMesh(node_count=2)
    transmitter = MagicMock(portNumber=mesh.nodes[0].port)
    receiver = MagicMock(spec=TCPInterface)
    mesh.nodes[1].iface = receiver
    mesh._port_to_index[mesh.nodes[0].port] = 0

    mesh._on_sim_packet(
        packet={
            "decoded": {
                "payload": b"x" * (mesh_pb2.Constants.DATA_PAYLOAD_LEN + 1)
            }
        },
        interface=transmitter,
    )

    receiver._send_to_radio.assert_not_called()


@pytest.mark.unit
def test_simradio_bridge_drops_malformed_routing_metadata() -> None:
    """Invalid decoded routing values must not escape the pubsub callback."""
    mesh = SimMesh(node_count=2)
    transmitter = MagicMock(portNumber=mesh.nodes[0].port)
    receiver = MagicMock(spec=TCPInterface)
    mesh.nodes[1].iface = receiver
    mesh._port_to_index[mesh.nodes[0].port] = 0
    mesh._started = True

    mesh._on_sim_packet(
        packet={"to": "not-a-node-number", "decoded": {"payload": b"payload"}},
        interface=transmitter,
    )

    receiver._send_to_radio.assert_not_called()


@pytest.mark.unit
def test_simradio_injection_accepts_current_upstream_internal_spelling() -> None:
    """Harness adaptation should tolerate upstream's pre-refactor sender name."""
    legacy_iface = MagicMock(spec_set=["_sendToRadio"])
    to_radio = mesh_pb2.ToRadio()

    _inject_simulator_packet(legacy_iface, to_radio)

    legacy_iface._sendToRadio.assert_called_once_with(to_radio)


@pytest.mark.unit
def test_find_meshtasticd_prefers_executable_environment_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """MESHTASTICD_BIN should win when it names an executable file."""
    executable = tmp_path / "meshtasticd"
    executable.write_text("#!/bin/sh\n", encoding="utf-8")
    executable.chmod(0o700)
    monkeypatch.setenv("MESHTASTICD_BIN", str(executable))

    assert find_meshtasticd() == str(executable.resolve())


@pytest.mark.unit
def test_simradio_node_rejects_occupied_port(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Harness startup must not attach to an unrelated existing listener."""
    connected_socket = MagicMock()
    connected_socket.__enter__.return_value = connected_socket
    monkeypatch.setattr(
        simradio_harness.socket,
        "create_connection",
        lambda *_args, **_kwargs: connected_socket,
    )

    with pytest.raises(RuntimeError, match="already in use"):
        _ensure_port_available(44_404)


@pytest.mark.unit
def test_simradio_node_cleans_resources_when_process_launch_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A Popen failure should close logs and remove the temporary VFS."""
    def _fail_launch(*_args: object, **_kwargs: object) -> None:
        raise OSError("simulated launch failure")

    monkeypatch.setattr(simradio_harness, "_ensure_port_available", lambda _port: None)
    monkeypatch.setattr(subprocess, "Popen", _fail_launch)
    node = SimNode(0, base_port=44_405)

    with pytest.raises(RuntimeError, match="failed to start"):
        node.start("/does/not/matter")

    assert node.process is None
    assert node.workdir is None
    assert node.iface is None
    assert node._temporary_directory is None
    assert node._log_files == []


@pytest.mark.unit
def test_run_cli_retries_transient_failure_without_logging_values(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Transient connection failures should retry and return attempt metadata."""
    completed = iter(
        (
            subprocess.CompletedProcess(
                args=[], returncode=1, stdout="Connection refused"
            ),
            subprocess.CompletedProcess(args=[], returncode=0, stdout="ok"),
        )
    )
    monkeypatch.setattr(subprocess, "run", lambda *_args, **_kwargs: next(completed))
    monkeypatch.setattr(simradio_helpers.time, "sleep", lambda _seconds: None)

    with caplog.at_level(logging.DEBUG, logger="meshtastic.tests.simradio_helpers"):
        result = run_cli(
            4404,
            "--set",
            "network.wifi_psk",
            "distinctive-secret",
        )

    assert result.returncode == 0
    assert result.output == "ok"
    assert result.attempts == 2
    assert "distinctive-secret" not in caplog.text


@pytest.mark.unit
def test_cli_failure_diagnostics_redact_positional_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Assertion diagnostics must not echo credentials supplied to the CLI."""
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(
            args=[],
            returncode=1,
            stdout="Rejected distinctive-secret for network.wifi_psk",
        ),
    )

    with pytest.raises(AssertionError) as exc_info:
        cli_then_verify(
            4404,
            ("--set", "network.wifi_psk", "distinctive-secret"),
            None,
        )

    diagnostic = str(exc_info.value)
    assert "distinctive-secret" not in diagnostic
    assert "<redacted>" in diagnostic


@pytest.mark.unit
def test_packet_collectors_unsubscribe_independently() -> None:
    """Closing one collector must not remove another subscriber on the topic."""
    iface = MagicMock(spec=TCPInterface)
    first = PacketCollector(iface, "meshtastic.receive.text")
    second = PacketCollector(iface, "meshtastic.receive.text")
    try:
        first.close()
        pub.sendMessage(
            "meshtastic.receive.text",
            packet={
                "decoded": {
                    "portnum": "TEXT_MESSAGE_APP",
                    "text": "still-subscribed",
                }
            },
            interface=iface,
        )
        assert first.texts == []
        assert second.texts == ["still-subscribed"]
    finally:
        first.close()
        second.close()


@pytest.mark.unit
def test_simradio_stop_cleans_partially_started_nodes() -> None:
    """Stop should close all nodes even before the mesh marks itself started."""
    mesh = SimMesh(node_count=3)
    close_calls: list[int] = []
    for node in mesh.nodes:
        node.close = MagicMock(  # type: ignore[method-assign]
            side_effect=lambda node_id=node.node_id: close_calls.append(node_id)
        )

    mesh.stop()

    assert close_calls == [2, 1, 0]
