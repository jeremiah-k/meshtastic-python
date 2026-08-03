"""Shared builders for focused CLI validation tests."""

from unittest.mock import MagicMock

from meshtastic.node import Node
from meshtastic.protobuf import channel_pb2, localonly_pb2
from meshtastic.tcp_interface import TCPInterface


def _mock_tcp_interface_with_channels() -> tuple[MagicMock, MagicMock]:
    """Return a mocked TCP interface with a minimally configured node."""
    interface = MagicMock(autospec=TCPInterface)
    interface.__enter__ = MagicMock(return_value=interface)
    interface.__exit__ = MagicMock(return_value=None)
    node = MagicMock(autospec=Node)
    node.localConfig = localonly_pb2.LocalConfig()
    node.moduleConfig = localonly_pb2.LocalModuleConfig()
    node.channels = [channel_pb2.Channel(index=0), channel_pb2.Channel(index=1)]
    interface.getNode.return_value = node
    return interface, node
