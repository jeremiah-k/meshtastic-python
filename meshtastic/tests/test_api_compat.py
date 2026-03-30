"""API-compatibility tests for the Meshtastic Python library public surface.

These tests verify the public API shape has not regressed relative to
develop/master.  They catch signature changes (like the writeChannel()
positional-argument issue) and accidental removals of public methods.
"""

import inspect

import pytest

from meshtastic.mesh_interface import MeshInterface
from meshtastic.node import MAX_CHANNELS, MAX_LONG_NAME_LEN, MAX_SHORT_NAME_LEN, Node

pytestmark = pytest.mark.unit

# ---------------------------------------------------------------------------
# Node.writeChannel signature
# ---------------------------------------------------------------------------


def test_node_writechannel_signature() -> None:
    """WriteChannel must accept positional second arg for adminIndex."""
    sig = inspect.signature(Node.writeChannel)
    params = list(sig.parameters.values())

    # channelIndex must be first positional-or-keyword (index 0 is 'self')
    ch = params[1]
    assert ch.name == "channelIndex"
    assert ch.kind == inspect.Parameter.POSITIONAL_OR_KEYWORD

    # adminIndex must accept positional call: node.writeChannel(3, 1)
    admin = params[2]
    assert admin.name == "adminIndex"
    assert admin.kind in (
        inspect.Parameter.POSITIONAL_OR_KEYWORD,
        inspect.Parameter.POSITIONAL_ONLY,
    )


# ---------------------------------------------------------------------------
# Node public method existence
# ---------------------------------------------------------------------------


def test_node_public_methods_exist() -> None:
    """Key Node methods must exist."""
    required_methods = [
        "writeChannel",
        "writeConfig",
        "getChannelByChannelIndex",
        "getChannelCopyByChannelIndex",
        "getChannelByName",
        "getMetadata",
        "setURL",
        "getURL",
        "showChannels",
        "showInfo",
        "requestChannels",
        "requestConfig",
        "deleteChannel",
        "setOwner",
        "reboot",
        "shutdown",
        "factoryReset",
        "getAdminChannelIndex",
    ]
    for method in required_methods:
        assert hasattr(Node, method), f"Node.{method} must exist"


# ---------------------------------------------------------------------------
# MeshInterface public method existence
# ---------------------------------------------------------------------------


def test_meshinterface_public_methods_exist() -> None:
    """Key MeshInterface methods must exist."""
    required_methods = [
        "sendText",
        "sendData",
        "sendPosition",
        "sendTelemetry",
        "sendTraceRoute",
        "sendAlert",
        "getNode",
        "close",
        "showInfo",
        "showNodes",
        "getMyNodeInfo",
        "getMyUser",
        "getLongName",
        "getShortName",
        "waitForConfig",
        "waitForAckNak",
    ]
    for method in required_methods:
        assert hasattr(MeshInterface, method), f"MeshInterface.{method} must exist"


# ---------------------------------------------------------------------------
# Module-level constants
# ---------------------------------------------------------------------------


def test_node_exports_constants() -> None:
    """Exported constants must be present and be positive integers."""
    assert isinstance(MAX_CHANNELS, int)
    assert MAX_CHANNELS > 0
    assert isinstance(MAX_LONG_NAME_LEN, int)
    assert MAX_LONG_NAME_LEN > 0
    assert isinstance(MAX_SHORT_NAME_LEN, int)
    assert MAX_SHORT_NAME_LEN > 0


# ---------------------------------------------------------------------------
# No unexpected positional-only params
# ---------------------------------------------------------------------------


def test_no_unexpected_positional_only_in_node() -> None:
    """Node public methods should not have positional-only parameters.

    Positional-only params break callers that pass keyword arguments,
    which is the common pattern in user code (e.g. writeChannel(channelIndex=3)).
    """
    for name in dir(Node):
        if name.startswith("_"):
            continue
        attr = getattr(Node, name, None)
        if not callable(attr):
            continue
        try:
            sig = inspect.signature(attr)
        except (ValueError, TypeError):
            continue
        for pname, param in sig.parameters.items():
            if pname == "self":
                continue
            assert param.kind != inspect.Parameter.POSITIONAL_ONLY, (
                f"Node.{name}() has positional-only param '{pname}' — "
                "this breaks keyword-arg callers"
            )


def test_no_unexpected_positional_only_in_meshinterface() -> None:
    """MeshInterface public methods should not have positional-only parameters."""
    for name in dir(MeshInterface):
        if name.startswith("_"):
            continue
        attr = getattr(MeshInterface, name, None)
        if not callable(attr):
            continue
        try:
            sig = inspect.signature(attr)
        except (ValueError, TypeError):
            continue
        for pname, param in sig.parameters.items():
            if pname == "self":
                continue
            assert param.kind != inspect.Parameter.POSITIONAL_ONLY, (
                f"MeshInterface.{name}() has positional-only param '{pname}' — "
                "this breaks keyword-arg callers"
            )
