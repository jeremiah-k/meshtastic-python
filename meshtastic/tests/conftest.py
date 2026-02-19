"""Common pytest code (place for fixtures)."""

import argparse
from typing import Callable, ClassVar, List, Type
from unittest.mock import MagicMock

import pytest

from meshtastic import mt_config

from ..mesh_interface import MeshInterface


def _create_context_manager_mock(spec_class: Type) -> MagicMock:
    """
    Create a MagicMock that supports the context manager protocol for the given spec class.
    
    Parameters:
        spec_class (Type): Class used as the mock's spec (e.g., SerialInterface).
    
    Returns:
        MagicMock: A mock configured so its `__enter__` returns the mock itself and its `__exit__` returns None.
    """
    mock = MagicMock(spec=spec_class)
    mock.__enter__ = MagicMock(return_value=mock)
    mock.__exit__ = MagicMock(return_value=None)
    return mock


class FakeTimer:
    """Simple timer stub for heartbeat timer tests."""

    created: ClassVar[List["FakeTimer"]] = []

    def __init__(self, interval: float, function: Callable[[], None]) -> None:
        """
        Create a FakeTimer and record it in FakeTimer.created for test inspection.
        
        Parameters:
            interval (float): Time interval in seconds that the timer represents.
            function (Callable[[], None]): Callback that would be invoked when a real timer fires.
        """
        self.interval = interval
        self.function = function
        self.daemon = False
        self.started = False
        self.cancelled = False
        FakeTimer.created.append(self)

    def start(self) -> None:
        """Mark the FakeTimer as started.
        
        The timer's callback is not executed automatically. To invoke the callback in a test, call `FakeTimer.created[i].function()` directly.
        """
        self.started = True

    def cancel(self) -> None:
        """
        Mark the fake timer as cancelled.
        
        Sets the instance's `cancelled` flag to True so tests can detect that the timer was cancelled.
        """
        self.cancelled = True


@pytest.fixture(name="fake_timer_cls")
def _fake_timer_cls_fixture(monkeypatch: pytest.MonkeyPatch) -> Type["FakeTimer"]:
    """
    Replace meshtastic.mesh_interface.threading.Timer with a deterministic FakeTimer for tests.

    Clears any previously created FakeTimer instances and installs FakeTimer in place of threading.Timer so tests can control timer behavior.

    Returns:
        The FakeTimer class that was installed.

    """
    FakeTimer.created.clear()
    monkeypatch.setattr("meshtastic.mesh_interface.threading.Timer", FakeTimer)
    return FakeTimer


@pytest.fixture
def reset_mt_config():
    """
    Reset the global mt_config state and install a fresh ArgumentParser for tests.
    
    Creates a new argparse.ArgumentParser with add_help=False, calls mt_config.reset(), and assigns the new parser to mt_config.parser so tests start with a clean configuration.
    """
    parser = None
    parser = argparse.ArgumentParser(add_help=False)
    mt_config.reset()
    mt_config.parser = parser


@pytest.fixture
def iface_with_nodes():
    """
    Provide a MeshInterface pre-populated with a sample node and a mocked myInfo.
    
    The returned MeshInterface has `nodes` and `nodesByNum` populated with a single node (numeric id 2475227164). `myInfo` is a MagicMock and its `my_node_num` attribute is set to 2475227164.
    
    Returns:
        MeshInterface: Instance with prepared node dictionaries and a mocked `myInfo`.
    """
    nodesById = {
        "!9388f81c": {
            "num": 2475227164,
            "user": {
                "id": "!9388f81c",
                "longName": "Unknown f81c",
                "shortName": "?1C",
                "macaddr": "RBeTiPgc",
                "hwModel": "TBEAM",
            },
            "position": {},
            "lastHeard": 1640204888,
        }
    }

    nodesByNum = {
        2475227164: {
            "num": 2475227164,
            "user": {
                "id": "!9388f81c",
                "longName": "Unknown f81c",
                "shortName": "?1C",
                "macaddr": "RBeTiPgc",
                "hwModel": "TBEAM",
            },
            "position": {"time": 1640206266},
            "lastHeard": 1640206266,
        }
    }
    iface = MeshInterface(noProto=True)
    iface.nodes = nodesById
    iface.nodesByNum = nodesByNum
    myInfo = MagicMock()
    iface.myInfo = myInfo
    iface.myInfo.my_node_num = 2475227164
    return iface


@pytest.fixture
def mock_serial_interface() -> MagicMock:
    """
    Provide a mock SerialInterface configured for node-related tests.
    
    Configured behavior:
    - localNode.getChannelByName returns None
    - myInfo.max_channels is set to 8
    
    Returns:
        MagicMock: A mock acting like a SerialInterface with the above attributes.
    """
    mock_iface = MagicMock()
    mock_iface.localNode.getChannelByName.return_value = None
    mock_iface.myInfo.max_channels = 8
    return mock_iface