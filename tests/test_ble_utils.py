"""Targeted tests for BLE utility wrappers and error branches."""
# pylint: disable=wrong-import-position

import asyncio
from types import ModuleType

import pytest

pytest.importorskip("bleak")
import meshtastic.interfaces.ble.utils as ble_utils
from meshtastic.interfaces.ble.utils import (
    resolve_ble_module,
    resolveBleModule,
    with_timeout,
    withTimeout,
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
        raise ImportError("transitive failure", name="some_unrelated_module")

    monkeypatch.setattr(ble_utils.importlib, "import_module", _raise_transitive)

    with pytest.raises(ImportError, match="transitive failure"):
        resolve_ble_module()


@pytest.mark.unit
def test_sanitizeAddress_alias_delegates(monkeypatch: pytest.MonkeyPatch) -> None:
    """sanitizeAddress should delegate to sanitize_address."""
    monkeypatch.setattr(
        ble_utils,
        "sanitize_address",
        lambda address: "aa:bb:cc:dd:ee:ff" if address else None,
    )

    assert ble_utils.sanitizeAddress("AA-BB-CC-DD-EE-FF") == "aa:bb:cc:dd:ee:ff"


@pytest.mark.unit
def test_withTimeout_alias_delegates() -> None:
    """withTimeout should delegate to with_timeout and return the awaited value."""

    async def _done() -> str:
        return "ok"

    assert asyncio.run(withTimeout(_done(), timeout=1.0, label="alias")) == "ok"


@pytest.mark.unit
def test_resolveBleModule_alias_delegates(monkeypatch: pytest.MonkeyPatch) -> None:
    """resolveBleModule should delegate to resolve_ble_module."""
    sentinel = ModuleType("sentinel_ble_module")
    monkeypatch.setattr(ble_utils, "resolve_ble_module", lambda: sentinel)

    assert resolveBleModule() is sentinel
