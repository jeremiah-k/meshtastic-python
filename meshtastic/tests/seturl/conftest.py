# pylint: disable=redefined-outer-name
"""Shared fixtures and helpers for seturl runtime tests."""

import base64
import threading
from collections.abc import Callable
from typing import NoReturn
from unittest.mock import MagicMock

import pytest

from meshtastic.node_runtime.seturl_runtime import (
    _SetUrlCacheManager,
    _SetUrlExecutionEngine,
)
from meshtastic.protobuf import (
    apponly_pb2,
    channel_pb2,
    localonly_pb2,
)


def _make_raise_error_side_effect() -> Callable[[str], NoReturn]:
    """Create a side_effect callable that raises ValueError."""

    def raise_error(msg: str) -> NoReturn:
        raise ValueError(msg)

    return raise_error


def _make_channel(
    index: int,
    role: int,
    name: str = "",
    psk: bytes = b"",
) -> channel_pb2.Channel:
    """Create a Channel protobuf with the given index, role, name, and psk.

    Parameters
    ----------
    index : int
        Channel index.
    role : int
        Channel role (PRIMARY=1, SECONDARY=2, DISABLED=0).
    name : str
        Optional channel settings name.
    psk : bytes
        Optional channel settings psk.

    Returns
    -------
    channel_pb2.Channel
        A configured Channel instance.
    """
    channel = channel_pb2.Channel(index=index, role=role)  # type: ignore[arg-type]  # pyright: ignore[reportArgumentType]
    if name:
        channel.settings.name = name
    if psk:
        channel.settings.psk = psk
    return channel


def _make_valid_channel_set_url(channel_name: str = "test") -> str:
    """Create a valid channel set URL for testing.

    Parameters
    ----------
    channel_name : str
        Name for the test channel.

    Returns
    -------
    str
        A valid meshtastic URL with encoded ChannelSet.
    """
    channel_set = apponly_pb2.ChannelSet()
    settings = channel_set.settings.add()
    settings.name = channel_name
    settings.psk = b"\x01"
    encoded = base64.urlsafe_b64encode(channel_set.SerializeToString()).decode("utf-8")
    return f"https://meshtastic.org/e/#{encoded.rstrip('=')}"


def _make_channel_set_with_lora(channel_name: str = "test") -> apponly_pb2.ChannelSet:
    """Create a ChannelSet with LoRa config for testing.

    Parameters
    ----------
    channel_name : str
        Name for the test channel.

    Returns
    -------
    apponly_pb2.ChannelSet
        A ChannelSet with channel settings and LoRa config.
    """
    channel_set = apponly_pb2.ChannelSet()
    settings = channel_set.settings.add()
    settings.name = channel_name
    settings.psk = b"\x01"
    channel_set.lora_config.hop_limit = 3
    return channel_set


@pytest.fixture
def mock_iface() -> MagicMock:
    """Create a minimal mock interface.

    Returns
    -------
    MagicMock
        A mock interface with localNode attribute.
    """
    iface = MagicMock(spec=["localNode"])
    iface.localNode = None
    return iface


@pytest.fixture
def mock_local_node(mock_iface: MagicMock) -> MagicMock:
    """Create a mock local node for setURL testing.

    Parameters
    ----------
    mock_iface : MagicMock
        The mock interface fixture.

    Returns
    -------
    MagicMock
        A mock node configured as a local node with all required attributes.
    """
    node = MagicMock(
        spec=[
            "nodeNum",
            "iface",
            "noProto",
            "_channels_lock",
            "channels",
            "partialChannels",
            "localConfig",
            "_raise_interface_error",
            "_write_channel_snapshot",
            "_send_admin",
            "ensureSessionKey",
            "_get_admin_channel_index",
            "_get_named_admin_channel_index",
            "_execute_with_node_db_lock",
            "getURL",
        ]
    )
    node.nodeNum = 1234567890
    node.iface = mock_iface
    node.noProto = False
    node._channels_lock = threading.RLock()
    node.channels = None
    node.partialChannels = []
    node.localConfig = localonly_pb2.LocalConfig()
    node._raise_interface_error = MagicMock(side_effect=Exception("interface error"))
    node._write_channel_snapshot = MagicMock()
    node._send_admin = MagicMock()
    node.ensureSessionKey = MagicMock()
    node._get_admin_channel_index = MagicMock(return_value=0)
    node._get_named_admin_channel_index = MagicMock(return_value=None)
    node._execute_with_node_db_lock = MagicMock(side_effect=lambda func: func())
    node.getURL = MagicMock(return_value="https://meshtastic.org/e/#test")

    mock_iface.localNode = node
    return node


