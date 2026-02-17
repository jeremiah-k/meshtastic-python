"""Common pytest code (place for fixtures)."""

import argparse
from typing import ClassVar
from unittest.mock import MagicMock

import pytest

from meshtastic import mt_config

from ..mesh_interface import MeshInterface


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
