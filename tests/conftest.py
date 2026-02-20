"""Shared pytest fixtures for BLE tests."""

import logging
from typing import Iterator

import pytest

from meshtastic.interfaces.ble import gating

logger = logging.getLogger(__name__)


def _clear_all_registries() -> None:
    """
    Clear BLE gating global registries.

    Delegates to the public gating reset helper.
    """
    gating.clear_all_registries()


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
    except ImportError:
        # BLE extras may not be installed in this test environment.
        return

    try:
        runner = BLECoroutineRunner()
        runner.stop(timeout=2.0)
    except Exception as exc:
        logger.debug(
            "Failed to stop BLECoroutineRunner during test cleanup: %s",
            exc,
        )