@pytest.fixture
def cache_manager(mock_local_node: MagicMock) -> _SetUrlCacheManager:
    """Provide a _SetUrlCacheManager instance bound to the mock node.

    Parameters
    ----------
    mock_local_node : MagicMock
        The mock node fixture.

    Returns
    -------
    _SetUrlCacheManager
        The cache manager instance under test.
    """
    return _SetUrlCacheManager(mock_local_node)


@pytest.fixture
def execution_engine(
    mock_local_node: MagicMock, cache_manager: _SetUrlCacheManager
) -> _SetUrlExecutionEngine:
    """Provide a _SetUrlExecutionEngine instance.

    Parameters
    ----------
    mock_local_node : MagicMock
        The mock node fixture.
    cache_manager : _SetUrlCacheManager
        The cache manager fixture.

    Returns
    -------
    _SetUrlExecutionEngine
        The execution engine instance under test.
    """
    return _SetUrlExecutionEngine(mock_local_node, cache_manager=cache_manager)


@pytest.fixture
def mock_iface_with_reconnect() -> MagicMock:
    """Create a mock interface that simulates reconnect behavior.

    Returns
    -------
    MagicMock
        A mock interface with isConnected Event that can be controlled.
    """
    iface = MagicMock(
        spec=[
            "localNode",
            "isConnected",
            "waitForConfig",
            "connect",
            "_attempt_reconnect",
        ]
    )
    iface.localNode = None
    iface.isConnected = threading.Event()
    iface.isConnected.set()
    iface.waitForConfig = MagicMock()
    iface.connect = MagicMock()
    iface._attempt_reconnect = MagicMock(return_value=False)
    return iface


@pytest.fixture
def mock_local_node_with_reconnect(
    mock_iface_with_reconnect: MagicMock,
) -> MagicMock:
    """Create a mock local node with reconnect-capable interface.

    Parameters
    ----------
    mock_iface_with_reconnect : MagicMock
        The mock interface fixture with reconnect behavior.

    Returns
    -------
    MagicMock
        A mock node configured for reconnect testing.
    """
    node = MagicMock(
        spec=[
            "nodeNum",
            "iface",
            "noProto",
            "_channels_lock",
            "channels",
            "partialChannels",
            "localConfig",
            "_raise_interface_error",
            "_write_channel_snapshot",
            "_send_admin",
            "ensureSessionKey",
            "_get_admin_channel_index",
            "_get_named_admin_channel_index",
            "_execute_with_node_db_lock",
            "getURL",
        ]
    )
    node.nodeNum = 1234567890
    node.iface = mock_iface_with_reconnect
    node.noProto = False
    node._channels_lock = threading.RLock()
    node.channels = None
    node.partialChannels = []
    node.localConfig = localonly_pb2.LocalConfig()
    node._raise_interface_error = MagicMock(side_effect=Exception("interface error"))
    node._write_channel_snapshot = MagicMock()
    node._send_admin = MagicMock()
    node.ensureSessionKey = MagicMock()
    node._get_admin_channel_index = MagicMock(return_value=0)
    node._get_named_admin_channel_index = MagicMock(return_value=None)
    node._execute_with_node_db_lock = MagicMock(side_effect=lambda func: func())
    node.getURL = MagicMock(return_value="https://meshtastic.org/e/#test")

    mock_iface_with_reconnect.localNode = node
    return node
