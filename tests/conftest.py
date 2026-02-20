"""Shared pytest fixtures for BLE tests."""

import logging
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

logger = logging.getLogger(__name__)


def _clear_all_registries() -> None:
    """
    Clear BLE gating global registries.

    Acquires the module registry lock and clears the following registries:
    `_ADDR_LOCKS`, `_CONNECTED_ADDRS`, `_CONNECTED_MARKED_AT`, `_CONNECTED_OWNER_IDS`,
    `_CONNECTED_OWNERS`, and `_LOCK_HOLDERS`.
    """
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


@pytest.fixture(scope="session", autouse=True)
def _stop_ble_runner_at_session_end() -> Iterator[None]:
    """
    Ensure BLECoroutineRunner is stopped at the end of the test session.

    This prevents the runner's background thread from hanging during pytest exit.
    """
    yield
    # Stop the BLECoroutineRunner singleton at session end
    try:
        from meshtastic.interfaces.ble.runner import BLECoroutineRunner

        runner = BLECoroutineRunner()
        runner.stop(timeout=2.0)
    except Exception as exc:
        logger.debug(
            "Failed to stop BLECoroutineRunner during test cleanup: %s",
            exc,
        )
