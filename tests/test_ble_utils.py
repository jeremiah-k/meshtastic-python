"""Targeted tests for BLE utility wrappers and error branches."""

# pylint: disable=wrong-import-position

import asyncio
from types import ModuleType

import pytest

pytest.importorskip("bleak")
import meshtastic.interfaces.ble.utils as ble_utils
from meshtastic.ble_interface import (
    BLEAddressMismatchError as CompatBLEAddressMismatchError,
    BLEConnectionSuppressedError as CompatBLEConnectionSuppressedError,
    BLEConnectionTimeoutError as CompatBLEConnectionTimeoutError,
    BLEDBusTransportError as CompatBLEDBusTransportError,
    BLEDeviceNotFoundError as CompatBLEDeviceNotFoundError,
    BLEDiscoveryError as CompatBLEDiscoveryError,
    MeshtasticBLEError as CompatMeshtasticBLEError,
    sanitize_address as compat_sanitize_address,
)
from meshtastic.interfaces.ble import (
    BLEAddressMismatchError as PublicBLEAddressMismatchError,
    BLEConnectionSuppressedError as PublicBLEConnectionSuppressedError,
    BLEConnectionTimeoutError as PublicBLEConnectionTimeoutError,
    BLEDBusTransportError as PublicBLEDBusTransportError,
    BLEDeviceNotFoundError as PublicBLEDeviceNotFoundError,
    BLEDiscoveryError as PublicBLEDiscoveryError,
    MeshtasticBLEError as PublicMeshtasticBLEError,
    sanitize_address as public_sanitize_address,
)
from meshtastic.interfaces.ble.utils import (
    resolve_ble_module,
    with_timeout,
)


@pytest.mark.unit
def test_with_timeout_reraises_timeout_without_error_factory() -> None:
    """with_timeout should re-raise asyncio.TimeoutError when no factory is provided."""

    async def _never_finishes() -> None:
        await asyncio.sleep(0.1)

    with pytest.raises(asyncio.TimeoutError):
        asyncio.run(with_timeout(_never_finishes(), timeout=0.0, label="op"))


@pytest.mark.unit
def test_resolve_ble_module_reraises_unrelated_import_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """resolve_ble_module should re-raise transitive/unrelated ImportErrors."""

    def _raise_transitive(_name: str) -> ModuleType:
        raise ImportError(  # noqa: TRY003 - intentional synthetic error for branch coverage
            "transitive failure",
            name="some_unrelated_module",
        )

    monkeypatch.setattr(ble_utils.importlib, "import_module", _raise_transitive)

    with pytest.raises(ImportError, match="transitive failure"):
        resolve_ble_module()


@pytest.mark.unit
def test_with_timeout_returns_result() -> None:
    """with_timeout should return the awaited value."""

    async def _done() -> str:
        return "ok"

    assert asyncio.run(with_timeout(_done(), timeout=1.0, label="alias")) == "ok"


@pytest.mark.unit
def test_resolve_ble_module_returns_module(monkeypatch: pytest.MonkeyPatch) -> None:
    """resolve_ble_module should return the value from importlib.import_module."""
    sentinel = ModuleType("sentinel_ble_module")
    monkeypatch.setattr(ble_utils.importlib, "import_module", lambda _name: sentinel)

    assert resolve_ble_module() is sentinel


@pytest.mark.unit
def test_sanitize_address_exported_from_ble_public_api() -> None:
    """sanitize_address should be publicly exported from meshtastic.interfaces.ble."""
    assert public_sanitize_address("AA-BB:CC_DD EE FF") == "aabbccddeeff"


@pytest.mark.unit
def test_sanitize_address_exported_from_ble_compat_surface() -> None:
    """sanitize_address should be available through meshtastic.ble_interface."""
    assert compat_sanitize_address("AA:BB:CC:DD:EE:FF") == "aabbccddeeff"


@pytest.mark.unit
def test_typed_ble_exceptions_exported_from_public_and_compat_surfaces() -> None:
    """Typed BLE exceptions should be importable from public and compat BLE modules."""
    assert PublicMeshtasticBLEError is CompatMeshtasticBLEError
    assert PublicBLEDiscoveryError is CompatBLEDiscoveryError
    assert PublicBLEDeviceNotFoundError is CompatBLEDeviceNotFoundError
    assert PublicBLEConnectionSuppressedError is CompatBLEConnectionSuppressedError
    assert PublicBLEConnectionTimeoutError is CompatBLEConnectionTimeoutError
    assert PublicBLEAddressMismatchError is CompatBLEAddressMismatchError
    assert PublicBLEDBusTransportError is CompatBLEDBusTransportError
