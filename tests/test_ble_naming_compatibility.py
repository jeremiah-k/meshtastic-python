"""Naming compatibility checks for the BLE public surface."""

import asyncio
import warnings
from unittest.mock import MagicMock

import pytest

from meshtastic.interfaces.ble import BLEClient, BLEInterface

pytestmark = pytest.mark.unit


def test_ble_interface_naming_compatibility() -> None:
    """Verify BLEInterface exposes the intended compatibility/public method names."""
    # We don't need to actually connect, just check for attribute existence
    assert hasattr(BLEInterface, "findDevice")
    assert hasattr(BLEInterface, "find_device")
    # Legacy public callbacks from master must remain available
    assert hasattr(BLEInterface, "from_num_handler")
    assert hasattr(BLEInterface, "log_radio_handler")
    assert hasattr(BLEInterface, "legacy_log_radio_handler")


def test_ble_client_naming_compatibility() -> None:
    """Verify BLEClient exposes pre-refactor-compatible names plus selected promotions."""
    # Established public API on master
    assert hasattr(BLEClient, "discover")
    assert hasattr(BLEClient, "pair")
    assert hasattr(BLEClient, "connect")
    assert hasattr(BLEClient, "disconnect")
    assert hasattr(BLEClient, "read_gatt_char")
    assert hasattr(BLEClient, "write_gatt_char")
    assert hasattr(BLEClient, "has_characteristic")
    assert hasattr(BLEClient, "start_notify")
    assert hasattr(BLEClient, "close")
    # Legacy master methods must remain callable for backward compatibility
    assert hasattr(BLEClient, "async_await")
    assert hasattr(BLEClient, "async_run")
    # Explicit low-risk promotions
    assert hasattr(BLEClient, "isConnected")
    assert hasattr(BLEClient, "stopNotify")
    assert hasattr(BLEClient, "is_connected")
    assert hasattr(BLEClient, "stop_notify")


def test_ble_module_level_naming_compatibility() -> None:
    """Verify internal gating helpers are not leaked as interface module aliases."""
    from meshtastic.interfaces.ble import interface

    assert not hasattr(interface, "addr_key")
    assert not hasattr(interface, "is_currently_connected_elsewhere")


def test_reconnect_policy_naming_compatibility() -> None:
    """ReconnectPolicy is internal and uses snake_case canonical names."""
    from meshtastic.interfaces.ble.policies import ReconnectPolicy

    assert hasattr(ReconnectPolicy, "next_attempt")
    assert hasattr(ReconnectPolicy, "get_attempt_count")
    assert not hasattr(ReconnectPolicy, "nextAttempt")
    assert not hasattr(ReconnectPolicy, "getAttemptCount")


def test_ble_client_legacy_async_aliases_delegate() -> None:
    """Legacy BLEClient async aliases should delegate to canonical underscored methods."""
    client = object.__new__(BLEClient)
    async_await_mock = MagicMock(return_value="await-ok")
    async_run_mock = MagicMock(return_value="run-ok")
    client._async_await = async_await_mock  # type: ignore[attr-defined]
    client._async_run = async_run_mock  # type: ignore[attr-defined]

    coro_await = asyncio.sleep(0)
    try:
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            assert client.async_await(coro_await, timeout=1.5) == "await-ok"
        assert not any(issubclass(w.category, DeprecationWarning) for w in caught)
        async_await_mock.assert_called_once_with(coro_await, timeout=1.5)
    finally:
        coro_await.close()

    coro_run = asyncio.sleep(0)
    try:
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            assert client.async_run(coro_run) == "run-ok"
        assert not any(issubclass(w.category, DeprecationWarning) for w in caught)
        async_run_mock.assert_called_once_with(coro_run)
    finally:
        coro_run.close()


def test_ble_interface_legacy_handler_aliases_delegate() -> None:
    """Legacy BLEInterface handler aliases should delegate to canonical underscored handlers."""
    iface = object.__new__(BLEInterface)
    sender = object()
    payload = b"\x01\x02\x03\x04"

    from_num_mock = MagicMock()
    log_mock = MagicMock()
    legacy_log_mock = MagicMock()

    iface._from_num_handler = from_num_mock  # type: ignore[attr-defined]
    iface._log_radio_handler = log_mock  # type: ignore[attr-defined]
    iface._legacy_log_radio_handler = legacy_log_mock  # type: ignore[attr-defined]

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        iface.from_num_handler(sender, payload)
    assert not any(issubclass(w.category, DeprecationWarning) for w in caught)
    from_num_mock.assert_called_once_with(sender, payload)

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        asyncio.run(iface.log_radio_handler(sender, payload))
    assert not any(issubclass(w.category, DeprecationWarning) for w in caught)
    log_mock.assert_called_once_with(sender, payload)

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        asyncio.run(iface.legacy_log_radio_handler(sender, payload))
    assert not any(issubclass(w.category, DeprecationWarning) for w in caught)
    legacy_log_mock.assert_called_once_with(sender, payload)


def test_ble_find_device_shim_is_silent_and_delegates() -> None:
    """find_device should remain callable, delegate to findDevice, and stay silent."""
    iface = object.__new__(BLEInterface)
    delegate = MagicMock(return_value=object())
    iface.findDevice = delegate  # type: ignore[attr-defined]

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        result = iface.find_device("abc")

    delegate.assert_called_once_with("abc")
    assert result is delegate.return_value
    assert not any(issubclass(w.category, DeprecationWarning) for w in caught)
