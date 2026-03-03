"""Targeted tests for BLE utility wrappers and error branches."""

import asyncio
from types import ModuleType

import pytest

try:
    import meshtastic.interfaces.ble.utils as ble_utils
    from meshtastic.interfaces.ble.utils import (
        resolve_ble_module,
        resolveBleModule,
        sanitize_address,
        with_timeout,
        withTimeout,
    )
except ImportError:
    pytest.skip("BLE dependencies not available", allow_module_level=True)


@pytest.mark.unit
def test_withTimeout_reraises_timeout_without_error_factory() -> None:
    """WithTimeout should re-raise asyncio.TimeoutError when no factory is provided."""

    async def _never_finishes() -> None:
        await asyncio.sleep(0.1)

    with pytest.raises(asyncio.TimeoutError):
        asyncio.run(withTimeout(_never_finishes(), timeout=0.0, label="op"))


@pytest.mark.unit
def test_resolveBleModule_reraises_unrelated_import_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ResolveBleModule should re-raise transitive/unrelated ImportErrors."""

    def _raise_transitive(_name: str) -> ModuleType:
        raise ImportError("transitive failure", name="some_unrelated_module")

    monkeypatch.setattr(ble_utils.importlib, "import_module", _raise_transitive)

    with pytest.raises(ImportError, match="transitive failure"):
        resolveBleModule()


@pytest.mark.unit
def test_sanitize_address_alias_delegates(monkeypatch: pytest.MonkeyPatch) -> None:
    """sanitize_address should delegate to sanitizeAddress."""
    monkeypatch.setattr(
        ble_utils,
        "sanitizeAddress",
        lambda address: "aa:bb:cc:dd:ee:ff" if address else None,
    )

    assert sanitize_address("AA-BB-CC-DD-EE-FF") == "aa:bb:cc:dd:ee:ff"


@pytest.mark.unit
def test_with_timeout_alias_delegates() -> None:
    """with_timeout should delegate to withTimeout and return the awaited value."""

    async def _done() -> str:
        return "ok"

    assert asyncio.run(with_timeout(_done(), timeout=1.0, label="alias")) == "ok"


@pytest.mark.unit
def test_resolve_ble_module_alias_delegates(monkeypatch: pytest.MonkeyPatch) -> None:
    """resolve_ble_module should delegate to resolveBleModule."""
    sentinel = ModuleType("sentinel_ble_module")
    monkeypatch.setattr(ble_utils, "resolveBleModule", lambda: sentinel)

    assert resolve_ble_module() is sentinel
