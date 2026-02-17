"""Shared pytest fixtures for BLE tests."""

from typing import Iterator

import pytest

from meshtastic.interfaces.ble.gating import (
    _ADDR_LOCKS,
    _CONNECTED_ADDRS,
    _CONNECTED_MARKED_AT,
    _CONNECTED_OWNER_IDS,
    _CONNECTED_OWNERS,
    _LOCK_HOLDERS,
    _REGISTRY_LOCK,
)


def _clear_all_registries() -> None:
    """Clear all BLE gating global registries."""
    with _REGISTRY_LOCK:
        _ADDR_LOCKS.clear()
        _CONNECTED_ADDRS.clear()
        _CONNECTED_MARKED_AT.clear()
        _CONNECTED_OWNER_IDS.clear()
        _CONNECTED_OWNERS.clear()
        _LOCK_HOLDERS.clear()


@pytest.fixture
def clear_registry() -> Iterator[None]:
    """Reset BLE gating global registries before and after each test."""
    _clear_all_registries()
    yield
    _clear_all_registries()
