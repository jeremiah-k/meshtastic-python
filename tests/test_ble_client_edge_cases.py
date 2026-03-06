"""Additional edge case tests for BLE client functionality."""

import asyncio
import re
import threading
from collections.abc import Awaitable, Callable
from typing import Any, cast

import pytest

try:
    from bleak.exc import BleakError

    from meshtastic.interfaces.ble.client import (
        SERVICE_CHARACTERISTIC_RETRY_COUNT,
        BLEClient,
    )
    from meshtastic.interfaces.ble.constants import (
        BLECLIENT_ERROR_ASYNC_TIMEOUT,
        BLECLIENT_ERROR_CANCELLED,
        BLECLIENT_ERROR_CANNOT_CONNECT_NOT_INITIALIZED,
        BLECLIENT_ERROR_CANNOT_DISCONNECT_NOT_INITIALIZED,
        BLECLIENT_ERROR_CANNOT_GET_SERVICES_NOT_INITIALIZED,
        BLECLIENT_ERROR_CANNOT_PAIR_NOT_INITIALIZED,
        BLECLIENT_ERROR_CANNOT_PAIR_UNSUPPORTED,
        BLECLIENT_ERROR_CANNOT_READ_NOT_INITIALIZED,
        BLECLIENT_ERROR_CANNOT_START_NOTIFY_NOT_INITIALIZED,
        BLECLIENT_ERROR_CANNOT_STOP_NOTIFY_NOT_INITIALIZED,
        BLECLIENT_ERROR_CANNOT_UNPAIR_NOT_INITIALIZED,
        BLECLIENT_ERROR_CANNOT_UNPAIR_UNSUPPORTED,
        BLECLIENT_ERROR_CANNOT_WRITE_NOT_INITIALIZED,
        BLECLIENT_ERROR_RUNNER_THREAD_WAIT,
        BLECLIENT_MANAGEMENT_AWAIT_TIMEOUT,
        ERROR_MANAGEMENT_AWAIT_TIMEOUT_INVALID,
    )
except ImportError:
    pytest.skip("BLE dependencies not available", allow_module_level=True)

TRANSIENT_SERVICES_LOOKUP_FAILURE = "transient services lookup failure"


def _make_run_awaitable(
    captured_timeout: list[float | None] | None = None,
) -> Callable[[Awaitable[object], float | None], object]:
    """Create a simple `_async_await` stand-in that runs the awaitable locally."""

    def _run_awaitable(
        awaitable: Awaitable[object],
        timeout: float | None = None,
    ) -> object:
        if captured_timeout is not None:
            captured_timeout.append(timeout)
        return asyncio.run(awaitable)

    return _run_awaitable


@pytest.mark.unit
def test_bleclient_discovery_mode_without_address(ble_client: BLEClient) -> None:
    """BLEClient should support discovery-only mode when initialized without an address."""
    assert ble_client.address is None
    assert ble_client.bleak_client is None
    assert not ble_client.isConnected()


@pytest.mark.unit
def test_bleclient_isConnected_handles_missing_bleak_client(
    ble_client: BLEClient,
) -> None:
    """IsConnected should return False when bleak_client is None."""
    assert not ble_client.isConnected()


@pytest.mark.unit
def test_bleclient_is_connected_alias(ble_client: BLEClient) -> None:
    """is_connected should be an alias for isConnected."""
    assert ble_client.is_connected() == ble_client.isConnected()


@pytest.mark.unit
def test_bleclient_close_is_idempotent(ble_client: BLEClient) -> None:
    """close() should be idempotent and safe to call multiple times."""
    ble_client.close()
    ble_client.close()  # Should not raise
    ble_client.close()  # Should not raise


@pytest.mark.unit
def test_bleclient_context_manager() -> None:
    """BLEClient should work as a context manager."""
    with BLEClient(address=None) as client:
        assert client is not None
    assert not client.isConnected()


@pytest.mark.unit
def test_bleclient_error_class_exists() -> None:
    """BLEClient should have a BLEError exception class."""
    assert hasattr(BLEClient, "BLEError")
    assert issubclass(BLEClient.BLEError, Exception)


