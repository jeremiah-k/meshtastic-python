"""Tests for meshtastic.interfaces.ble package initialization.

Tests the import guard and error handling when bleak is not available.
"""

from __future__ import annotations

import builtins
import importlib
import sys
from types import ModuleType
from unittest.mock import patch

import pytest

# pylint: disable=import-outside-toplevel

_BLE_IMPORT_TEST_MODULE_PREFIXES = (
    "bleak",
    "meshtastic.ble_interface",
    "meshtastic.interfaces",
    "meshtastic.interfaces.ble",
)


def _module_matches_prefix(name: str, prefixes: tuple[str, ...]) -> bool:
    """Return True when a module name is in or under one of the prefixes."""
    return any(name == prefix or name.startswith(f"{prefix}.") for prefix in prefixes)


def _snapshot_modules(prefixes: tuple[str, ...]) -> dict[str, ModuleType]:
    """Capture module objects for the provided prefixes."""
    return {
        name: module
        for name, module in sys.modules.items()
        if _module_matches_prefix(name, prefixes)
    }


def _restore_modules(snapshot: dict[str, ModuleType], prefixes: tuple[str, ...]) -> None:
    """Restore captured modules and remove any modules created during the test."""
    for name in list(sys.modules.keys()):
        if _module_matches_prefix(name, prefixes):
            del sys.modules[name]
    sys.modules.update(snapshot)


@pytest.mark.unit
class TestBLEPackageInit:
    """Test BLE package initialization and import guard."""

    def test_ble_init_imports_successfully(self) -> None:
        """Test that ble package imports successfully when bleak is available."""
        from meshtastic.interfaces import ble

        assert ble is not None
        assert hasattr(ble, "BLEInterface")
        assert hasattr(ble, "BLEClient")
        assert hasattr(ble, "SERVICE_UUID")

    def test_ble_init_raises_import_error_when_bleak_missing(self) -> None:
        """Test that importing ble package raises helpful ImportError when bleak is missing.

        Covers lines 14-17 of meshtastic/interfaces/ble/__init__.py:
        the ModuleNotFoundError handler that provides a helpful error message.
        """
        module_snapshot = _snapshot_modules(_BLE_IMPORT_TEST_MODULE_PREFIXES)
        original_import = builtins.__import__

        try:
            if "bleak" in sys.modules:
                del sys.modules["bleak"]
            if "meshtastic.interfaces.ble" in sys.modules:
                del sys.modules["meshtastic.interfaces.ble"]
            for key in list(sys.modules.keys()):
                if key.startswith("meshtastic.interfaces.ble"):
                    del sys.modules[key]

            def raise_bleak_not_found(
                name: str,
                _globals: dict[str, object] | None = None,
                _locals: dict[str, object] | None = None,
                fromlist: tuple[str, ...] | None = None,
                level: int = 0,
            ) -> ModuleType:
                if name == "bleak":
                    raise ModuleNotFoundError("No module named 'bleak'", name="bleak")
                return original_import(name, _globals, _locals, fromlist, level)

            with patch("builtins.__import__", side_effect=raise_bleak_not_found):
                with pytest.raises(ImportError) as exc_info:
                    importlib.import_module("meshtastic.interfaces.ble")

            assert "bleak" in str(exc_info.value).lower()
            assert "poetry install" in str(exc_info.value).lower()

        finally:
            _restore_modules(module_snapshot, _BLE_IMPORT_TEST_MODULE_PREFIXES)

    def test_ble_init_reraises_non_bleak_module_not_found(self) -> None:
        """Test that ModuleNotFoundError for non-bleak modules is re-raised.

        Covers line 15-16: the check that only catches ModuleNotFoundError
        when the missing module is specifically 'bleak'.
        """
        module_snapshot = _snapshot_modules(_BLE_IMPORT_TEST_MODULE_PREFIXES)
        original_import = builtins.__import__

        try:
            if "meshtastic.interfaces.ble" in sys.modules:
                del sys.modules["meshtastic.interfaces.ble"]
            for key in list(sys.modules.keys()):
                if key.startswith("meshtastic.interfaces.ble"):
                    del sys.modules[key]

            def raise_other_not_found(
                name: str,
                _globals: dict[str, object] | None = None,
                _locals: dict[str, object] | None = None,
                fromlist: tuple[str, ...] | None = None,
                level: int = 0,
            ) -> ModuleType:
                if name == "bleak":
                    raise ModuleNotFoundError(
                        "No module named 'some_other_module'", name="some_other_module"
                    )
                return original_import(name, _globals, _locals, fromlist, level)

            with patch("builtins.__import__", side_effect=raise_other_not_found):
                with pytest.raises(ModuleNotFoundError) as exc_info:
                    importlib.import_module("meshtastic.interfaces.ble")

                assert exc_info.value.name == "some_other_module"

        finally:
            _restore_modules(module_snapshot, _BLE_IMPORT_TEST_MODULE_PREFIXES)

    def test_ble_all_exports(self) -> None:
        """Test that __all__ exports are accessible."""
        from meshtastic.interfaces import ble

        for name in ble.__all__:
            assert hasattr(ble, name), f"Missing export: {name}"


@pytest.mark.unit
class TestBLEPackageConstants:
    """Test BLE package exports are accessible."""

    def test_uuid_constants_exported(self) -> None:
        """Test that UUID constants are exported."""
        from meshtastic.interfaces.ble import (
            FROMNUM_UUID,
            FROMRADIO_UUID,
            LEGACY_LOGRADIO_UUID,
            LOGRADIO_UUID,
            SERVICE_UUID,
            TORADIO_UUID,
        )

        assert SERVICE_UUID is not None
        assert TORADIO_UUID is not None
        assert FROMRADIO_UUID is not None
        assert FROMNUM_UUID is not None
        assert LOGRADIO_UUID is not None
        assert LEGACY_LOGRADIO_UUID is not None

    def test_error_constants_exported(self) -> None:
        """Test that error message constants are exported."""
        from meshtastic.interfaces.ble import (
            BLECLIENT_ERROR_ASYNC_TIMEOUT,
            ERROR_CONNECTION_FAILED,
            ERROR_MULTIPLE_DEVICES,
            ERROR_NO_PERIPHERAL_FOUND,
            ERROR_NO_PERIPHERALS_FOUND,
            ERROR_READING_BLE,
            ERROR_TIMEOUT,
            ERROR_WRITING_BLE,
        )

        assert ERROR_TIMEOUT is not None
        assert ERROR_MULTIPLE_DEVICES is not None
        assert ERROR_READING_BLE is not None
        assert ERROR_NO_PERIPHERAL_FOUND is not None
        assert ERROR_WRITING_BLE is not None
        assert ERROR_CONNECTION_FAILED is not None
        assert ERROR_NO_PERIPHERALS_FOUND is not None
        assert BLECLIENT_ERROR_ASYNC_TIMEOUT is not None

    def test_classes_exported(self) -> None:
        """Test that main classes are exported."""
        from meshtastic.interfaces.ble import BLEClient, BLEConfig, BLEInterface

        assert BLEInterface is not None
        assert BLEClient is not None
        assert BLEConfig is not None

    def test_logger_exported(self) -> None:
        """Test that logger is exported for backward compatibility."""
        from meshtastic.interfaces.ble import logger

        assert logger is not None
