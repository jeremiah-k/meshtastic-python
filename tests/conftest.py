"""Shared pytest fixtures for BLE tests."""

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


@pytest.fixture
def clear_registry() -> None:
    """Reset BLE gating global registries before and after each test."""
    with _REGISTRY_LOCK:
        _ADDR_LOCKS.clear()
        _CONNECTED_ADDRS.clear()
        _CONNECTED_MARKED_AT.clear()
        _CONNECTED_OWNER_IDS.clear()
        _CONNECTED_OWNERS.clear()
        _LOCK_HOLDERS.clear()
    yield
    with _REGISTRY_LOCK:
        _ADDR_LOCKS.clear()
        _CONNECTED_ADDRS.clear()
        _CONNECTED_MARKED_AT.clear()
        _CONNECTED_OWNER_IDS.clear()
        _CONNECTED_OWNERS.clear()
        _LOCK_HOLDERS.clear()
