"""Common pytest code (place for fixtures)."""

# pylint: disable=redefined-outer-name
# Pytest fixtures use parameter injection which pylint sees as redefinition.

import argparse
import copy
import importlib
import math
import shutil
import threading
import time
from collections.abc import Callable, Generator
from typing import TYPE_CHECKING, Any, ClassVar, NoReturn, cast
from unittest.mock import MagicMock, create_autospec, patch

import pytest

from meshtastic import mt_config, publishingThread
from meshtastic.powermon import power_supply as power_supply_module
from meshtastic.util import DeferredExecution

from ..mesh_interface import MeshInterface
from ..serial_interface import SerialInterface

if TYPE_CHECKING:
    from meshtastic.powermon.power_supply import PowerSupply
    from meshtastic.powermon.ppk2 import PPK2PowerSupply
    from meshtastic.powermon.riden import RidenPowerSupply
    from meshtastic.powermon.stress import PowerStressClient


def pytest_addoption(  # pylint: disable=unused-argument
    parser: pytest.Parser, pluginmanager: pytest.PytestPluginManager
) -> None:
    """Add custom command line options for baseline tests.

    Parameters
    ----------
    parser : pytest.Parser
        The pytest argument parser to add options to.
    pluginmanager : pytest.PytestPluginManager
        The pytest plugin manager (unused but required by hook signature).
    """
    parser.addoption(
        "--update-baselines",
        action="store_true",
        default=False,
        help="Update stored API baselines to match current code",
    )


_MT_CONFIG_SENTINEL = object()
_OPTIONAL_ANALYSIS_DEPS = {
    "dash",
    "dash_bootstrap_components",
    "matplotlib",
    "numpy",
    "pandas",
    "plotly",
    "pyarrow",
}


def _create_context_manager_mock(spec_class: type[Any]) -> MagicMock:
    """Create a MagicMock that behaves as a context manager for the given spec class.

    Parameters
    ----------
    spec_class : type[Any]
        Class to use as the mock's specification (e.g., SerialInterface).

    Returns
    -------
    MagicMock
        A mock whose `__enter__` returns the mock itself and whose `__exit__` returns `None`.
    """
    mock = create_autospec(spec_class, instance=True)
    mock.__enter__ = MagicMock(return_value=mock)
    mock.__exit__ = MagicMock(return_value=None)
    return cast(MagicMock, mock)


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
def _fake_timer_cls_fixture(monkeypatch: pytest.MonkeyPatch) -> type["FakeTimer"]:
    """Install a per-fixture FakeTimer subclass in place of meshtastic.mesh_interface.threading.Timer for use in tests.

    Parameters
    ----------
    monkeypatch : pytest.MonkeyPatch
        Pytest fixture used to replace the real Timer with the fake subclass for the duration of the test.

    Returns
    -------
    type['FakeTimer']
        The FakeTimer subclass that was installed.
    """

    class FakeTimerForTest(FakeTimer):
        """Per-fixture timer class with isolated created-state."""

        created: ClassVar[list["FakeTimer"]] = []

    monkeypatch.setattr("meshtastic.mesh_interface.threading.Timer", FakeTimerForTest)
    return FakeTimerForTest


@pytest.fixture
def reset_mt_config(monkeypatch: pytest.MonkeyPatch) -> None:
    """Reset the global mt_config state and install a fresh ArgumentParser for tests.

    Creates a new argparse.ArgumentParser with add_help=False, calls mt_config.reset(), and assigns
    the new parser to mt_config.parser so tests start with a clean configuration.
    """
    parser = argparse.ArgumentParser(add_help=False)
    mt_config.reset()
    monkeypatch.setattr(mt_config, "parser", parser)