@pytest.mark.unit
def test_bleclient_operations_require_initialized_client(ble_client: BLEClient) -> None:
    """BLEClient operations should raise BLEError when bleak_client is not initialized."""
    with pytest.raises(
        BLEClient.BLEError, match=BLECLIENT_ERROR_CANNOT_CONNECT_NOT_INITIALIZED
    ):
        ble_client.connect()

    with pytest.raises(
        BLEClient.BLEError, match=BLECLIENT_ERROR_CANNOT_DISCONNECT_NOT_INITIALIZED
    ):
        ble_client.disconnect()

    with pytest.raises(
        BLEClient.BLEError, match=BLECLIENT_ERROR_CANNOT_READ_NOT_INITIALIZED
    ):
        ble_client.read_gatt_char("uuid")

    with pytest.raises(
        BLEClient.BLEError, match=BLECLIENT_ERROR_CANNOT_WRITE_NOT_INITIALIZED
    ):
        ble_client.write_gatt_char("uuid", b"data")

    with pytest.raises(
        BLEClient.BLEError, match=BLECLIENT_ERROR_CANNOT_PAIR_NOT_INITIALIZED
    ):
        ble_client.pair()

    with pytest.raises(
        BLEClient.BLEError, match=BLECLIENT_ERROR_CANNOT_UNPAIR_NOT_INITIALIZED
    ):
        ble_client.unpair()

    with pytest.raises(
        BLEClient.BLEError, match=BLECLIENT_ERROR_CANNOT_GET_SERVICES_NOT_INITIALIZED
    ):
        ble_client._get_services()

    with pytest.raises(
        BLEClient.BLEError, match=BLECLIENT_ERROR_CANNOT_START_NOTIFY_NOT_INITIALIZED
    ):
        ble_client.start_notify("uuid", lambda *_args: None)

    with pytest.raises(
        BLEClient.BLEError,
        match=BLECLIENT_ERROR_CANNOT_STOP_NOTIFY_NOT_INITIALIZED,
    ):
        ble_client.stopNotify("uuid")


@pytest.mark.unit
def test_bleclient_has_characteristic_returns_false_without_client(
    ble_client: BLEClient,
) -> None:
    """has_characteristic should return False when bleak_client is None."""
    assert not ble_client.has_characteristic("some-uuid")


@pytest.mark.unit
def test_bleclient_stop_notify_alias(ble_client: BLEClient) -> None:
    """stop_notify should be an alias for stopNotify."""
    # Both should raise the same error
    with pytest.raises(
        BLEClient.BLEError,
        match=BLECLIENT_ERROR_CANNOT_STOP_NOTIFY_NOT_INITIALIZED,
    ):
        ble_client.stopNotify("uuid")
    with pytest.raises(
        BLEClient.BLEError,
        match=BLECLIENT_ERROR_CANNOT_STOP_NOTIFY_NOT_INITIALIZED,
    ):
        ble_client.stop_notify("uuid")


@pytest.mark.unit
def test_bleclient_operations_fail_when_closed(ble_client: BLEClient) -> None:
    """Operations should fail with clear error when client is closed."""
    ble_client.close()

    # Create a simple coroutine to test
    async def dummy_coro() -> str:
        return "result"

    with pytest.raises(
        BLEClient.BLEError, match="Cannot schedule operation: BLE client is closed"
    ):
        ble_client._async_await(dummy_coro())


@pytest.mark.unit
def test_bleclient_async_await_static_method() -> None:
    """BLEClient should have _with_timeout method."""
    assert hasattr(BLEClient, "_with_timeout")
    assert callable(BLEClient._with_timeout)


@pytest.mark.unit
def test_bleclient_error_constant() -> None:
    """Verify BLECLIENT_ERROR_ASYNC_TIMEOUT constant is properly defined."""
    assert BLECLIENT_ERROR_ASYNC_TIMEOUT == "Async operation timed out"


@pytest.mark.unit
def test_bleclient_discover_method_exists(ble_client: BLEClient) -> None:
    """BLEClient should have discover and _discover methods."""
    assert hasattr(ble_client, "discover")
    assert hasattr(ble_client, "_discover")
    assert hasattr(ble_client, "find_device")
    assert hasattr(ble_client, "findDevice")
    assert callable(ble_client.discover)
    assert callable(ble_client._discover)
    assert callable(ble_client.find_device)
    assert callable(ble_client.findDevice)


@pytest.mark.unit
def test_bleclient_with_timeout_delegates_to_utils_wrapper() -> None:
    """_with_timeout should execute through the shared with_timeout utility."""

    async def _done() -> str:
        return "ok"

    assert (
        asyncio.run(BLEClient._with_timeout(_done(), timeout=1.0, label="test")) == "ok"
    )


@pytest.mark.unit
def test_bleclient_has_characteristic_retries_and_skips_final_sleep(
    monkeypatch: pytest.MonkeyPatch,
    ble_client: BLEClient,
) -> None:
    """has_characteristic should retry BleakError reads and avoid sleeping after final retry."""

    class _Services:
        def get_characteristic(self, _specifier: str) -> object:
            """Raise a transient lookup error to trigger retry logic."""
            raise BleakError(TRANSIENT_SERVICES_LOOKUP_FAILURE)

    services = _Services()
    sleep_calls: list[float] = []
    ble_client.bleak_client = type("_BleakClient", (), {"services": services})()
    monkeypatch.setattr(ble_client, "_get_services", lambda: services)
    monkeypatch.setattr(
        ble_client.error_handler,
        "_safe_execute",
        lambda operation, **_kwargs: operation(),
    )

    def _record_sleep(delay: float) -> None:
        sleep_calls.append(delay)

    monkeypatch.setattr(
        "meshtastic.interfaces.ble.client.time.sleep",
        _record_sleep,
    )

    assert ble_client.has_characteristic("0000") is False
    assert len(sleep_calls) == SERVICE_CHARACTERISTIC_RETRY_COUNT - 1


