"""Targeted tests for BLE utility wrappers and error branches."""

# pylint: disable=wrong-import-position

import asyncio
from types import ModuleType

import pytest

pytest.importorskip("bleak")
import meshtastic.interfaces.ble.utils as ble_utils
from meshtastic.ble_interface import (
    BLEAddressMismatchError as CompatBLEAddressMismatchError,
)
from meshtastic.ble_interface import (
    BLEConnectionSuppressedError as CompatBLEConnectionSuppressedError,
)
from meshtastic.ble_interface import (
    BLEConnectionTimeoutError as CompatBLEConnectionTimeoutError,
)
from meshtastic.ble_interface import (
    BLEDBusTransportError as CompatBLEDBusTransportError,
)
from meshtastic.ble_interface import (
    BLEDeviceNotFoundError as CompatBLEDeviceNotFoundError,
)
from meshtastic.ble_interface import BLEDiscoveryError as CompatBLEDiscoveryError
from meshtastic.ble_interface import MeshtasticBLEError as CompatMeshtasticBLEError
from meshtastic.ble_interface import sanitize_address as compat_sanitize_address
from meshtastic.interfaces.ble import (
    BLEAddressMismatchError as PublicBLEAddressMismatchError,
)
from meshtastic.interfaces.ble import (
    BLEConnectionSuppressedError as PublicBLEConnectionSuppressedError,
)
from meshtastic.interfaces.ble import (
    BLEConnectionTimeoutError as PublicBLEConnectionTimeoutError,
)
from meshtastic.interfaces.ble import (
    BLEDBusTransportError as PublicBLEDBusTransportError,
)
from meshtastic.interfaces.ble import (
    BLEDeviceNotFoundError as PublicBLEDeviceNotFoundError,
)
from meshtastic.interfaces.ble import BLEDiscoveryError as PublicBLEDiscoveryError
from meshtastic.interfaces.ble import MeshtasticBLEError as PublicMeshtasticBLEError
from meshtastic.interfaces.ble import sanitize_address as public_sanitize_address
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


@pytest.mark.unit
def test_ble_device_not_found_error_identifier_property() -> None:
    """BLEDeviceNotFoundError should expose .identifier for BleakDeviceNotFoundError compat."""
    err = PublicBLEDeviceNotFoundError(
        "not found",
        requested_identifier="AA:BB:CC:DD:EE:FF",
    )
    assert err.identifier == "AA:BB:CC:DD:EE:FF"
    assert err.requested_identifier == "AA:BB:CC:DD:EE:FF"


@pytest.mark.unit
def test_ble_device_not_found_error_catchable_as_bleak_device_not_found() -> None:
    """BLEDeviceNotFoundError should be catchable as BleakDeviceNotFoundError."""
    from bleak.exc import BleakDeviceNotFoundError

    err = PublicBLEDeviceNotFoundError("not found")
    assert isinstance(err, BleakDeviceNotFoundError)


@pytest.mark.unit
def test_ble_connection_timeout_error_is_instance_of_timeout_error() -> None:
    """BLEConnectionTimeoutError should be catchable as TimeoutError."""
    err = PublicBLEConnectionTimeoutError("timed out")
    assert isinstance(err, TimeoutError)


@pytest.mark.unit
def test_ble_errors_are_instance_of_ble_interface_ble_error() -> None:
    """All new BLE exceptions should be catchable as BLEInterface.BLEError."""
    from meshtastic.interfaces.ble import BLEInterface

    assert isinstance(PublicBLEDeviceNotFoundError("x"), BLEInterface.BLEError)
    assert isinstance(PublicBLEConnectionTimeoutError("x"), BLEInterface.BLEError)
    assert isinstance(PublicBLEConnectionSuppressedError("x"), BLEInterface.BLEError)
    assert isinstance(PublicBLEAddressMismatchError("x"), BLEInterface.BLEError)
    assert isinstance(PublicBLEDBusTransportError("x"), BLEInterface.BLEError)
    assert isinstance(PublicBLEDiscoveryError("x"), BLEInterface.BLEError)


@pytest.mark.unit
def test_ble_dbus_transport_error_str_returns_normalized_message() -> None:
    """BLEDBusTransportError.__str__ should return the normalized Meshtastic message."""
    err = PublicBLEDBusTransportError(
        "normalized transport failure",
        dbus_error="org.bluez.Error.Failed",
        dbus_error_details=["raw detail"],
    )
    assert str(err) == "normalized transport failure"
    assert err.dbus_error_name == "org.bluez.Error.Failed"
    assert err.dbus_error_body == (["raw detail"],)
