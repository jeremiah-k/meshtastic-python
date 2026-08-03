"""BLE trust and management lifecycle tests."""

# pylint: disable=redefined-outer-name

import contextlib
import logging
import re
import subprocess
import threading
import time
from collections.abc import Iterator
from types import SimpleNamespace, TracebackType
from typing import Any, Literal, cast

import pytest
from bleak.backends.device import BLEDevice

# Import meshtastic modules for use in tests
from meshtastic.interfaces.ble import (
    BLEClient,
    BLEInterface,
)
from meshtastic.interfaces.ble.constants import (
    ERROR_MANAGEMENT_ADDRESS_EMPTY,
    ERROR_MANAGEMENT_CONNECTING,
    ERROR_MANAGEMENT_TARGET_CHANGED,
    ERROR_TRUST_ADDRESS_NOT_RESOLVED,
    ERROR_TRUST_BLUETOOTHCTL_MISSING,
    ERROR_TRUST_COMMAND_FAILED,
    ERROR_TRUST_COMMAND_TIMEOUT,
    ERROR_TRUST_INVALID_TIMEOUT,
)
from meshtastic.interfaces.ble.state import ConnectionState


from tests._ble_interface_core_support import (
    _MAX_SPURIOUS_CLOSE_WAIT_CALLS_BEFORE_FAIL,
    _capture_management_wait_event,
    _clear_management_handler,
    _create_ble_device,
    _pin_interface_platform,
    _pin_interface_shutil_which,
    _pin_interface_subprocess_run,
    _pin_trust_environment,
)

# Import common fixtures
from tests.test_ble_interface_fixtures import DummyClient, _build_interface

pytestmark = pytest.mark.unit


def test_ble_interface_trust_rejects_blank_explicit_target_before_environment_checks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """trust() should reject blank targets before platform or tool validation."""
    iface = _build_interface(monkeypatch, DummyClient(), start_receive_thread=False)
    _pin_interface_platform(monkeypatch, "darwin")
    _pin_interface_shutil_which(monkeypatch, lambda _name: None)
    _pin_interface_subprocess_run(
        monkeypatch,
        lambda *_args, **_kwargs: pytest.fail("subprocess.run should not be reached"),
    )

    with pytest.raises(BLEInterface.BLEError, match=ERROR_MANAGEMENT_ADDRESS_EMPTY):
        iface.trust("   ")

    iface.close()


