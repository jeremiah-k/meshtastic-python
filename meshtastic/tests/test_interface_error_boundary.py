"""Compatibility tests for the shared MeshInterface error boundary."""

import pickle

import pytest

from meshtastic._interface_errors import MeshInterfaceError as _MeshInterfaceError
from meshtastic.mesh_interface import MeshInterface
from meshtastic.node import Node


@pytest.mark.unit
def test_mesh_interface_error_preserves_nested_public_identity() -> None:
    """The leaf definition must remain the exact historical nested class object."""
    error_type = MeshInterface.MeshInterfaceError

    assert error_type is _MeshInterfaceError
    assert error_type.__name__ == "MeshInterfaceError"
    assert error_type.__module__ == "meshtastic.mesh_interface"
    assert error_type.__qualname__ == "MeshInterface.MeshInterfaceError"


@pytest.mark.unit
def test_mesh_interface_error_preserves_message_and_pickle_contract() -> None:
    """Moved exception definitions should preserve construction and pickling."""
    original = MeshInterface.MeshInterfaceError("boundary failure")

    restored = pickle.loads(pickle.dumps(original))  # noqa: S301 - trusted local test object

    assert type(restored) is MeshInterface.MeshInterfaceError
    assert restored.message == "boundary failure"
    assert str(restored) == "boundary failure"


@pytest.mark.unit
def test_node_raises_shared_interface_error_without_facade_import() -> None:
    """Node's internal error seam should raise the shared public exception type."""
    node = object.__new__(Node)

    with pytest.raises(MeshInterface.MeshInterfaceError, match="node failure"):
        node._raise_interface_error("node failure")  # noqa: SLF001

@pytest.mark.unit
def test_internal_exception_subclasses_preserve_public_mesh_interface_base() -> None:
    """Internal subclasses should use the leaf class without changing public ancestry."""
    from meshtastic.interfaces.ble.errors import MeshtasticBLEError
    from meshtastic.stream_interface import StreamInterface

    assert issubclass(StreamInterface.StreamInterfaceError, MeshInterface.MeshInterfaceError)
    assert issubclass(StreamInterface.PayloadTooLargeError, MeshInterface.MeshInterfaceError)
    assert issubclass(MeshtasticBLEError, MeshInterface.MeshInterfaceError)