@pytest.mark.unit
def test_bleclient_async_await_maps_asyncio_cancelled_to_cancelled_error(
    monkeypatch: pytest.MonkeyPatch,
    ble_client: BLEClient,
) -> None:
    """_async_await should map asyncio cancellation to BLE cancelled error."""

    class _CancelledFuture:
        def result(self, _timeout: float | None = None) -> None:
            """Raise asyncio.CancelledError to simulate cancelled future result retrieval."""
            raise asyncio.CancelledError()

    async def _dummy_coro() -> None:
        pass

    def _fake_async_run(coro: Any) -> _CancelledFuture:
        coro.close()
        return _CancelledFuture()

    monkeypatch.setattr(ble_client, "_async_run", _fake_async_run)
    with pytest.raises(BLEClient.BLEError, match=BLECLIENT_ERROR_CANCELLED):
        ble_client._async_await(_dummy_coro())


@pytest.mark.unit
def test_bleclient_async_await_rejects_wait_from_runner_thread(
    monkeypatch: pytest.MonkeyPatch,
    ble_client: BLEClient,
) -> None:
    """_async_await should fail fast when invoked from the runner thread."""

    class _FutureLike:
        def cancel(self) -> None:
            """Support best-effort cancellation in guarded path."""

    async def _dummy_coro() -> None:
        pass

    def _fake_async_run(coro: Any) -> _FutureLike:
        coro.close()
        return _FutureLike()

    monkeypatch.setattr(ble_client, "_async_run", _fake_async_run)

    fake_runner = type("_Runner", (), {"_thread": threading.current_thread()})()
    monkeypatch.setattr(ble_client, "_runner", fake_runner)

    with pytest.raises(BLEClient.BLEError, match=BLECLIENT_ERROR_RUNNER_THREAD_WAIT):
        ble_client._async_await(_dummy_coro())


@pytest.mark.unit
def test_bleclient_find_device_aliases_delegate_to_expected_targets(
    monkeypatch: pytest.MonkeyPatch,
    ble_client: BLEClient,
) -> None:
    """find_device and findDevice aliases should delegate to discover/find_device respectively."""
    discover_calls: list[dict[str, Any]] = []
    find_device_calls: list[dict[str, Any]] = []

    def _discover_stub(**kwargs: Any) -> list[str]:
        discover_calls.append(kwargs)
        return ["discovered"]

    def _find_device_stub(**kwargs: Any) -> list[str]:
        find_device_calls.append(kwargs)
        return ["found"]

    monkeypatch.setattr(ble_client, "discover", _discover_stub)
    assert ble_client.find_device(address="AA:BB") == ["discovered"]
    assert discover_calls == [{"address": "AA:BB"}]

    monkeypatch.setattr(ble_client, "find_device", _find_device_stub)
    assert ble_client.findDevice(name="node") == ["found"]
    assert find_device_calls == [{"name": "node"}]


@pytest.mark.unit
def test_bleclient_has_characteristic_returns_false_when_services_lookup_fails(
    monkeypatch: pytest.MonkeyPatch,
    ble_client: BLEClient,
) -> None:
    """has_characteristic should return False when services cannot be resolved."""

    class _BleakClientWithBrokenServices:
        @property
        def services(self) -> Any:
            """Simulate a services property that fails at access time."""
            raise BleakError("services unavailable")

    ble_client.bleak_client = cast(Any, _BleakClientWithBrokenServices())
    monkeypatch.setattr(ble_client, "_get_services", lambda: None)
    monkeypatch.setattr(
        ble_client.error_handler,
        "_safe_execute",
        lambda operation, **_kwargs: operation(),
    )

    assert ble_client.has_characteristic("0000") is False


@pytest.mark.unit
def test_bleclient_unpair_raises_when_backend_does_not_support_unpair(
    ble_client: BLEClient,
) -> None:
    """unpair() should fail with a clear message when backend lacks unpair support."""
    ble_client.bleak_client = type("_NoUnpairClient", (), {})()
    with pytest.raises(
        BLEClient.BLEError,
        match=BLECLIENT_ERROR_CANNOT_UNPAIR_UNSUPPORTED,
    ):
        ble_client.unpair()


