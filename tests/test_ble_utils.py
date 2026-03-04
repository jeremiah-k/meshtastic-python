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
    assert asyncio.run(withTimeout(_done(), timeout=1.0, label="alias")) == "ok"


@pytest.mark.unit
def test_resolve_ble_module_returns_module(monkeypatch: pytest.MonkeyPatch) -> None:
    """resolve_ble_module should return the value from importlib.import_module."""
    sentinel = ModuleType("sentinel_ble_module")
    monkeypatch.setattr(ble_utils.importlib, "import_module", lambda _name: sentinel)

    assert resolve_ble_module() is sentinel
    assert resolveBleModule() is sentinel
