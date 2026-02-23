"""Shared pytest fixtures for BLE tests."""

import logging
from collections.abc import Iterator

import pytest

from meshtastic.interfaces.ble import gating

logger = logging.getLogger(__name__)


@pytest.fixture
def clear_registry() -> Iterator[None]:
    """Reset BLE gating global registries before and after each test."""
    gating._clear_all_registries()
    yield
    gating._clear_all_registries()


@pytest.fixture(scope="session", autouse=True)
def _stop_ble_runner_at_session_end() -> Iterator[None]:
    """Ensure BLECoroutineRunner is stopped at the end of the test session.

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
        stop_runner = getattr(runner, "stop", None)
        if callable(stop_runner):
            stop_runner(timeout=2.0)
    except Exception as exc:  # noqa: BLE001 - teardown must not raise
        logger.debug(
            "Failed to stop BLECoroutineRunner during test cleanup: %s",
            exc,
        )