@pytest.fixture
def mt_config_state() -> Generator[None, None, None]:
    """Snapshot and restore mt_config module state mutated by tests."""
    state_keys = tuple(mt_config.MODULE_STATE_DEFAULTS.keys())
    snapshot: dict[str, Any] = {}
    for key in state_keys:
        value = getattr(mt_config, key, _MT_CONFIG_SENTINEL)
        if value is _MT_CONFIG_SENTINEL:
            snapshot[key] = value
            continue
        try:
            snapshot[key] = copy.deepcopy(value)
        except (TypeError, copy.Error):
            snapshot[key] = value
    # Also snapshot and restore warn-once tracking for deprecation warnings
    # Clear before yield so each test starts with fresh warn-once state
    warned_deprecations_lock = getattr(mt_config, "_warned_deprecations_lock", None)
    warned_deprecations: Any = getattr(mt_config, "_warned_deprecations", set())
    if warned_deprecations_lock is not None:
        with cast(Any, warned_deprecations_lock):
            if isinstance(warned_deprecations, set):
                try:
                    warned_snapshot: set[str] = copy.deepcopy(warned_deprecations)
                except (TypeError, copy.Error):
                    warned_snapshot = set(warned_deprecations)
                # Ensure each test starts from a clean warn-once registry
                warned_deprecations.clear()
            else:
                warned_snapshot = set()
                mt_config._warned_deprecations = set()
    elif isinstance(warned_deprecations, set):
        try:
            warned_snapshot = copy.deepcopy(warned_deprecations)
        except (TypeError, copy.Error):
            warned_snapshot = set(warned_deprecations)
        warned_deprecations.clear()
    else:
        warned_snapshot = set()
        mt_config._warned_deprecations = set()
    try:
        yield
    finally:
        for key, value in snapshot.items():
            if value is _MT_CONFIG_SENTINEL:
                if hasattr(mt_config, key):
                    delattr(mt_config, key)
            else:
                setattr(mt_config, key, value)
        warned_deprecations_restore = getattr(mt_config, "_warned_deprecations", None)
        warned_deprecations_lock_restore = getattr(
            mt_config, "_warned_deprecations_lock", None
        )
        if warned_deprecations_lock_restore is not None:
            with cast(Any, warned_deprecations_lock_restore):
                if isinstance(warned_deprecations_restore, set):
                    warned_deprecations_restore.clear()
                    warned_deprecations_restore.update(warned_snapshot)
                else:
                    mt_config._warned_deprecations = set(warned_snapshot)
        elif isinstance(warned_deprecations_restore, set):
            warned_deprecations_restore.clear()
            warned_deprecations_restore.update(warned_snapshot)
        else:
            mt_config._warned_deprecations = set(warned_snapshot)