def test_ble_interface_trust_revalidates_implicit_target_after_gate_handoff(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """trust() should abort if the implicit management target changes at the gate."""
    current_address = "AA:BB:CC:DD:EE:FF"
    replacement_address = "11:22:33:44:55:66"
    client = DummyClient()
    client.address = current_address
    client.bleak_client = SimpleNamespace(address=current_address)
    iface = _build_interface(monkeypatch, client, start_receive_thread=False)

    replacement_client = DummyClient()
    replacement_client.address = replacement_address
    replacement_client.bleak_client = SimpleNamespace(address=replacement_address)

    @contextlib.contextmanager
    def _swap_target_gate(_target_address: str) -> Iterator[None]:
        with iface._state_lock:
            cast(Any, iface).client = replacement_client
            iface.address = replacement_address
            iface._state_manager._reset_to_disconnected()
            assert iface._state_manager._transition_to(ConnectionState.CONNECTING)
            assert iface._state_manager._transition_to(ConnectionState.CONNECTED)
        yield

    def _unexpected_run(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("subprocess.run should not be reached")

    _pin_trust_environment(monkeypatch, run=_unexpected_run)
    _clear_management_handler(iface)
    monkeypatch.setattr(iface, "_management_target_gate", _swap_target_gate)

    with pytest.raises(BLEInterface.BLEError, match=ERROR_MANAGEMENT_TARGET_CHANGED):
        iface.trust(timeout=7.0)

    iface.close()


@pytest.mark.parametrize("method_name", ["pair", "unpair", "trust"])
def test_ble_interface_management_allows_bound_name_when_target_stays_resolved(
    monkeypatch: pytest.MonkeyPatch,
    method_name: str,
) -> None:
    """Disconnected name-bound management ops should revalidate by resolving the same target."""
    target_name = "mesh-node"
    target_address = "AA:BB:CC:DD:EE:20"
    iface = _build_interface(monkeypatch, DummyClient(), start_receive_thread=False)
    with iface._state_lock:
        iface.client = None
        iface.address = target_name
        iface._state_manager._reset_to_disconnected()

    monkeypatch.setattr(
        iface,
        "findDevice",
        lambda identifier: _create_ble_device(target_address, str(identifier)),
    )

    if method_name == "trust":
        run_calls: list[tuple[tuple[object, ...], dict[str, object]]] = []

        def _run(*args: object, **kwargs: object) -> SimpleNamespace:
            run_calls.append((args, dict(kwargs)))
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        _pin_trust_environment(monkeypatch, run=_run)
        _clear_management_handler(iface)
        iface.trust(timeout=7.0)
        assert run_calls
    else:
        command_calls: list[str] = []
        temp_client = SimpleNamespace(
            pair=lambda **_kwargs: command_calls.append("pair"),
            unpair=lambda **_kwargs: command_calls.append("unpair"),
            bleak_client=SimpleNamespace(address=target_address),
        )
        cleanup_calls: list[object] = []

        monkeypatch.setattr(
            "meshtastic.interfaces.ble.interface.BLEClient",
            lambda _address, **_kwargs: temp_client,
        )
        _clear_management_handler(iface)
        monkeypatch.setattr(
            iface._client_manager,
            "_safe_close_client",
            lambda client: cleanup_calls.append(client),
        )

        if method_name == "pair":
            iface.pair(confirm=True, await_timeout=7.0)
        else:
            iface.unpair(await_timeout=7.0)
        assert command_calls == [method_name]
        assert cleanup_calls == [temp_client]

    iface.close()


@pytest.mark.parametrize("method_name", ["pair", "unpair", "trust"])
def test_ble_interface_management_requires_target_when_disconnected(
    monkeypatch: pytest.MonkeyPatch,
    method_name: str,
) -> None:
    """Management operations should not discover an arbitrary device when disconnected."""
    iface = _build_interface(monkeypatch, DummyClient(), start_receive_thread=False)
    with iface._state_lock:
        iface.client = None
        iface.address = None
        iface._state_manager._reset_to_disconnected()

    find_device_called = False

    def _unexpected_find_device(_address: str | None) -> BLEDevice:
        nonlocal find_device_called
        find_device_called = True
        return _create_ble_device("AA:BB:CC:DD:EE:FF", "Meshtastic")

    monkeypatch.setattr(iface, "findDevice", _unexpected_find_device)
    if method_name == "trust":
        _pin_trust_environment(monkeypatch)

    with pytest.raises(BLEInterface.BLEError, match="explicit address"):
        getattr(iface, method_name)()

    assert find_device_called is False
    iface.close()


@pytest.mark.parametrize("method_name", ["pair", "unpair", "trust"])
def test_ble_interface_management_rejects_connecting_state(
    monkeypatch: pytest.MonkeyPatch,
    method_name: str,
) -> None:
    """Management operations should refuse to run while a connect is in progress."""
    iface = _build_interface(monkeypatch, DummyClient(), start_receive_thread=False)
    with iface._state_lock:
        iface.client = None
        iface._state_manager._reset_to_disconnected()
        assert iface._state_manager._transition_to(ConnectionState.CONNECTING) is True

    if method_name == "trust":
        _pin_trust_environment(monkeypatch)

    with pytest.raises(BLEInterface.BLEError, match=ERROR_MANAGEMENT_CONNECTING):
        getattr(iface, method_name)("AA:BB:CC:DD:EE:FF")

    iface.close()


def test_ble_interface_resolve_management_address_prefers_connected_client_address(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Management target resolution should reuse the connected client's address."""
    client = DummyClient()
    client.address = "11:22:33:44:55:66"
    client.bleak_client = SimpleNamespace(address="AA:BB:CC:DD:EE:FF")
    iface = _build_interface(monkeypatch, client, start_receive_thread=False)

    assert iface._resolve_target_address_for_management(None) == "aa:bb:cc:dd:ee:ff"
    iface.close()


def test_ble_interface_resolve_management_address_rejects_blank_bound_target(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bound but blank management targets should fail fast."""
    iface = _build_interface(monkeypatch, DummyClient(), start_receive_thread=False)
    with iface._state_lock:
        iface.client = None
        iface.address = "   "
        iface._state_manager._reset_to_disconnected()

    with pytest.raises(BLEInterface.BLEError, match=ERROR_MANAGEMENT_ADDRESS_EMPTY):
        iface._resolve_target_address_for_management(None)

    iface.close()


def test_ble_interface_resolve_management_address_uses_existing_client_address(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Management target resolution should reuse a matching existing client address."""
    iface = _build_interface(monkeypatch, DummyClient(), start_receive_thread=False)
    with iface._state_lock:
        iface.client = None
        iface._state_manager._reset_to_disconnected()

    existing_client = DummyClient()
    existing_client.address = "11:22:33:44:55:66"
    existing_client.bleak_client = SimpleNamespace(address=None)
    monkeypatch.setattr(
        iface,
        "_get_existing_client_if_valid",
        lambda _request: cast(BLEClient, existing_client),
    )

    assert (
        iface._resolve_target_address_for_management("mesh-node") == "11:22:33:44:55:66"
    )
    iface.close()


def test_ble_interface_resolve_management_address_accepts_explicit_ble_address(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Explicit BLE addresses should bypass discovery resolution."""
    iface = _build_interface(monkeypatch, DummyClient(), start_receive_thread=False)
    with iface._state_lock:
        iface.client = None
        iface._state_manager._reset_to_disconnected()

    discovery_called = False

    def _unexpected_find_device(_address: str | None) -> BLEDevice:
        nonlocal discovery_called
        discovery_called = True
        return _create_ble_device("11:22:33:44:55:66", "Unexpected")

    monkeypatch.setattr(iface, "findDevice", _unexpected_find_device)

    assert (
        iface._resolve_target_address_for_management("AA-BB-CC-DD-EE-FF")
        == "aa:bb:cc:dd:ee:ff"
    )
    assert discovery_called is False
    iface.close()


def test_ble_interface_format_bluetoothctl_address_rejects_unresolved_input(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bluetoothctl formatting should fail for unresolved non-address identifiers."""
    iface = _build_interface(monkeypatch, DummyClient(), start_receive_thread=False)

    with pytest.raises(
        BLEInterface.BLEError,
        match=re.escape(ERROR_TRUST_ADDRESS_NOT_RESOLVED.format(address="mesh-node")),
    ):
        iface._format_bluetoothctl_address("mesh-node")

    iface.close()


def test_ble_interface_trust_includes_stdout_and_stderr_in_failure_details(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """trust() should include both stderr and stdout when bluetoothctl fails."""
    iface = _build_interface(monkeypatch, DummyClient(), start_receive_thread=False)
    with iface._state_lock:
        iface.client = None
        iface._state_manager._reset_to_disconnected()
    monkeypatch.setattr(
        iface,
        "findDevice",
        lambda _address: _create_ble_device("AA:BB:CC:DD:EE:FF", "Meshtastic"),
    )
    _pin_interface_platform(monkeypatch, "linux")
    _pin_interface_shutil_which(
        monkeypatch, lambda _name: "/usr/bin/bluetoothctl"
    )
    _pin_interface_subprocess_run(
        monkeypatch,
        lambda *_args, **_kwargs: SimpleNamespace(
            returncode=1,
            stdout="generic output",
            stderr="specific failure",
        ),
    )

    with pytest.raises(BLEInterface.BLEError) as exc_info:
        iface.trust("mesh-node")

    detail = str(exc_info.value)
    assert "stderr: specific failure" in detail
    assert "stdout: generic output" in detail

    iface.close()


def test_ble_interface_trust_truncates_long_subprocess_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """trust() should truncate oversized subprocess snippets to a bounded length."""
    iface = _build_interface(monkeypatch, DummyClient(), start_receive_thread=False)
    with iface._state_lock:
        iface.client = None
        iface._state_manager._reset_to_disconnected()
    monkeypatch.setattr(
        iface,
        "findDevice",
        lambda _address: _create_ble_device("AA:BB:CC:DD:EE:FF", "Meshtastic"),
    )
    _pin_interface_platform(monkeypatch, "linux")
    _pin_interface_shutil_which(
        monkeypatch, lambda _name: "/usr/bin/bluetoothctl"
    )
    long_output = "long-output-segment " * 200
    _pin_interface_subprocess_run(
        monkeypatch,
        lambda *_args, **_kwargs: SimpleNamespace(
            returncode=1,
            stdout="",
            stderr=long_output,
        ),
    )

    with pytest.raises(BLEInterface.BLEError) as exc_info:
        iface.trust("mesh-node")

    detail = str(exc_info.value)
    assert "stderr:" in detail
    assert "..." in detail

    iface.close()


def test_ble_interface_trust_runs_bluetoothctl(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """trust() should invoke bluetoothctl trust with a canonicalized address."""
    iface = _build_interface(monkeypatch, DummyClient(), start_receive_thread=False)
    with iface._state_lock:
        iface.client = None
        iface._state_manager._reset_to_disconnected()
    monkeypatch.setattr(
        iface,
        "findDevice",
        lambda _address: _create_ble_device("aa bb cc dd ee ff", "Meshtastic"),
    )
    _pin_interface_platform(monkeypatch, "linux")
    _pin_interface_shutil_which(
        monkeypatch, lambda _name: "/usr/bin/bluetoothctl"
    )

    run_calls: list[tuple[list[str], float]] = []

    def _fake_run(
        args: list[str],
        *,
        capture_output: bool,
        text: bool,
        check: bool,
        timeout: float,
    ) -> SimpleNamespace:
        _ = (capture_output, text, check)
        run_calls.append((args, timeout))
        return SimpleNamespace(returncode=0, stdout="succeeded", stderr="")

    _pin_interface_subprocess_run(monkeypatch, _fake_run)

    iface.trust("mesh-node", timeout=7.0)

    assert run_calls == [(["/usr/bin/bluetoothctl", "trust", "AA:BB:CC:DD:EE:FF"], 7.0)]
    iface.close()


def test_ble_interface_trust_rejects_non_linux(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """trust() should reject non-Linux hosts with a clear BLEError."""
    iface = _build_interface(monkeypatch, DummyClient(), start_receive_thread=False)
    _pin_interface_platform(monkeypatch, "darwin")
    _pin_interface_shutil_which(
        monkeypatch, lambda _name: pytest.fail("shutil.which should not be reached")
    )
    _pin_interface_subprocess_run(
        monkeypatch,
        lambda *_args, **_kwargs: pytest.fail("subprocess.run should not be reached"),
    )
    with pytest.raises(BLEInterface.BLEError, match="only supported on Linux"):
        iface.trust("AA:BB:CC:DD:EE:FF")
    iface.close()


@pytest.mark.parametrize(
    "invalid_timeout",
    [0, -1.0, float("nan"), float("inf"), float("-inf"), True, "7.0"],
)
def test_ble_interface_trust_rejects_invalid_timeout(
    monkeypatch: pytest.MonkeyPatch,
    invalid_timeout: object,
) -> None:
    """trust() should require a finite positive numeric timeout."""
    iface = _build_interface(monkeypatch, DummyClient(), start_receive_thread=False)
    _pin_trust_environment(monkeypatch)

    with pytest.raises(BLEInterface.BLEError, match=ERROR_TRUST_INVALID_TIMEOUT):
        iface.trust("AA:BB:CC:DD:EE:FF", timeout=cast(Any, invalid_timeout))

    iface.close()


def test_ble_interface_trust_requires_bluetoothctl_binary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """trust() should fail before spawning when bluetoothctl is unavailable."""
    iface = _build_interface(monkeypatch, DummyClient(), start_receive_thread=False)
    _pin_interface_platform(monkeypatch, "linux")
    _pin_interface_shutil_which(monkeypatch, lambda _name: None)
    _pin_interface_subprocess_run(
        monkeypatch,
        lambda *_args, **_kwargs: pytest.fail("subprocess.run should not be reached"),
    )

    with pytest.raises(BLEInterface.BLEError, match=ERROR_TRUST_BLUETOOTHCTL_MISSING):
        iface.trust("AA:BB:CC:DD:EE:FF")

    iface.close()


def test_ble_interface_trust_translates_subprocess_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """trust() should translate bluetoothctl timeouts into BLEError."""
    iface = _build_interface(monkeypatch, DummyClient(), start_receive_thread=False)
    _pin_interface_platform(monkeypatch, "linux")
    _pin_interface_shutil_which(
        monkeypatch, lambda _name: "/usr/bin/bluetoothctl"
    )

    def _raise_timeout(*_args: object, **_kwargs: object) -> SimpleNamespace:
        raise subprocess.TimeoutExpired(
            cmd=["/usr/bin/bluetoothctl", "trust", "AA:BB:CC:DD:EE:FF"],
            timeout=2.5,
        )

    _pin_interface_subprocess_run(monkeypatch, _raise_timeout)

    with pytest.raises(
        BLEInterface.BLEError,
        match=re.escape(
            ERROR_TRUST_COMMAND_TIMEOUT.format(timeout=2.5, address="AA:BB:CC:DD:EE:FF")
        ),
    ):
        iface.trust("AA:BB:CC:DD:EE:FF", timeout=2.5)

    iface.close()


def test_ble_interface_trust_translates_spawn_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """trust() should translate subprocess spawn failures into BLEError."""
    iface = _build_interface(monkeypatch, DummyClient(), start_receive_thread=False)
    _pin_interface_platform(monkeypatch, "linux")
    _pin_interface_shutil_which(
        monkeypatch, lambda _name: "/usr/bin/bluetoothctl"
    )

    def _raise_os_error(*_args: object, **_kwargs: object) -> SimpleNamespace:
        raise OSError("permission denied")

    _pin_interface_subprocess_run(monkeypatch, _raise_os_error)

    with pytest.raises(
        BLEInterface.BLEError,
        match=re.escape(
            ERROR_TRUST_COMMAND_FAILED.format(
                address="AA:BB:CC:DD:EE:FF",
                detail="/usr/bin/bluetoothctl: permission denied",
            )
        ),
    ):
        iface.trust("AA:BB:CC:DD:EE:FF", timeout=2.5)

    iface.close()


def test_ble_interface_trust_rejects_closing_interface(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """trust() should fail before resolution or subprocess work once shutdown starts."""
    iface = _build_interface(monkeypatch, DummyClient(), start_receive_thread=False)
    with iface._state_lock:
        iface.client = None
        iface._closed = True

    find_device_called = False
    subprocess_called = False

    def _unexpected_find_device(_address: str | None) -> BLEDevice:
        nonlocal find_device_called
        find_device_called = True
        return _create_ble_device("AA:BB:CC:DD:EE:FF", "Meshtastic")

    def _unexpected_run(*_args: object, **_kwargs: object) -> SimpleNamespace:
        nonlocal subprocess_called
        subprocess_called = True
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(iface, "findDevice", _unexpected_find_device)
    _pin_interface_platform(monkeypatch, "linux")
    _pin_interface_shutil_which(
        monkeypatch, lambda _name: "/usr/bin/bluetoothctl"
    )
    _pin_interface_subprocess_run(monkeypatch, _unexpected_run)

    try:
        with pytest.raises(BLEInterface.BLEError, match="closing"):
            iface.trust("mesh-node")

        assert find_device_called is False
        assert subprocess_called is False
    finally:
        with iface._state_lock:
            iface._closed = False
        iface.close()


def test_ble_interface_trust_does_not_hold_interface_locks_during_subprocess(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """trust() should let close() mark shutdown before bluetoothctl returns."""
    iface = _build_interface(monkeypatch, DummyClient(), start_receive_thread=False)
    trust_target = "AA:BB:CC:DD:EE:FF"
    with iface._state_lock:
        assert iface.client is not None
        active_client = cast(DummyClient, iface.client)
        active_client.address = trust_target
        active_client.bleak_client = SimpleNamespace(address=trust_target)
        iface.address = trust_target
    run_started = threading.Event()
    allow_run_return = threading.Event()
    close_done = threading.Event()
    close_started = threading.Event()
    trust_errors: list[Exception] = []
    close_errors: list[Exception] = []

    def _blocking_run(*_args: object, **_kwargs: object) -> SimpleNamespace:
        run_started.set()
        assert allow_run_return.wait(timeout=1.0)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    _pin_trust_environment(monkeypatch, run=_blocking_run)
    management_wait_entered = _capture_management_wait_event(monkeypatch, iface)

    def _run_trust() -> None:
        try:
            iface.trust(trust_target, timeout=7.0)
        except Exception as exc:  # pragma: no cover - failure captured below  # noqa: BLE001 - test captures thread errors
            trust_errors.append(exc)

    def _close_iface() -> None:
        try:
            close_started.set()
            iface.close()
        except Exception as exc:  # pragma: no cover - failure captured below  # noqa: BLE001 - test captures thread errors
            close_errors.append(exc)
        finally:
            close_done.set()

    trust_thread = threading.Thread(target=_run_trust, daemon=True)
    trust_thread.start()
    assert run_started.wait(timeout=1.0)

    close_thread = threading.Thread(target=_close_iface, daemon=True)
    close_thread.start()
    assert close_started.wait(timeout=1.0)
    assert management_wait_entered.wait(timeout=1.0)
    with iface._state_lock:
        assert iface._closed is True
    assert close_done.is_set() is False

    allow_run_return.set()
    trust_thread.join(timeout=2.0)
    close_thread.join(timeout=2.0)

    assert not trust_thread.is_alive()
    assert not close_thread.is_alive()
    assert close_done.is_set() is True
    assert trust_errors == []
    assert close_errors == []
    with iface._state_lock:
        assert iface._closed is True


def test_ble_interface_close_waits_for_explicit_trust_without_active_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """close() should wait for explicit trust() even when no client is active."""
    iface = _build_interface(monkeypatch, DummyClient(), start_receive_thread=False)
    with iface._state_lock:
        iface.client = None
        iface.address = None
        iface._state_manager._reset_to_disconnected()

    run_started = threading.Event()
    allow_run_return = threading.Event()
    trust_errors: list[Exception] = []
    close_errors: list[Exception] = []
    close_done = threading.Event()

    def _blocking_run(*_args: object, **_kwargs: object) -> SimpleNamespace:
        run_started.set()
        assert allow_run_return.wait(timeout=1.0)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    _pin_trust_environment(monkeypatch, run=_blocking_run)
    management_wait_entered = _capture_management_wait_event(monkeypatch, iface)

    def _run_trust() -> None:
        try:
            iface.trust("AA:BB:CC:DD:EE:FF", timeout=7.0)
        except Exception as exc:  # pragma: no cover - failure captured below  # noqa: BLE001 - test captures thread errors
            trust_errors.append(exc)

    def _run_close() -> None:
        try:
            iface.close()
        except Exception as exc:  # pragma: no cover - failure captured below  # noqa: BLE001 - test captures thread errors
            close_errors.append(exc)
        finally:
            close_done.set()

    trust_thread = threading.Thread(target=_run_trust, daemon=True)
    trust_thread.start()
    assert run_started.wait(timeout=1.0)

    close_thread = threading.Thread(target=_run_close, daemon=True)
    close_thread.start()
    assert management_wait_entered.wait(timeout=1.0)
    with iface._state_lock:
        assert iface._closed is True
    assert close_done.is_set() is False

    allow_run_return.set()
    trust_thread.join(timeout=2.0)
    close_thread.join(timeout=2.0)

    assert not trust_thread.is_alive()
    assert not close_thread.is_alive()
    assert trust_errors == []
    assert close_errors == []
    assert close_done.is_set() is True


def test_ble_interface_close_skips_management_gate_after_wait_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """close() should not block on the per-target gate after management wait timeout."""
    client = DummyClient()
    client.address = "AA:BB:CC:DD:EE:21"
    client.bleak_client = SimpleNamespace(address=client.address)
    iface = _build_interface(monkeypatch, client, start_receive_thread=False)
    gate_calls: list[str] = []
    unsubscribe_calls: list[object] = []
    disconnect_calls: list[object] = []

    with iface._management_lock:
        iface._management_inflight = 1

    monkeypatch.setattr(
        iface._management_idle_condition,
        "wait",
        lambda timeout=None: False,
        raising=True,
    )

    def _unexpected_management_gate(
        _address: str,
    ) -> contextlib.AbstractContextManager[None]:
        gate_calls.append(_address)
        return contextlib.nullcontext()

    monkeypatch.setattr(
        iface,
        "_management_target_gate",
        _unexpected_management_gate,
        raising=True,
    )
    monkeypatch.setattr(
        "meshtastic.interfaces.ble.interface.MeshInterface.close",
        lambda _self: None,
    )
    monkeypatch.setattr(
        iface._notification_manager,
        "_unsubscribe_all",
        lambda active_client, timeout=None: unsubscribe_calls.append(active_client),
        raising=True,
    )
    monkeypatch.setattr(
        iface,
        "_disconnect_and_close_client",
        lambda active_client: disconnect_calls.append(active_client),
        raising=True,
    )
    monkeypatch.setattr(
        iface._notification_manager,
        "_cleanup_all",
        lambda: None,
        raising=True,
    )

    iface.close()

    assert gate_calls == []
    assert unsubscribe_calls == [client]
    assert disconnect_calls == [client]


def test_ble_interface_close_bounds_wait_on_spurious_management_wakeups(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """close() should enforce shutdown timeout despite spurious management wakeups."""
    client = DummyClient()
    client.address = "AA:BB:CC:DD:EE:31"
    client.bleak_client = SimpleNamespace(address=client.address)
    iface = _build_interface(monkeypatch, client, start_receive_thread=False)
    wait_calls: list[float | None] = []
    gate_calls: list[str] = []
    unsubscribe_calls: list[object] = []
    disconnect_calls: list[object] = []
    close_errors: list[Exception] = []
    close_done = threading.Event()

    with iface._management_lock:
        iface._management_inflight = 1
    with iface._state_lock:
        iface._disconnect_notified = True

    monkeypatch.setattr(
        "meshtastic.interfaces.ble.interface._MANAGEMENT_SHUTDOWN_WAIT_TIMEOUT_SECONDS",
        0.05,
    )
    monkeypatch.setattr(
        "meshtastic.interfaces.ble.interface._MANAGEMENT_CONNECT_WAIT_POLL_SECONDS",
        0.005,
    )

    def _spurious_wait(timeout: float | None = None) -> bool:
        """Simulate a bounded spurious shutdown wait.

        Parameters
        ----------
        timeout : float | None
            Requested wait duration. Positive values are slept to preserve the
            shutdown deadline behavior under test.

        Returns
        -------
        bool
            ``True`` to simulate a spurious wakeup.

        Raises
        ------
        AssertionError
            If shutdown exceeds the configured spurious-wakeup budget.
        """
        wait_calls.append(timeout)
        if timeout is not None and timeout > 0:
            time.sleep(timeout)
        if len(wait_calls) > _MAX_SPURIOUS_CLOSE_WAIT_CALLS_BEFORE_FAIL:
            close_done.set()
            raise AssertionError(
                "close() kept waiting past the shutdown timeout budget"
            )
        return True

    monkeypatch.setattr(
        iface._management_idle_condition,
        "wait",
        _spurious_wait,
        raising=True,
    )

    def _management_gate(
        address: str,
    ) -> contextlib.AbstractContextManager[None]:
        """Record a management address and return a no-op gate.

        Parameters
        ----------
        address : str
            Management address requested by the close path.

        Returns
        -------
        contextlib.AbstractContextManager[None]
            No-op context manager for deterministic gate assertions.
        """
        gate_calls.append(address)
        return contextlib.nullcontext()

    monkeypatch.setattr(
        iface, "_management_target_gate", _management_gate, raising=True
    )
    monkeypatch.setattr(
        "meshtastic.interfaces.ble.interface.MeshInterface.close",
        lambda _self: None,
    )
    monkeypatch.setattr(
        iface._notification_manager,
        "_unsubscribe_all",
        lambda active_client, timeout=None: unsubscribe_calls.append(active_client),
        raising=True,
    )
    monkeypatch.setattr(
        iface,
        "_disconnect_and_close_client",
        lambda active_client: disconnect_calls.append(active_client),
        raising=True,
    )
    monkeypatch.setattr(
        iface._notification_manager,
        "_cleanup_all",
        lambda: None,
        raising=True,
    )

    def _run_close() -> None:
        """Close the interface, capture failures, and always signal completion."""
        try:
            iface.close()
        except Exception as exc:  # pragma: no cover - captured for assertion
            close_errors.append(exc)
        finally:
            close_done.set()

    with caplog.at_level(logging.WARNING):
        close_thread = threading.Thread(target=_run_close, daemon=True)
        close_thread.start()
        close_thread.join(timeout=1.0)

    assert not close_thread.is_alive()
    assert close_done.is_set() is True
    assert close_errors == []
    assert wait_calls
    assert gate_calls == []
    assert unsubscribe_calls == [client]
    assert disconnect_calls == [client]
    assert any("Timed out waiting" in record.message for record in caplog.records)


def test_ble_interface_close_forwards_management_wait_poll_seconds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """close() should forward management shutdown timing kwargs to lifecycle close."""
    from meshtastic.interfaces.ble import interface as interface_mod

    iface = _build_interface(monkeypatch, DummyClient(), start_receive_thread=False)
    original_lifecycle_close = iface._lifecycle_controller._close
    close_calls: list[dict[str, object]] = []

    def _capture_close(
        *,
        management_shutdown_wait_timeout: float,
        management_wait_poll_seconds: float,
    ) -> None:
        close_calls.append(
            {
                "management_shutdown_wait_timeout": management_shutdown_wait_timeout,
                "management_wait_poll_seconds": management_wait_poll_seconds,
            }
        )
        original_lifecycle_close(
            management_shutdown_wait_timeout=management_shutdown_wait_timeout,
            management_wait_poll_seconds=management_wait_poll_seconds,
        )

    monkeypatch.setattr(
        iface._lifecycle_controller,
        "_close",
        _capture_close,
        raising=True,
    )

    monkeypatch.setattr(
        interface_mod,
        "_MANAGEMENT_SHUTDOWN_WAIT_TIMEOUT_SECONDS",
        1.23,
        raising=True,
    )
    monkeypatch.setattr(
        interface_mod,
        "_MANAGEMENT_CONNECT_WAIT_POLL_SECONDS",
        0.045,
        raising=True,
    )

    iface.close()

    assert close_calls == [
        {
            "management_shutdown_wait_timeout": 1.23,
            "management_wait_poll_seconds": 0.045,
        }
    ]


def test_ble_interface_implicit_trust_releases_connect_lock_before_subprocess(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify implicit trust() releases connect lock before blocking subprocess.

    Parameters
    ----------
    monkeypatch : pytest.MonkeyPatch
        Fixture used to patch trust subprocess behavior.

    Returns
    -------
    None
    """
    iface = _build_interface(monkeypatch, DummyClient(), start_receive_thread=False)
    trust_target = "AA:BB:CC:DD:EE:FF"
    with iface._state_lock:
        assert iface.client is not None
        active_client = cast(DummyClient, iface.client)
        active_client.address = trust_target
        active_client.bleak_client = SimpleNamespace(address=trust_target)
        iface.address = trust_target

    run_started = threading.Event()
    allow_run_return = threading.Event()
    trust_errors: list[Exception] = []

    def _blocking_run(*_args: object, **_kwargs: object) -> SimpleNamespace:
        run_started.set()
        assert allow_run_return.wait(timeout=1.0)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    _pin_trust_environment(monkeypatch, run=_blocking_run)

    def _run_trust() -> None:
        try:
            iface.trust(timeout=7.0)
        except Exception as exc:  # pragma: no cover - failure captured below  # noqa: BLE001 - test captures thread errors
            trust_errors.append(exc)

    trust_thread = threading.Thread(target=_run_trust, daemon=True)
    trust_thread.start()
    try:
        assert run_started.wait(timeout=1.0)

        assert iface._connect_lock.acquire(blocking=False) is True
        iface._connect_lock.release()
    finally:
        allow_run_return.set()
        if trust_thread.is_alive():
            trust_thread.join(timeout=2.0)

    assert not trust_thread.is_alive()
    assert trust_errors == []
    assert iface._connect_lock.acquire(blocking=False) is True
    iface._connect_lock.release()

    iface.close()


def test_ble_interface_close_serializes_with_management_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """close() should not mark the interface closed while a management op holds the lock."""
    iface = _build_interface(monkeypatch, DummyClient(), start_receive_thread=False)
    close_done = threading.Event()
    close_started = threading.Event()
    close_errors: list[Exception] = []

    def _close_iface() -> None:
        try:
            close_started.set()
            iface.close()
        except Exception as exc:  # pragma: no cover - failure captured below  # noqa: BLE001 - test captures thread errors
            close_errors.append(exc)
        finally:
            close_done.set()

    with iface._management_lock:
        close_thread = threading.Thread(target=_close_iface, daemon=True)
        close_thread.start()
        assert close_started.wait(timeout=1.0)
        with iface._state_lock:
            assert iface._closed is False
        assert close_done.is_set() is False

    close_thread.join(timeout=2.0)
    assert close_errors == []
    assert close_done.is_set() is True


def test_ble_interface_close_does_not_wait_for_connect_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """close() should still start shutdown while the connect lock is held."""
    iface = _build_interface(monkeypatch, DummyClient(), start_receive_thread=False)
    close_done = threading.Event()
    close_errors: list[Exception] = []

    def _close_iface() -> None:
        try:
            iface.close()
        except Exception as exc:  # pragma: no cover - failure captured below  # noqa: BLE001 - test captures thread errors
            close_errors.append(exc)
        finally:
            close_done.set()

    with iface._connect_lock:
        close_thread = threading.Thread(target=_close_iface, daemon=True)
        close_thread.start()
        assert close_done.wait(timeout=1.0)
        with iface._state_lock:
            assert iface._closed is True

    close_thread.join(timeout=2.0)
    assert close_errors == []
    assert close_done.is_set() is True


def test_ble_interface_pair_waits_for_connect_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """pair() should serialize behind the interface connect lock."""
    iface = _build_interface(monkeypatch, DummyClient(), start_receive_thread=False)
    real_connect_lock = iface._connect_lock
    with iface._state_lock:
        iface.client = None
        iface._state_manager._reset_to_disconnected()
    monkeypatch.setattr(
        iface,
        "findDevice",
        lambda _address: _create_ble_device("AA:BB:CC:DD:EE:FF", "Meshtastic"),
    )

    pair_kwargs: list[dict[str, object]] = []
    pair_await_timeouts: list[float | None] = []
    close_calls: list[object] = []
    pair_finished = threading.Event()
    pair_thread_started = threading.Event()
    temp_client_created = threading.Event()
    allow_temp_client_creation = threading.Event()
    connect_lock_attempted = threading.Event()

    class _ObservedConnectLock:
        """Connect-lock proxy that records acquisition before delegating."""

        def __enter__(self) -> "_ObservedConnectLock":
            connect_lock_attempted.set()
            real_connect_lock.acquire()
            return self

        def __exit__(
            self,
            exc_type: type[BaseException] | None,
            exc: BaseException | None,
            tb: TracebackType | None,
        ) -> Literal[False]:
            _ = (exc_type, exc, tb)
            real_connect_lock.release()
            return False

    def _pair(*, await_timeout: float | None = None, **kwargs: object) -> None:
        pair_kwargs.append(dict(kwargs))
        pair_await_timeouts.append(await_timeout)
        pair_finished.set()

    temp_client = SimpleNamespace(
        pair=_pair,
        bleak_client=SimpleNamespace(address="AA:BB:CC:DD:EE:FF"),
    )

    def _temp_client_factory(_address: str, **_kwargs: object) -> SimpleNamespace:
        assert allow_temp_client_creation.wait(timeout=1.0)
        temp_client_created.set()
        return temp_client

    def _run_pair() -> None:
        pair_thread_started.set()
        iface.pair("mesh-node", confirm=True, await_timeout=7.0)

    monkeypatch.setattr(
        "meshtastic.interfaces.ble.interface.BLEClient",
        _temp_client_factory,
    )
    _clear_management_handler(iface)
    monkeypatch.setattr(
        iface._client_manager,
        "_safe_close_client",
        lambda client: close_calls.append(client),
    )
    monkeypatch.setattr(iface, "_connect_lock", _ObservedConnectLock())

    with real_connect_lock:
        pair_thread = threading.Thread(target=_run_pair, daemon=True)
        pair_thread.start()
        assert pair_thread_started.wait(timeout=1.0)
        assert connect_lock_attempted.wait(timeout=1.0)
        assert pair_kwargs == []
        assert pair_finished.is_set() is False
        allow_temp_client_creation.set()

    pair_thread.join(timeout=2.0)
    assert not pair_thread.is_alive()
    assert temp_client_created.is_set() is True
    assert pair_kwargs == [{"confirm": True}]
    assert pair_await_timeouts == [7.0]
    assert close_calls == [temp_client]
    assert pair_finished.is_set() is True
    iface.close()


def test_ble_interface_pair_waits_for_address_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """pair() should serialize temporary management work with the address gate."""
    iface = _build_interface(monkeypatch, DummyClient(), start_receive_thread=False)
    with iface._state_lock:
        iface.client = None
        iface._state_manager._reset_to_disconnected()
    monkeypatch.setattr(
        iface,
        "findDevice",
        lambda _address: _create_ble_device("AA:BB:CC:DD:EE:FF", "Meshtastic"),
    )

    pair_kwargs: list[dict[str, object]] = []
    pair_await_timeouts: list[float | None] = []
    close_calls: list[object] = []
    pair_finished = threading.Event()
    pair_thread_started = threading.Event()
    temp_client_created = threading.Event()
    allow_temp_client_creation = threading.Event()
    addr_gate_attempted = threading.Event()

    class _ObservedAddressLock:
        """Address-lock test double that records context entry."""

        def __init__(self) -> None:
            self._lock = threading.RLock()

        def __enter__(self) -> "_ObservedAddressLock":
            addr_gate_attempted.set()
            self._lock.acquire()
            return self

        def __exit__(
            self,
            exc_type: type[BaseException] | None,
            exc: BaseException | None,
            tb: TracebackType | None,
        ) -> Literal[False]:
            _ = (exc_type, exc, tb)
            self._lock.release()
            return False

    observed_address_lock = _ObservedAddressLock()

    @contextlib.contextmanager
    def _observed_addr_lock_context(
        _addr: str | None,
    ) -> Iterator[_ObservedAddressLock]:
        with observed_address_lock:
            yield observed_address_lock

    def _pair(*, await_timeout: float | None = None, **kwargs: object) -> None:
        pair_kwargs.append(dict(kwargs))
        pair_await_timeouts.append(await_timeout)
        pair_finished.set()

    temp_client = SimpleNamespace(
        pair=_pair,
        bleak_client=SimpleNamespace(address="AA:BB:CC:DD:EE:FF"),
    )

    def _temp_client_factory(_address: str, **_kwargs: object) -> SimpleNamespace:
        assert allow_temp_client_creation.wait(timeout=1.0)
        temp_client_created.set()
        return temp_client

    def _run_pair() -> None:
        pair_thread_started.set()
        iface.pair("mesh-node", confirm=True, await_timeout=7.0)

    monkeypatch.setattr(
        "meshtastic.interfaces.ble.interface.BLEClient",
        _temp_client_factory,
    )
    _clear_management_handler(iface)
    monkeypatch.setattr(
        iface._client_manager,
        "_safe_close_client",
        lambda client: close_calls.append(client),
    )
    monkeypatch.setattr(
        "meshtastic.interfaces.ble.interface._addr_lock_context",
        _observed_addr_lock_context,
    )

    with observed_address_lock:
        pair_thread = threading.Thread(target=_run_pair, daemon=True)
        pair_thread.start()
        assert pair_thread_started.wait(timeout=1.0)
        assert addr_gate_attempted.wait(timeout=1.0)
        assert pair_kwargs == []
        assert pair_finished.is_set() is False
        allow_temp_client_creation.set()

    pair_thread.join(timeout=2.0)
    assert not pair_thread.is_alive()
    assert temp_client_created.is_set() is True
    assert pair_kwargs == [{"confirm": True}]
    assert pair_await_timeouts == [7.0]
    assert close_calls == [temp_client]
    assert pair_finished.is_set() is True
    iface.close()


def test_ble_interface_close_logs_when_shutdown_already_in_progress(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """close() should log when cleanup continues from an already-closing state."""
    iface = _build_interface(monkeypatch, DummyClient(), start_receive_thread=False)
    with iface._state_lock:
        iface._state_manager._reset_to_disconnected()
        assert iface._state_manager._transition_to(ConnectionState.CONNECTING) is True
        assert iface._state_manager._transition_to(ConnectionState.CONNECTED) is True
        assert (
            iface._state_manager._transition_to(ConnectionState.DISCONNECTING) is True
        )

    with caplog.at_level(logging.DEBUG):
        iface.close()

    assert "another shutdown is in progress" in caplog.text
