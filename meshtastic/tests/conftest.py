"""Common pytest code (place for fixtures)."""

import argparse
from collections.abc import Generator
from typing import Any, Callable, ClassVar, Type
from unittest.mock import MagicMock

import pytest

from meshtastic import mt_config

from ..mesh_interface import MeshInterface
from ..serial_interface import SerialInterface


def _create_context_manager_mock(spec_class: Type) -> MagicMock:
    """Create a MagicMock that behaves as a context manager for the given spec class.

    Parameters
    ----------
    spec_class : Type
        Class to use as the mock's specification (e.g., SerialInterface).

    Returns
    -------
    MagicMock
        A mock whose `__enter__` returns the mock itself and whose `__exit__` returns `None`.
    """
    mock = MagicMock(spec=spec_class)
    mock.__enter__ = MagicMock(return_value=mock)
    mock.__exit__ = MagicMock(return_value=None)
    return mock


class FakeTimer:
    """Simple timer stub for heartbeat timer tests."""

    created: ClassVar[list["FakeTimer"]] = []

    def __init__(
        self,
        interval: float,
        function: Callable[..., Any],
        args: tuple[Any, ...] | None = None,
        kwargs: dict[str, Any] | None = None,
    ) -> None:
        """Initialize a FakeTimer and append it to the class-level `created` list for test inspection.

        Parameters
        ----------
        interval : float
            Time interval in seconds represented by the timer.
        function : Callable[..., Any]
            Callback that would be invoked when a real timer fires.
        args : tuple[Any, ...] | None
            Optional positional arguments for the callback; defaults to empty tuple.
        kwargs : dict[str, Any] | None
            Optional keyword arguments for the callback; defaults to empty dict.
        """
        self.interval = interval
        self.function = function
        self.args = args or ()
        self.kwargs = kwargs or {}
        self.daemon = False
        self.started = False
        self.cancelled = False
        type(self).created.append(self)

    def start(self) -> None:
        """Mark the FakeTimer instance as started.

        The timer's callback is not invoked automatically; to execute it in a test,
            call the stored function directly (for example:
            type(timer).created[i].function()).
        """
        self.started = True

    def cancel(self) -> None:
        """Mark the timer as cancelled by setting its `cancelled` attribute to True."""
        self.cancelled = True


@pytest.fixture(name="fake_timer_cls")
def _fake_timer_cls_fixture(monkeypatch: pytest.MonkeyPatch) -> Type["FakeTimer"]:
    """Install a per-fixture FakeTimer subclass in place of meshtastic.mesh_interface.threading.Timer for use in tests.

    Parameters
    ----------
    monkeypatch : pytest.MonkeyPatch
        Pytest fixture used to replace the real Timer with the fake subclass for the duration of the test.

    Returns
    -------
    Type['FakeTimer']
        The FakeTimer subclass that was installed.
    """

    class FakeTimerForTest(FakeTimer):
        """Per-fixture timer class with isolated created-state."""

        created: ClassVar[list["FakeTimer"]] = []

    monkeypatch.setattr("meshtastic.mesh_interface.threading.Timer", FakeTimerForTest)
    return FakeTimerForTest


@pytest.fixture
def reset_mt_config() -> None:
    """Reset the global mt_config state and install a fresh ArgumentParser for tests.

    Creates a new argparse.ArgumentParser with add_help=False, calls mt_config.reset(), and assigns
    the new parser to mt_config.parser so tests start with a clean configuration.
    """
    parser = argparse.ArgumentParser(add_help=False)
    mt_config.reset()
    mt_config.parser = parser


@pytest.fixture
def iface_with_nodes() -> Generator[MeshInterface, None, None]:
    """Provide a MeshInterface populated with a sample node and a mocked myInfo for tests.

    Yields a MeshInterface whose `nodes` and `nodesByNum` each contain a single node (numeric id 2475227164)
    and whose `myInfo.my_node_num` is set to 2475227164. Ensures the interface is closed when the fixture
    teardown runs.

    Yields
    ------
    MeshInterface
        Interface instance populated with test node data and a mocked myInfo.
    """
    nodes_by_id = {
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

    nodes_by_num = {
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
    try:
        iface.nodes = nodes_by_id
        iface.nodesByNum = nodes_by_num
        myInfo = MagicMock()
        iface.myInfo = myInfo
        iface.myInfo.my_node_num = 2475227164
        yield iface
    finally:
        iface.close()


@pytest.fixture
def mock_serial_interface() -> MagicMock:
    """Create a MagicMock that mimics a SerialInterface for node-related tests.

    The mock supports the context manager protocol, its localNode.getChannelByName returns None,
    and its myInfo.max_channels is set to 8.

    Returns
    -------
    MagicMock
        A mock configured to act like a SerialInterface with the above behavior.
    """
    mock_iface = _create_context_manager_mock(SerialInterface)
    mock_iface.localNode = MagicMock(spec=["getChannelByName"])
    mock_iface.localNode.getChannelByName.return_value = None
    mock_iface.myInfo = MagicMock(spec=["max_channels"])
    mock_iface.myInfo.max_channels = 8
    return mock_iface