@pytest.mark.unit
def test_bleclient_unpair_delegates_to_backend(
    monkeypatch: pytest.MonkeyPatch,
    ble_client: BLEClient,
) -> None:
    """unpair() should invoke backend unpair through _async_await."""
    backend_calls: list[bool] = []
    captured_timeout: list[float | None] = []

    class _Backend:
        async def unpair(self) -> None:
            backend_calls.append(True)
            return None

    ble_client.bleak_client = cast(Any, _Backend())
    monkeypatch.setattr(
        ble_client,
        "_async_await",
        _make_run_awaitable(captured_timeout),
    )

    ble_client.unpair(await_timeout=12.5)
    assert backend_calls == [True]
    assert captured_timeout == [12.5]


@pytest.mark.unit
def test_bleclient_unpair_translates_not_implemented_error(
    monkeypatch: pytest.MonkeyPatch,
    ble_client: BLEClient,
) -> None:
    """unpair() should wrap backend NotImplementedError as unsupported."""

    class _Backend:
        async def unpair(self) -> None:
            raise NotImplementedError

    ble_client.bleak_client = cast(Any, _Backend())
    monkeypatch.setattr(ble_client, "_async_await", _make_run_awaitable())

    with pytest.raises(
        BLEClient.BLEError, match=BLECLIENT_ERROR_CANNOT_UNPAIR_UNSUPPORTED
    ) as exc_info:
        ble_client.unpair()

    assert isinstance(exc_info.value.__cause__, NotImplementedError)


@pytest.mark.unit
def test_bleclient_pair_delegates_to_backend(
    monkeypatch: pytest.MonkeyPatch,
    ble_client: BLEClient,
) -> None:
    """pair() should invoke backend pair through _async_await and return None."""
    backend_calls: list[dict[str, object]] = []
    captured_timeout: list[float | None] = []

    class _Backend:
        async def pair(self, **kwargs: object) -> None:
            backend_calls.append(kwargs)
            return None

    ble_client.bleak_client = cast(Any, _Backend())
    monkeypatch.setattr(
        ble_client,
        "_async_await",
        _make_run_awaitable(captured_timeout),
    )

    result = ble_client.pair(confirm=True, await_timeout=9.0)
    assert result is None
    assert backend_calls == [{"confirm": True}]
    assert captured_timeout == [9.0]


@pytest.mark.unit
def test_bleclient_pair_uses_bounded_default_await_timeout(
    monkeypatch: pytest.MonkeyPatch,
    ble_client: BLEClient,
) -> None:
    """pair() should use the named bounded await timeout by default."""
    captured_timeout: list[float | None] = []

    class _Backend:
        async def pair(self, **_kwargs: object) -> None:
            return None

    ble_client.bleak_client = cast(Any, _Backend())
    monkeypatch.setattr(
        ble_client,
        "_async_await",
        _make_run_awaitable(captured_timeout),
    )

    ble_client.pair()
    assert captured_timeout == [BLECLIENT_MANAGEMENT_AWAIT_TIMEOUT]


@pytest.mark.unit
def test_bleclient_pair_translates_not_implemented_error(
    monkeypatch: pytest.MonkeyPatch,
    ble_client: BLEClient,
) -> None:
    """pair() should wrap backend NotImplementedError as unsupported."""

    class _Backend:
        async def pair(self, **_kwargs: object) -> None:
            raise NotImplementedError

    ble_client.bleak_client = cast(Any, _Backend())
    monkeypatch.setattr(ble_client, "_async_await", _make_run_awaitable())

    with pytest.raises(
        BLEClient.BLEError, match=BLECLIENT_ERROR_CANNOT_PAIR_UNSUPPORTED
    ) as exc_info:
        ble_client.pair(confirm=True)

    assert isinstance(exc_info.value.__cause__, NotImplementedError)


@pytest.mark.unit
@pytest.mark.parametrize("method_name", ["pair", "unpair"])
@pytest.mark.parametrize(
    "invalid_timeout",
    [None, 0.0, -1.0, float("nan"), float("inf"), float("-inf"), True],
)
def test_bleclient_management_rejects_invalid_await_timeout(
    ble_client: BLEClient,
    method_name: str,
    invalid_timeout: object,
) -> None:
    """pair()/unpair() should require a finite positive await timeout."""

    class _Backend:
        async def pair(self, **_kwargs: object) -> None:
            return None

        async def unpair(self) -> None:
            return None

    ble_client.bleak_client = cast(Any, _Backend())

    with pytest.raises(
        BLEClient.BLEError,
        match=re.escape(ERROR_MANAGEMENT_AWAIT_TIMEOUT_INVALID),
    ):
        if method_name == "pair":
            ble_client.pair(confirm=True, await_timeout=cast(Any, invalid_timeout))
        else:
            ble_client.unpair(await_timeout=cast(Any, invalid_timeout))
