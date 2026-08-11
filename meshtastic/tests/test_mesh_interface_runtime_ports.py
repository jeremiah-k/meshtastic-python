"""Behavioral tests for narrow MeshInterface runtime capability ports."""

from unittest.mock import MagicMock

import pytest

from meshtastic.mesh_interface import MeshInterface
from meshtastic.mesh_interface_runtime.ports import (
    _interface_error_type,
    _NodeViewPort,
    _ReceivePipelinePort,
    _SendPipelinePort,
)
from meshtastic.protobuf import mesh_pb2


@pytest.mark.unit
def test_interface_error_type_prefers_concrete_instance_override() -> None:
    """Compatibility doubles can provide their own concrete interface error type."""

    class TestInterfaceError(Exception):
        """Test-only interface error."""

    interface = MagicMock()
    interface.MeshInterfaceError = TestInterfaceError

    assert _interface_error_type(interface) is TestInterfaceError


@pytest.mark.unit
def test_interface_error_type_supports_spec_based_interface_double() -> None:
    """Spec-based mocks retain the facade class error contract."""
    interface = MagicMock(spec=MeshInterface)

    assert _interface_error_type(interface) is MeshInterface.MeshInterfaceError


@pytest.mark.unit
def test_interface_error_type_rejects_collaborator_without_error_contract() -> None:
    """Missing MeshInterfaceError should fail at the port boundary, not at first use."""

    class IncompleteInterface:
        """Test collaborator without the required error type."""

    with pytest.raises(TypeError, match="MeshInterfaceError"):
        _interface_error_type(IncompleteInterface())


@pytest.mark.unit
def test_send_port_defaults_missing_no_proto_to_false() -> None:
    """Compatibility doubles may omit the instance-only ``noProto`` attribute."""
    interface = MagicMock(spec=MeshInterface)

    assert _SendPipelinePort(interface).no_proto is False


@pytest.mark.unit
def test_receive_port_ignores_missing_bootstrap_recorder_on_loose_mock() -> None:
    """A loose test double must not fabricate a callable bootstrap recorder."""
    interface = MagicMock()
    interface.MeshInterfaceError = MeshInterface.MeshInterfaceError

    assert _ReceivePipelinePort(interface).record_bootstrap_decode_error() == 0


@pytest.mark.unit
def test_receive_port_completes_config_through_facade_seams() -> None:
    """Configuration completion installs channels before publishing connection state."""
    interface = MagicMock()
    channels = [MagicMock()]

    _ReceivePipelinePort(interface).complete_config(channels)

    interface.localNode.setChannels.assert_called_once_with(channels)
    interface._connected.assert_called_once_with()


@pytest.mark.unit
def test_send_port_preserves_facade_send_packet_monkeypatch_seam() -> None:
    """The send port delegates packet transmission through ``_send_packet``."""
    interface = MagicMock()
    sent = mesh_pb2.MeshPacket(id=42)
    interface._send_packet.return_value = sent
    packet = mesh_pb2.MeshPacket(id=41)

    result = _SendPipelinePort(interface).send_packet(
        packet,
        "!12345678",
        want_ack=True,
        hop_limit=5,
        pki_encrypted=False,
        public_key=b"key",
    )

    assert result is sent
    interface._send_packet.assert_called_once_with(
        packet,
        "!12345678",
        wantAck=True,
        hopLimit=5,
        pkiEncrypted=False,
        publicKey=b"key",
    )


@pytest.mark.unit
def test_node_view_port_defaults_missing_no_proto_to_false() -> None:
    """Compatibility doubles without ``noProto`` retain the historical default."""
    interface = MagicMock(spec=MeshInterface)

    assert _NodeViewPort(interface).no_proto is False