@pytest.fixture
def cli_exit_capture(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Patch analysis_main._cli_exit and return a capture dict for message/code assertions."""
    try:
        analysis_main = importlib.import_module("meshtastic.analysis.__main__")
    except ModuleNotFoundError as exc:
        missing = (exc.name or "").split(".", maxsplit=1)[0]
        if missing in _OPTIONAL_ANALYSIS_DEPS:
            pytest.skip("Can't import meshtastic.analysis")
        raise

    captured: dict[str, Any] = {}

    def _fake_cli_exit(message: str, return_value: int = 1) -> NoReturn:
        captured["message"] = message
        captured["code"] = return_value
        raise SystemExit(return_value)

    monkeypatch.setattr(analysis_main, "_cli_exit", _fake_cli_exit)
    return captured


@pytest.fixture
def reset_power_supply_deprecations() -> Generator[None, None, None]:
    """Reset and restore powermon deprecation warn-once state for isolated tests."""
    warned_deprecations = power_supply_module._warned_deprecations
    warned_lock = getattr(power_supply_module, "_warned_deprecations_lock", None)
    if warned_lock is not None:
        with cast(Any, warned_lock):
            previous = set(warned_deprecations)
            warned_deprecations.clear()
    else:
        previous = set(warned_deprecations)
        warned_deprecations.clear()
    try:
        yield
    finally:
        if warned_lock is not None:
            with cast(Any, warned_lock):
                warned_deprecations.clear()
                warned_deprecations.update(previous)
        else:
            warned_deprecations.clear()
            warned_deprecations.update(previous)


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
            "position": {"time": 1640206266},
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
    mock_iface.localNode = MagicMock(spec=["getChannelByName", "getChannelCopyByName"])
    mock_iface.localNode.getChannelByName.return_value = None
    mock_iface.localNode.getChannelCopyByName.return_value = None
    mock_iface.myInfo = MagicMock(spec=["max_channels"])
    mock_iface.myInfo.max_channels = 8
    return mock_iface


@pytest.fixture
def autospec_local_node_iface() -> Callable[[type[Any]], MagicMock]:
    """Provide a factory for creating autospecced interface mocks with localNode.

    Returns
    -------
    Callable[[type[Any]], MagicMock]
        A factory function that takes a spec class (e.g., MeshInterface, SerialInterface)
        and returns an autospecced mock with a localNode attribute configured with
        _get_admin_channel_index returning 0, _get_named_admin_channel_index
        returning None, and getAdminChannelIndex returning 0.
    """

    def _factory(spec_class: type[Any]) -> MagicMock:
        iface = create_autospec(spec_class, instance=True)
        local_node = MagicMock(
            spec=[
                "_get_admin_channel_index",
                "_get_named_admin_channel_index",
                "getAdminChannelIndex",
            ]
        )
        local_node._get_admin_channel_index.return_value = 0
        local_node._get_named_admin_channel_index.return_value = None
        local_node.getAdminChannelIndex.return_value = 0
        iface.localNode = local_node
        return cast(MagicMock, iface)

    return _factory


@pytest.fixture
def power_supply() -> "PowerSupply":
    """Provide a fresh PowerSupply instance for voltage-validation tests."""
    from meshtastic.powermon.power_supply import PowerSupply  # pylint: disable=C0415

    return PowerSupply()


@pytest.fixture
def riden_stub() -> "RidenPowerSupply":
    """Create a minimally initialized RidenPowerSupply test instance."""
    from meshtastic.powermon.riden import RidenPowerSupply  # pylint: disable=C0415

    # Bypass __init__ to avoid opening a real serial port during unit tests.
    pps = object.__new__(RidenPowerSupply)
    pps.r = MagicMock()
    pps.prevPowerTime = time.monotonic() - 10.0
    pps.prevWattHour = 100.0
    pps.v = 3.3
    return pps


@pytest.fixture
def ppk2_stub() -> "PPK2PowerSupply":
    """Create a minimally initialized PPK2PowerSupply test instance."""
    from meshtastic.powermon.ppk2 import PPK2PowerSupply  # pylint: disable=C0415

    # Bypass __init__ to avoid starting the background measurement thread in tests.
    ppk = object.__new__(PPK2PowerSupply)
    ppk.r = MagicMock()
    ppk.measuring = False
    ppk.measurement_thread = threading.Thread(
        target=lambda: None, daemon=True, name="ppk2 test thread"
    )
    ppk._v = 3.3
    ppk_any = cast(Any, ppk)
    ppk_any._is_supply = False
    ppk_any._closed = False
    ppk_any._shutdown_event = threading.Event()
    # Keep both names initialized: legacy code paths may still touch
    # measurement_thread while current code uses _measurement_thread.
    ppk_any._measurement_thread = None
    ppk._result_lock = threading.Condition()
    ppk._want_measurement = threading.Condition()
    ppk._measurement_state_lock = threading.Lock()
    ppk.current_sum = 0
    ppk.current_num_samples = 0
    ppk.current_min = 0
    ppk.current_max = 0
    ppk.current_average = math.nan
    ppk.last_reported_min = math.nan
    ppk.last_reported_max = math.nan
    ppk.num_data_reads = 0
    ppk.total_data_len = 0
    ppk.max_data_len = 0
    return ppk


@pytest.fixture
def power_stress_client() -> tuple[MagicMock, "PowerStressClient"]:
    """Create a PowerStressClient with a default mocked interface."""
    from meshtastic.powermon.stress import PowerStressClient  # pylint: disable=C0415

    iface = MagicMock()
    iface.myInfo = MagicMock()
    iface.myInfo.my_node_num = 1
    client = PowerStressClient(iface)
    return iface, client


def _mock_iface_with_gpio_channel(channel_index: int = 0) -> MagicMock:
    """Create a SerialInterface mock that provides a stubbed GPIO channel."""
    iface = create_autospec(SerialInterface, instance=True)
    iface.localNode = MagicMock()
    channel = MagicMock()
    channel.index = channel_index
    iface.localNode.getChannelByName.return_value = channel
    iface.localNode.getChannelCopyByName.return_value = channel
    return cast(MagicMock, iface)


def _resolve_cli_binary_or_skip(binary_name: str) -> str:
    """Resolve a CLI binary path or skip tests when it is unavailable."""
    exe = shutil.which(binary_name)
    if exe is None:
        pytest.skip(f"{binary_name} CLI not found in PATH")
    return exe


@pytest.fixture(name="mock_gpio_iface")
def _mock_gpio_iface_fixture() -> Generator[MagicMock, None, None]:
    """Provide a GPIO-capable mocked interface for GPIO tests."""
    yield _mock_iface_with_gpio_channel()


@pytest.fixture(scope="session")
def meshtastic_bin() -> str:
    """Resolve the meshtastic CLI binary path or skip tests if not found.

    Returns
    -------
    str
        The absolute path to the meshtastic CLI binary.

    Raises
    ------
    pytest.skip
        If the meshtastic CLI is not found in PATH.
    """
    return _resolve_cli_binary_or_skip("meshtastic")


@pytest.fixture(scope="session")
def mesh_tunnel_bin() -> str:
    """Resolve the mesh-tunnel CLI binary path or skip tests if not found.

    Returns
    -------
    str
        The absolute path to the mesh-tunnel CLI binary.

    Raises
    ------
    pytest.skip
        If the mesh-tunnel CLI is not found in PATH.
    """
    return _resolve_cli_binary_or_skip("mesh-tunnel")


@pytest.fixture(name="platform_socket_mocks")
def _platform_socket_mocks() -> Generator[tuple[MagicMock, MagicMock], None, None]:
    """Patch platform.system and socket.socket for tunnel tests."""
    with (
        patch("platform.system", return_value="Linux") as platform_mock,
        patch("socket.socket") as socket_mock,
    ):
        yield platform_mock, socket_mock


@pytest.fixture(scope="session", autouse=True)
def _shutdown_publishing_thread() -> Generator[None, None, None]:
    """Ensure the global publishing thread is shut down after all tests complete.

    The publishingThread is a global DeferredExecution instance that creates a
    daemon worker thread. While daemon threads should exit when the main thread
    exits, pytest can hang waiting for them. This fixture ensures clean shutdown.
    """
    yield
    # After all tests complete, shut down the publishing thread

    # Use cast to satisfy mypy - publishingThread is a DeferredExecution instance
    if publishingThread is not None:
        _thread = cast(DeferredExecution, publishingThread)
        _thread.stop()
        _thread.join(timeout=5.0)


@pytest.fixture
def mock_interface() -> Generator[MeshInterface, None, None]:
    """Provide a MeshInterface with mocked internals for behavioral testing.

    Yields
    ------
    MeshInterface
        Interface instance with mocked `_send_to_radio_impl`, populated nodes,
        and a mocked `myInfo`.
    """
    iface = MeshInterface(noProto=True)
    try:
        # Mock critical methods to avoid hardware dependency
        iface._send_to_radio_impl = MagicMock()  # type: ignore[method-assign]
        iface.myInfo = MagicMock()
        iface.myInfo.my_node_num = 2475227164

        # Set up sample nodes - use non-hex IDs to ensure DB lookup path
        iface.nodes = {
            "!9388f81c": {
                "num": 2475227164,
                "user": {
                    "id": "!9388f81c",
                    "longName": "Test Node",
                    "shortName": "TN",
                    "macaddr": "RBeTiPgc",
                    "hwModel": "TBEAM",
                },
                "position": {"time": 1640206266},
                "lastHeard": 1640204888,
            },
            "!testnode1": {
                "num": 11259375,
                "user": {
                    "id": "!testnode1",
                    "longName": "Remote Node",
                    "shortName": "RN",
                    "macaddr": "Test1234",
                    "hwModel": "RAK4631",
                },
                "position": {},
                "lastHeard": 1640205000,
            },
        }
        iface.nodesByNum = {
            2475227164: iface.nodes["!9388f81c"],
            11259375: iface.nodes["!testnode1"],
        }

        yield iface
    finally:
        iface.close()


@pytest.fixture
def mock_interface_with_nodes(mock_interface: MeshInterface) -> MeshInterface:
    """Provide a mock interface pre-configured with nodes.

    This is a semantic alias for ``mock_interface`` for tests that explicitly
    require a node database to be populated.

    Returns
    -------
    MeshInterface
        The same instance as ``mock_interface``, already configured with sample nodes.
    """
    # Already configured in mock_interface
    return mock_interface
