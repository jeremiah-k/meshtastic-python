"""BLE connection lifecycle tests."""

# pylint: disable=redefined-outer-name

import logging
import re
import threading
from types import SimpleNamespace
from typing import Any, Callable, cast

import pytest

# Import meshtastic modules for use in tests
from meshtastic.interfaces.ble import (
    BLEClient,
    BLEInterface,
)
from meshtastic.interfaces.ble.constants import (
    ERROR_MANAGEMENT_CONNECTING,
)
from meshtastic.interfaces.ble.state import ConnectionState


from tests._ble_interface_core_support import (
    _MAX_SPURIOUS_CONNECT_WAIT_CALLS_BEFORE_FAIL,
    _build_minimal_connect_test_interface,
)

# Import common fixtures
from tests.test_ble_interface_fixtures import DummyClient, _build_interface

pytestmark = pytest.mark.unit


def test_ble_interface_connect_uses_pair_override_for_orchestrator(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """connect() should forward pair and timeout overrides to connection orchestration."""
    iface = _build_minimal_connect_test_interface()

    monkeypatch.setattr(iface, "_validate_connection_preconditions", lambda: None)
    monkeypatch.setattr(iface, "_get_existing_client_if_valid", lambda _req: None)
    monkeypatch.setattr(
        iface, "_raise_if_duplicate_connect", lambda _key, **_kwargs: None
    )
    monkeypatch.setattr(iface, "_finalize_connection_gates", lambda *_args: None)
    connected_callbacks: list[bool] = []
    monkeypatch.setattr(iface, "_connected", lambda: connected_callbacks.append(True))

    captured_pair_flags: list[bool] = []
    captured_timeouts: list[float | None] = []

    def _establish_stub(
        address: str | None,
        normalized_request: str | None,
        address_key: str | None,
        *,
        pair_on_connect: bool = False,
        connect_timeout: float | None = None,
    ) -> tuple[DummyClient, str | None, str | None]:
        _ = (address, normalized_request, address_key)
        client = DummyClient()
        captured_pair_flags.append(pair_on_connect)
        captured_timeouts.append(connect_timeout)
        with iface._state_lock:
            cast(Any, iface).client = client
            iface.address = client.address
            iface._state_manager._reset_to_disconnected()
            assert iface._state_manager._transition_to(ConnectionState.CONNECTING)
            assert iface._state_manager._transition_to(ConnectionState.CONNECTED)
        return client, None, None

    monkeypatch.setattr(iface, "_establish_and_update_client", _establish_stub)

    iface.connect(pair=True, connect_timeout=4.5)
    iface.connect(pair=False)
    iface.pair_on_connect = True
    iface.connect()

    assert captured_pair_flags == [True, False, True]
    assert captured_timeouts == [4.5, None, None]
    assert connected_callbacks == [True, True, True]


def test_connect_wraps_invalid_connect_timeout_as_ble_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """connect() should wrap invalid timeout overrides as BLEError."""
    iface = _build_minimal_connect_test_interface()
    cast(Any, iface)._connection_orchestrator = SimpleNamespace(
        _establish_connection=lambda *_args, **_kwargs: pytest.fail(
            "_establish_connection should not be called for invalid connect_timeout"
        )
    )

    monkeypatch.setattr(iface, "_validate_connection_preconditions", lambda: None)
    monkeypatch.setattr(iface, "_get_existing_client_if_valid", lambda _req: None)
    monkeypatch.setattr(
        iface, "_raise_if_duplicate_connect", lambda _key, **_kwargs: None
    )

    with pytest.raises(
        BLEInterface.BLEError,
        match=re.escape(
            "invalid connect_timeout: connect_timeout must be a finite positive number of seconds."
        ),
    ):
        iface.connect("AA:BB:CC:DD:EE:10", connect_timeout=cast(Any, 0.0))


def test_validate_connect_timeout_override_rejects_non_numeric_values() -> None:
    """_validate_connect_timeout_override should wrap non-numeric overrides as BLEError."""
    iface = _build_minimal_connect_test_interface()
    with pytest.raises(BLEInterface.BLEError, match="invalid connect_timeout"):
        iface._validate_connect_timeout_override(
            cast(object, "invalid-timeout"),
            pair_on_connect=False,
        )


def test_finish_management_operation_clamps_underflow(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """_finish_management_operation() should clamp negative accounting to zero."""
    iface = _build_minimal_connect_test_interface()
    with iface._management_lock:
        iface._management_inflight = -1
    notify_calls: list[bool] = []
    monkeypatch.setattr(
        iface._management_idle_condition,
        "notify_all",
        lambda: notify_calls.append(True),
        raising=True,
    )

    with caplog.at_level(logging.WARNING):
        iface._finish_management_operation()

    with iface._management_lock:
        assert iface._management_inflight == 0
    assert notify_calls == [True]
    assert any("underflow" in record.message.lower() for record in caplog.records)


def test_finish_management_operation_notifies_when_count_reaches_zero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """_finish_management_operation() should notify waiters on zero transition."""
    iface = _build_minimal_connect_test_interface()
    with iface._management_lock:
        iface._management_inflight = 1
    notify_calls: list[bool] = []
    monkeypatch.setattr(
        iface._management_idle_condition,
        "notify_all",
        lambda: notify_calls.append(True),
        raising=True,
    )

    iface._finish_management_operation()

    with iface._management_lock:
        assert iface._management_inflight == 0
    assert notify_calls == [True]


def test_finish_management_operation_does_not_notify_above_zero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """_finish_management_operation() should not notify while inflight remains positive."""
    iface = _build_minimal_connect_test_interface()
    with iface._management_lock:
        iface._management_inflight = 2
    notify_calls: list[bool] = []
    monkeypatch.setattr(
        iface._management_idle_condition,
        "notify_all",
        lambda: notify_calls.append(True),
        raising=True,
    )

    iface._finish_management_operation()

    with iface._management_lock:
        assert iface._management_inflight == 1
    assert notify_calls == []


@pytest.mark.unit
def test_connect_rejects_non_bool_pair_override() -> None:
    """connect() should fail fast when `pair` is not explicitly bool/None."""
    iface = _build_minimal_connect_test_interface()
    with pytest.raises(BLEInterface.BLEError, match="pair must be a bool"):
        iface.connect("AA:BB:CC:DD:EE:10", pair=cast(Any, "false"))


@pytest.mark.unit
def test_connect_waits_for_inflight_management_before_establishing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """connect() should wait until in-flight management operations finish."""
    iface = _build_minimal_connect_test_interface()
    iface._management_lock = threading.RLock()
    iface._management_idle_condition = threading.Condition(iface._management_lock)
    iface._management_inflight = 1

    wait_calls: list[bool] = []
    establish_calls: list[bool] = []

    def _wait_for_management(timeout: float | None = None) -> bool:
        _ = timeout
        wait_calls.append(True)
        iface._management_inflight = 0
        return True

    monkeypatch.setattr(
        iface._management_idle_condition,
        "wait",
        _wait_for_management,
        raising=True,
    )
    monkeypatch.setattr(iface, "_validate_connection_preconditions", lambda: None)
    monkeypatch.setattr(iface, "_get_existing_client_if_valid", lambda _req: None)
    monkeypatch.setattr(
        iface, "_raise_if_duplicate_connect", lambda _key, **_kwargs: None
    )
    monkeypatch.setattr(iface, "_finalize_connection_gates", lambda *_args: None)
    monkeypatch.setattr(
        iface,
        "_verify_and_publish_connected",
        lambda *_args, **_kwargs: None,
    )

    def _establish_stub(
        _address: str | None,
        _normalized_request: str | None,
        _address_key: str | None,
        *,
        pair_on_connect: bool = False,
        connect_timeout: float | None = None,
    ) -> tuple[DummyClient, str | None, str | None]:
        _ = (pair_on_connect, connect_timeout)
        establish_calls.append(True)
        client = DummyClient()
        with iface._state_lock:
            cast(Any, iface).client = client
            iface.address = client.address
            iface._state_manager._reset_to_disconnected()
            assert iface._state_manager._transition_to(ConnectionState.CONNECTING)
            assert iface._state_manager._transition_to(ConnectionState.CONNECTED)
        return client, None, None

    monkeypatch.setattr(iface, "_establish_and_update_client", _establish_stub)

    iface.connect("AA:BB:CC:DD:EE:10")

    assert wait_calls == [True]
    assert establish_calls == [True]


def test_connect_returns_preexisting_client_before_resolving_target(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """connect() should return immediately when a preexisting client already satisfies the request."""
    iface = _build_minimal_connect_test_interface()
    existing_client = cast(BLEClient, DummyClient())
    monkeypatch.setattr(iface, "_validate_connection_preconditions", lambda: None)
    monkeypatch.setattr(
        iface, "_get_existing_client_if_valid", lambda _request: existing_client
    )
    monkeypatch.setattr(
        iface,
        "_resolve_target_address_for_connect",
        lambda _identifier: pytest.fail("resolution should not run"),
    )

    assert iface.connect("AA:BB:CC:DD:EE:10") is existing_client


def test_connect_times_out_waiting_for_management_operations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """connect() should abort when inflight management never drains."""
    iface = _build_minimal_connect_test_interface()
    iface._management_lock = threading.RLock()
    iface._management_idle_condition = threading.Condition(iface._management_lock)
    iface._management_inflight = 1

    monotonic_values = iter([0.0, 999.0])
    monkeypatch.setattr(
        "meshtastic.interfaces.ble.interface.time.monotonic",
        lambda: next(monotonic_values),
    )
    monkeypatch.setattr(
        iface._management_idle_condition,
        "wait",
        lambda timeout=None: False,
        raising=True,
    )
    monkeypatch.setattr(iface, "_validate_connection_preconditions", lambda: None)
    monkeypatch.setattr(iface, "_get_existing_client_if_valid", lambda _request: None)

    with pytest.raises(BLEInterface.BLEError, match=ERROR_MANAGEMENT_CONNECTING):
        iface.connect("AA:BB:CC:DD:EE:10")


def test_connect_times_out_on_spurious_management_wakeups(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """connect() should enforce timeout even if management wait wakes spuriously."""
    iface = _build_minimal_connect_test_interface()
    iface._management_lock = threading.RLock()
    iface._management_idle_condition = threading.Condition(iface._management_lock)
    iface._management_inflight = 1
    wait_calls: list[float | None] = []
    fake_time = 0.0

    monkeypatch.setattr(
        "meshtastic.interfaces.ble.interface._MANAGEMENT_CONNECT_WAIT_TIMEOUT_SECONDS",
        0.03,
    )
    monkeypatch.setattr(
        "meshtastic.interfaces.ble.interface._MANAGEMENT_CONNECT_WAIT_POLL_SECONDS",
        0.005,
    )

    def _monotonic() -> float:
        """
        Advance and return a test monotonic timestamp by 0.01 seconds.

        Returns
        -------
            Current monotonic time in seconds; the returned value increases by 0.01 on each call.
        """
        nonlocal fake_time
        fake_time += 0.01
        return fake_time

    monkeypatch.setattr(
        "meshtastic.interfaces.ble.interface.time.monotonic", _monotonic
    )

    def _spurious_wait(timeout: float | None = None) -> bool:
        """
        Record a spurious wait invocation and signal a wakeup.

        Parameters
        ----------
            timeout (float | None): The timeout value passed to the wait; may be None.

        Returns
        -------
            bool: `True` to indicate a spurious wakeup.

        Raises
        ------
            AssertionError: If the number of recorded wait calls exceeds the configured budget.
        """
        wait_calls.append(timeout)
        if len(wait_calls) > _MAX_SPURIOUS_CONNECT_WAIT_CALLS_BEFORE_FAIL:
            raise AssertionError("connect() kept waiting past the timeout budget")
        return True

    monkeypatch.setattr(
        iface._management_idle_condition,
        "wait",
        _spurious_wait,
        raising=True,
    )
    monkeypatch.setattr(iface, "_validate_connection_preconditions", lambda: None)
    monkeypatch.setattr(iface, "_get_existing_client_if_valid", lambda _request: None)

    with pytest.raises(BLEInterface.BLEError, match=ERROR_MANAGEMENT_CONNECTING):
        iface.connect("AA:BB:CC:DD:EE:10")

    assert wait_calls


def test_connect_management_wait_timeout_resets_between_wait_cycles(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """connect() should restart timeout accounting after management drains."""
    iface = _build_minimal_connect_test_interface()
    iface._management_lock = threading.RLock()
    iface._management_idle_condition = threading.Condition(iface._management_lock)
    iface._management_inflight = 1
    wait_calls: list[float | None] = []
    duplicate_checks = 0
    monotonic_values = iter([0.0, 0.1, 100.0, 100.1])
    last_time = 100.1

    monkeypatch.setattr(
        "meshtastic.interfaces.ble.interface._MANAGEMENT_CONNECT_WAIT_TIMEOUT_SECONDS",
        1.0,
    )
    monkeypatch.setattr(
        "meshtastic.interfaces.ble.interface._MANAGEMENT_CONNECT_WAIT_POLL_SECONDS",
        0.1,
    )

    def _monotonic() -> float:
        """
        Provide a monotonic timestamp for tests, advancing through a preset sequence and falling back to incremental steps when the sequence is exhausted.

        Returns
        -------
            float: The current monotonic time value and updates the captured `last_time` variable.
        """
        nonlocal last_time
        try:
            last_time = next(monotonic_values)
        except StopIteration:
            last_time += 0.1
        return last_time

    monkeypatch.setattr(
        "meshtastic.interfaces.ble.interface.time.monotonic", _monotonic
    )

    def _wait_for_management(timeout: float | None = None) -> bool:
        """
        Simulate waiting for management operations to drain and record the requested timeout.

        Parameters
        ----------
            timeout (float | None): Maximum seconds to wait, or `None` to indicate an indefinite wait.

        Behavior:
            Appends the provided `timeout` value to the `wait_calls` list and sets
            `iface._management_inflight` to 0 to indicate no inflight management operations.

        Returns
        -------
            bool: `True` to indicate the wait condition was signaled.
        """
        wait_calls.append(timeout)
        iface._management_inflight = 0
        return True

    def _raise_if_duplicate(_key: str | None, **_kwargs: object) -> None:
        """
        Increment the duplicate-check counter and, on the second invocation, mark the interface as having one inflight management operation.

        Parameters
        ----------
            _key (str | None): Ignored; present to match the duplicate-check callback signature.
        """
        nonlocal duplicate_checks
        duplicate_checks += 1
        if duplicate_checks == 2:
            iface._management_inflight = 1

    monkeypatch.setattr(
        iface._management_idle_condition,
        "wait",
        _wait_for_management,
        raising=True,
    )
    monkeypatch.setattr(iface, "_validate_connection_preconditions", lambda: None)
    monkeypatch.setattr(iface, "_get_existing_client_if_valid", lambda _request: None)
    monkeypatch.setattr(
        iface,
        "_resolve_target_address_for_connect",
        lambda identifier: cast(str, identifier),
    )
    monkeypatch.setattr(iface, "_raise_if_duplicate_connect", _raise_if_duplicate)
    monkeypatch.setattr(iface, "_finalize_connection_gates", lambda *_args: None)
    monkeypatch.setattr(
        iface,
        "_verify_and_publish_connected",
        lambda *_args, **_kwargs: None,
    )

    def _establish_stub(
        _address: str | None,
        _normalized_request: str | None,
        _address_key: str | None,
        *,
        pair_on_connect: bool = False,
        connect_timeout: float | None = None,
    ) -> tuple[DummyClient, str | None, str | None]:
        """
        Create and attach a DummyClient to the test BLE interface and mark it as connected.

        Parameters
        ----------
            _address (str | None): Ignored; present to match the real establish signature.
            _normalized_request (str | None): Ignored; present to match the real establish signature.
            _address_key (str | None): Ignored; present to match the real establish signature.
            pair_on_connect (bool): Accepted but ignored by this test stub.
            connect_timeout (float | None): Accepted but ignored by this test stub.

        Returns
        -------
            tuple[DummyClient, None, None]: The created DummyClient instance and two None placeholders (resolved address and resolved identifier).
        """
        _ = (pair_on_connect, connect_timeout)
        client = DummyClient()
        with iface._state_lock:
            cast(Any, iface).client = client
            iface.address = client.address
            iface._state_manager._reset_to_disconnected()
            assert iface._state_manager._transition_to(ConnectionState.CONNECTING)
            assert iface._state_manager._transition_to(ConnectionState.CONNECTED)
        return client, None, None

    monkeypatch.setattr(iface, "_establish_and_update_client", _establish_stub)

    assert isinstance(iface.connect("AA:BB:CC:DD:EE:10"), DummyClient)
    assert len(wait_calls) == 2


def test_connect_retries_when_management_becomes_inflight_inside_connect_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """connect() should continue outer loop when management starts after address-gate wait."""
    iface = _build_minimal_connect_test_interface()
    iface._management_lock = threading.RLock()
    iface._management_idle_condition = threading.Condition(iface._management_lock)
    iface._management_inflight = 0
    established: list[bool] = []
    duplicate_checks = 0

    def _wait_for_management(timeout: float | None = None) -> bool:
        _ = timeout
        iface._management_inflight = 0
        return True

    def _raise_if_duplicate(_key: str | None, **_kwargs: object) -> None:
        nonlocal duplicate_checks
        duplicate_checks += 1
        if duplicate_checks == 2:
            iface._management_inflight = 1

    monkeypatch.setattr(
        iface._management_idle_condition,
        "wait",
        _wait_for_management,
        raising=True,
    )
    monkeypatch.setattr(iface, "_validate_connection_preconditions", lambda: None)
    monkeypatch.setattr(iface, "_get_existing_client_if_valid", lambda _request: None)
    monkeypatch.setattr(
        iface,
        "_resolve_target_address_for_connect",
        lambda identifier: cast(str, identifier),
    )
    monkeypatch.setattr(iface, "_raise_if_duplicate_connect", _raise_if_duplicate)
    monkeypatch.setattr(iface, "_finalize_connection_gates", lambda *_args: None)
    monkeypatch.setattr(
        iface,
        "_verify_and_publish_connected",
        lambda *_args, **_kwargs: None,
    )

    def _establish_stub(
        _address: str | None,
        _normalized_request: str | None,
        _address_key: str | None,
        *,
        pair_on_connect: bool = False,
        connect_timeout: float | None = None,
    ) -> tuple[DummyClient, str | None, str | None]:
        _ = (pair_on_connect, connect_timeout)
        established.append(True)
        client = DummyClient()
        with iface._state_lock:
            cast(Any, iface).client = client
            iface.address = client.address
            iface._state_manager._reset_to_disconnected()
            assert iface._state_manager._transition_to(ConnectionState.CONNECTING)
            assert iface._state_manager._transition_to(ConnectionState.CONNECTED)
        return client, None, None

    monkeypatch.setattr(iface, "_establish_and_update_client", _establish_stub)

    assert isinstance(iface.connect("AA:BB:CC:DD:EE:10"), DummyClient)
    assert established == [True]
    assert duplicate_checks >= 4


def test_connect_returns_existing_client_after_lock_recheck(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """connect() should return an existing client found during in-lock recheck."""
    iface = _build_minimal_connect_test_interface()
    existing_client = cast(BLEClient, DummyClient())
    lookup_count = 0

    def _lookup_existing(_request: str | None) -> BLEClient | None:
        nonlocal lookup_count
        lookup_count += 1
        if lookup_count == 1:
            return None
        return existing_client

    monkeypatch.setattr(iface, "_validate_connection_preconditions", lambda: None)
    monkeypatch.setattr(iface, "_get_existing_client_if_valid", _lookup_existing)
    monkeypatch.setattr(
        iface,
        "_resolve_target_address_for_connect",
        lambda identifier: cast(str, identifier),
    )
    monkeypatch.setattr(
        iface, "_raise_if_duplicate_connect", lambda _key, **_kwargs: None
    )

    assert iface.connect("AA:BB:CC:DD:EE:10") is existing_client


def test_connect_raises_when_establish_returns_no_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """connect() should fail fast when establishment returns no client object."""
    iface = _build_minimal_connect_test_interface()
    monkeypatch.setattr(iface, "_validate_connection_preconditions", lambda: None)
    monkeypatch.setattr(iface, "_get_existing_client_if_valid", lambda _request: None)
    monkeypatch.setattr(
        iface,
        "_resolve_target_address_for_connect",
        lambda identifier: cast(str, identifier),
    )
    monkeypatch.setattr(
        iface, "_raise_if_duplicate_connect", lambda _key, **_kwargs: None
    )
    monkeypatch.setattr(
        iface,
        "_establish_and_update_client",
        lambda *_args, **_kwargs: (cast(DummyClient, None), None, None),
    )

    with pytest.raises(BLEInterface.BLEError, match="no BLE client established"):
        iface.connect("AA:BB:CC:DD:EE:10")


def test_connect_does_not_relabel_unrelated_establish_connection_value_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """connect() should preserve unrelated ValueError failures from orchestration."""
    iface = _build_minimal_connect_test_interface()
    cast(Any, iface)._connection_orchestrator = SimpleNamespace(
        _establish_connection=lambda *_args, **_kwargs: (_ for _ in ()).throw(
            ValueError("boom")
        )
    )

    monkeypatch.setattr(iface, "_validate_connection_preconditions", lambda: None)
    monkeypatch.setattr(iface, "_get_existing_client_if_valid", lambda _req: None)
    monkeypatch.setattr(
        iface, "_raise_if_duplicate_connect", lambda _key, **_kwargs: None
    )

    with pytest.raises(ValueError, match="boom"):
        iface.connect("AA:BB:CC:DD:EE:10", connect_timeout=4.5)


def test_ble_interface_establish_and_update_client_discards_late_connection_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Late connect results should be closed instead of being published during shutdown."""
    iface = _build_interface(monkeypatch, DummyClient(), start_receive_thread=False)
    connected_client = DummyClient()
    connected_client.address = "AA:BB:CC:DD:EE:FF"
    connected_client.bleak_client = SimpleNamespace(address="AA:BB:CC:DD:EE:FF")
    cleanup_calls: list[object] = []

    monkeypatch.setattr(
        iface._connection_orchestrator,
        "_establish_connection",
        lambda *_args, **_kwargs: cast(BLEClient, connected_client),
    )
    monkeypatch.setattr(
        iface._client_manager,
        "_safe_close_client",
        lambda client: cleanup_calls.append(client),
    )

    with iface._connect_lock:
        with iface._state_lock:
            iface._closed = True
        with pytest.raises(BLEInterface.BLEError, match="closing"):
            iface._establish_and_update_client(
                "AA:BB:CC:DD:EE:FF",
                "aabbccddeeff",
                "aabbccddeeff",
                pair_on_connect=False,
            )

    assert cleanup_calls == [connected_client]
    with iface._state_lock:
        assert cast(object, iface.client) is not connected_client
    iface.close()


def test_establish_and_update_client_sets_last_request_from_device_and_updates_previous(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Established clients should refresh last request from device key and close replaced clients."""
    iface = _build_interface(monkeypatch, DummyClient(), start_receive_thread=False)
    previous_client = DummyClient()
    previous_client.address = "11:22:33:44:55:66"
    previous_client.bleak_client = SimpleNamespace(address=previous_client.address)
    connected_client = DummyClient()
    connected_client.address = "AA:BB:CC:DD:EE:FF"
    connected_client.bleak_client = SimpleNamespace(address=connected_client.address)
    updated_refs: list[tuple[BLEClient, BLEClient | None]] = []

    monkeypatch.setattr(
        iface._connection_orchestrator,
        "_establish_connection",
        lambda *_args, **_kwargs: cast(BLEClient, connected_client),
    )
    monkeypatch.setattr(
        iface._client_manager,
        "_update_client_reference",
        lambda new_client, old_client: updated_refs.append((new_client, old_client)),
    )

    with iface._state_lock:
        cast(Any, iface).client = previous_client
        iface._state_manager._reset_to_disconnected()
        assert iface._state_manager._transition_to(ConnectionState.CONNECTING) is True
        assert iface._state_manager._transition_to(ConnectionState.CONNECTED) is True

    with iface._connect_lock:
        result_client, _, _ = iface._establish_and_update_client(
            "AA:BB:CC:DD:EE:FF",
            None,
            "aabbccddeeff",
            pair_on_connect=False,
        )

    assert result_client is connected_client
    assert updated_refs == [(cast(BLEClient, connected_client), previous_client)]
    assert iface._last_connection_request == iface._sanitize_address(
        connected_client.address
    )
    iface.close()


def test_handle_disconnect_ignores_stale_callbacks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Stale disconnect callbacks must not clear the current active client."""
    stale_client = DummyClient()
    iface = _build_interface(monkeypatch, stale_client)

    active_client = DummyClient()
    active_client.address = "active"
    active_client.bleak_client = SimpleNamespace(address="active")
    reconnect_calls: list[bool] = []
    disconnected_calls: list[bool] = []

    monkeypatch.setattr(
        iface,
        "_schedule_auto_reconnect",
        lambda: reconnect_calls.append(True),
        raising=True,
    )
    monkeypatch.setattr(
        iface, "_disconnected", lambda: disconnected_calls.append(True), raising=True
    )

    with iface._state_lock:
        cast(Any, iface).client = active_client
        iface._disconnect_notified = False
        iface._state_manager._reset_to_disconnected()
        assert iface._state_manager._transition_to(ConnectionState.CONNECTING) is True
        assert iface._state_manager._transition_to(ConnectionState.CONNECTED) is True

    # Stale callback by BLEClient instance should be ignored.
    assert iface._handle_disconnect("stale-client", client=stale_client) is True  # type: ignore[arg-type]
    # Stale callback by bleak client identity should also be ignored.
    assert (
        iface._handle_disconnect("stale-bleak", bleak_client=stale_client.bleak_client)  # type: ignore[arg-type]
        is True
    )

    assert cast(object, iface.client) is active_client
    assert iface._disconnect_notified is False
    assert reconnect_calls == []
    assert disconnected_calls == []

    iface.close()


def test_discard_invalidated_connected_client_marks_stale_callbacks_notified(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Discarded clients should not trigger a second disconnect via their stale callback."""
    iface = _build_interface(monkeypatch, DummyClient(), start_receive_thread=False)
    discarded_client = DummyClient()
    discarded_client.address = "AA:BB:CC:DD:EE:FF"
    discarded_client.bleak_client = SimpleNamespace(address=discarded_client.address)
    disconnected_calls: list[bool] = []
    reconnect_calls: list[bool] = []
    callback_results: list[bool] = []

    monkeypatch.setattr(
        iface, "_disconnected", lambda: disconnected_calls.append(True), raising=True
    )
    monkeypatch.setattr(
        iface,
        "_schedule_auto_reconnect",
        lambda: reconnect_calls.append(True),
        raising=True,
    )

    def _safe_close_client(client: BLEClient) -> None:
        callback_results.append(
            iface._handle_disconnect("discarded-client", client=client)
        )

    monkeypatch.setattr(
        iface._client_manager,
        "_safe_close_client",
        _safe_close_client,
        raising=True,
    )

    with iface._state_lock:
        cast(Any, iface).client = discarded_client
        iface.address = discarded_client.address
        iface._disconnect_notified = False
        iface._state_manager._reset_to_disconnected()
        assert iface._state_manager._transition_to(ConnectionState.CONNECTING) is True
        assert iface._state_manager._transition_to(ConnectionState.CONNECTED) is True

    iface._discard_invalidated_connected_client(cast(BLEClient, discarded_client))

    assert callback_results == [True]
    assert disconnected_calls == []
    assert reconnect_calls == []
    with iface._state_lock:
        assert iface.client is None
        assert iface.address is None
        assert iface._disconnect_notified is True
        assert iface._state_manager._current_state == ConnectionState.DISCONNECTED

    iface.close()


def test_discard_invalidated_connected_client_clears_pending_when_already_detached(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pending publish flag should clear even if the provisional client already detached."""
    iface = _build_interface(monkeypatch, DummyClient(), start_receive_thread=False)
    discarded_client = DummyClient()
    discarded_client.address = "AA:BB:CC:DD:EE:44"
    discarded_client.bleak_client = SimpleNamespace(address=discarded_client.address)
    closed_clients: list[BLEClient] = []

    monkeypatch.setattr(
        iface._client_manager,
        "_safe_close_client",
        lambda client: closed_clients.append(client),
        raising=True,
    )

    with iface._state_lock:
        cast(Any, iface).client = None
        iface._client_publish_pending = True
        iface._connected_publish_inflight_client = cast(BLEClient, discarded_client)
        iface._disconnect_notified = False
        iface._state_manager._reset_to_disconnected()
        assert iface._state_manager._transition_to(ConnectionState.CONNECTING) is True

    iface._discard_invalidated_connected_client(cast(BLEClient, discarded_client))

    assert closed_clients == [cast(BLEClient, discarded_client)]
    with iface._state_lock:
        assert iface.client is None
        assert iface._client_publish_pending is False
        assert iface._disconnect_notified is False

    iface.close()


def test_discard_invalidated_connected_client_emits_disconnect_for_retired_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Replacement-pending stale sessions should emit one disconnect publication."""
    from meshtastic import mesh_interface as mesh_iface_module

    iface = _build_interface(monkeypatch, DummyClient(), start_receive_thread=False)
    discarded_client = DummyClient()
    discarded_client.address = "AA:BB:CC:DD:EE:45"
    discarded_client.bleak_client = SimpleNamespace(address=discarded_client.address)
    closed_clients: list[BLEClient] = []
    published_topics: list[str] = []

    monkeypatch.setattr(
        iface._client_manager,
        "_safe_close_client",
        lambda client: closed_clients.append(client),
        raising=True,
    )
    monkeypatch.setattr(
        mesh_iface_module,
        "pub",
        SimpleNamespace(
            sendMessage=lambda topic, **_kwargs: published_topics.append(topic)
        ),
        raising=True,
    )
    monkeypatch.setattr(
        mesh_iface_module,
        "publishingThread",
        SimpleNamespace(queueWork=lambda callback: callback()),
        raising=True,
    )

    with iface._state_lock:
        cast(Any, iface).client = None
        iface._client_publish_pending = True
        iface._client_replacement_pending = True
        iface._connected_publish_inflight_client = cast(BLEClient, discarded_client)
        iface._disconnect_notified = False
        iface._state_manager._reset_to_disconnected()
        assert iface._state_manager._transition_to(ConnectionState.CONNECTING) is True
    with iface._heartbeat_lock:
        iface.isConnected.set()

    iface._discard_invalidated_connected_client(cast(BLEClient, discarded_client))
    assert published_topics.count("meshtastic.connection.lost") == 1

    assert closed_clients == [cast(BLEClient, discarded_client)]
    with iface._state_lock:
        assert iface.client is None
        assert iface._client_publish_pending is False
        assert iface._client_replacement_pending is False
        assert iface._disconnect_notified is True

    iface.close()
    assert published_topics.count("meshtastic.connection.lost") == 1


@pytest.mark.parametrize("is_closing", [True, False])
def test_finalize_connection_gates_cleans_up_when_client_loses_ownership_mid_finalize(
    monkeypatch: pytest.MonkeyPatch,
    is_closing: bool,
) -> None:
    """Gate finalization should clean up provisional claims when ownership disappears mid-finalize."""
    from meshtastic.interfaces.ble.lifecycle_service import BLELifecycleService

    iface = _build_interface(monkeypatch, DummyClient(), start_receive_thread=False)
    connected_client = DummyClient()
    cleanup_calls: list[tuple[str | None, str | None]] = []

    # Set up interface state to simulate that we had claimed ownership
    # before ownership was lost mid-finalize
    with iface._state_lock:
        iface.client = cast(BLEClient, connected_client)
        iface._connection_alias_key = "alias-key"

    monkeypatch.setattr(
        BLELifecycleService,
        "_get_connected_client_status",
        lambda _iface, _client: (True, False),
        raising=True,
    )
    monkeypatch.setattr(
        BLELifecycleService,
        "_get_connected_client_status_locked",
        lambda _iface, _client: (False, is_closing),
        raising=True,
    )
    monkeypatch.setattr(iface, "_mark_address_keys_connected", lambda *_keys: None)
    monkeypatch.setattr(
        iface,
        "_mark_address_keys_disconnected",
        lambda *keys: cleanup_calls.append(cast(tuple[str | None, str | None], keys)),
    )

    iface._finalize_connection_gates(
        cast(BLEClient, connected_client), "device-key", "alias-key"
    )

    assert cleanup_calls == [("device-key", "alias-key")]
    assert iface._connection_alias_key is None
    iface.close()


@pytest.mark.parametrize("is_closing", [True, False])
def test_finalize_connection_gates_logs_when_result_is_already_stale(
    monkeypatch: pytest.MonkeyPatch,
    is_closing: bool,
) -> None:
    """Gate finalization should no-op when initial ownership check already reports stale result."""
    from meshtastic.interfaces.ble.lifecycle_service import BLELifecycleService

    iface = _build_interface(monkeypatch, DummyClient(), start_receive_thread=False)
    connected_client = DummyClient()
    mark_connected_calls: list[tuple[str | None, str | None]] = []
    mark_disconnected_calls: list[tuple[str | None, str | None]] = []

    monkeypatch.setattr(
        BLELifecycleService,
        "_get_connected_client_status",
        lambda _iface, _client: (False, is_closing),
        raising=True,
    )
    monkeypatch.setattr(
        iface,
        "_mark_address_keys_connected",
        lambda *keys: mark_connected_calls.append(
            cast(tuple[str | None, str | None], keys)
        ),
        raising=True,
    )
    monkeypatch.setattr(
        iface,
        "_mark_address_keys_disconnected",
        lambda *keys: mark_disconnected_calls.append(
            cast(tuple[str | None, str | None], keys)
        ),
        raising=True,
    )

    iface._finalize_connection_gates(
        cast(BLEClient, connected_client), "device-key", "alias-key"
    )

    assert mark_connected_calls == []
    assert mark_disconnected_calls == []
    iface.close()


def test_is_owned_connected_client_reads_status_tuple(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Owned-client helper should return the first element of status tuple."""
    from meshtastic.interfaces.ble.lifecycle_service import BLELifecycleService

    iface = _build_interface(monkeypatch, DummyClient(), start_receive_thread=False)
    client = cast(BLEClient, DummyClient())
    monkeypatch.setattr(
        BLELifecycleService,
        "_get_connected_client_status",
        lambda _iface, _client: (True, False),
        raising=True,
    )
    assert iface._is_owned_connected_client(client) is True
    iface.close()


def test_emit_verified_connection_side_effects_sets_reconnected_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verified publish should signal reconnected_event for reconnect publishes."""
    iface = _build_interface(monkeypatch, DummyClient(), start_receive_thread=False)
    connected_client = DummyClient()
    connected_client.address = "AA:BB:CC:DD:EE:FF"
    connected_client.bleak_client = SimpleNamespace(address=connected_client.address)
    set_events: list[str] = []
    monkeypatch.setattr(
        iface.thread_coordinator,
        "_set_event",
        lambda event_name: set_events.append(event_name),
        raising=True,
    )

    iface._prior_publish_was_reconnect = True
    iface._emit_verified_connection_side_effects(cast(BLEClient, connected_client))

    assert set_events == ["reconnected_event"]
    assert iface._prior_publish_was_reconnect is False
    iface.close()


def test_thread_event_dispatcher_resolution_paths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Thread-event dispatcher resolution should cover underscored and missing hooks."""
    iface = _build_interface(monkeypatch, DummyClient(), start_receive_thread=False)
    original_close = iface.close
    original_thread_coordinator = iface.thread_coordinator
    try:
        class_calls: list[str] = []

        class _ClassCoordinator:
            def _set_event(self, event_name: str) -> None:
                class_calls.append(event_name)

        class_dispatch = BLEInterface._resolve_thread_event_dispatcher(
            _ClassCoordinator()
        )
        assert callable(class_dispatch)
        cast(Callable[[str], None], class_dispatch)("class-evt")
        assert class_calls == ["class-evt"]
        iface.thread_coordinator = _ClassCoordinator()
        iface._set_thread_event("class-evt-2")
        assert class_calls == ["class-evt", "class-evt-2"]

        instance_calls: list[str] = []

        def _instance_set_event(event_name: str) -> None:
            instance_calls.append(event_name)

        instance_coordinator = SimpleNamespace(_set_event=_instance_set_event)
        instance_dispatch = BLEInterface._resolve_thread_event_dispatcher(
            instance_coordinator
        )
        assert callable(instance_dispatch)
        cast(Callable[[str], None], instance_dispatch)("instance-evt")
        assert instance_calls == ["instance-evt"]

        missing_coordinator = SimpleNamespace()
        assert (
            BLEInterface._resolve_thread_event_dispatcher(missing_coordinator) is None
        )

        debug_messages: list[str] = []
        monkeypatch.setattr(
            "meshtastic.interfaces.ble.interface.logger.debug",
            lambda message, *_args, **_kwargs: debug_messages.append(
                cast(str, message)
            ),
        )
        iface.thread_coordinator = missing_coordinator
        iface._set_thread_event("missing-evt")
        assert any(
            "No callable thread event dispatcher available" in message
            for message in debug_messages
        )
    finally:
        iface.thread_coordinator = original_thread_coordinator
        original_close()
