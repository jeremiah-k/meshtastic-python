"""Common pytest code (place for fixtures)."""

import argparse
from typing import ClassVar, Type
from unittest.mock import MagicMock

import pytest

from meshtastic import mt_config

from ..mesh_interface import MeshInterface


def create_context_manager_mock(spec_class: Type) -> MagicMock:
    """Create a MagicMock that properly supports context manager protocol.

    When using ExitStack.enter_context(), the mock's __enter__ must return self,
    otherwise the returned object is a different mock than what was configured.

    This is needed because the CLI code now uses:
        client = stack.enter_context(SerialInterface(...))

    Without this fix, __enter__ returns a new MagicMock, breaking test assertions.

    Args:
        spec_class: The class to use as spec for the mock (e.g., SerialInterface)

    Returns:
        A MagicMock with __enter__ configured to return itself.
    """
    mock = MagicMock(autospec=spec_class)
    mock.__enter__ = MagicMock(return_value=mock)
    mock.__exit__ = MagicMock(return_value=None)
    return mock


class FakeTimer:
    """Simple timer stub for heartbeat timer tests."""

    created: ClassVar[list["FakeTimer"]] = []

    def __init__(self, interval, function):
        """
        Initialize a FakeTimer instance and register it in the class registry.

        Parameters
        ----------
            interval (float): Time interval in seconds for the timer.
            function (Callable): Callback to be invoked when the timer fires.

        Notes
        -----
            The new instance is appended to FakeTimer.created for test inspection.

        """
        self.interval = interval
        self.function = function
        self.daemon = False
        self.started = False
        self.cancelled = False
        FakeTimer.created.append(self)

    def start(self):
        """Record that the fake timer was started."""
        self.started = True

    def cancel(self):
        """
        Mark the fake timer as cancelled.

        Sets the instance's cancelled flag to True so tests can detect that the timer has been cancelled.
        """
        self.cancelled = True


@pytest.fixture(name="fake_timer_cls")
def _fake_timer_cls_fixture(monkeypatch):
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
    """Fixture to reset mt_config."""
    parser = None
    parser = argparse.ArgumentParser(add_help=False)
    mt_config.reset()
    mt_config.parser = parser


@pytest.fixture
def iface_with_nodes():
    """Fixture to setup some nodes."""
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
