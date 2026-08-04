"""Behavioral tests for explicit BLE failure dispositions."""

from __future__ import annotations

import logging

import pytest

from meshtastic.interfaces.ble.constants import logger
from meshtastic.interfaces.ble.failure_policy import (
    _BLEFailureDisposition,
    _log_ble_failure,
)
from meshtastic.interfaces.ble.lifecycle_ownership_runtime import (
    BLEConnectionOwnershipLifecycleCoordinator,
)

pytestmark = pytest.mark.unit


def test_failure_policy_preserves_log_text_and_attaches_disposition(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Failure classification should add metadata without rewriting messages."""
    with caplog.at_level(logging.WARNING, logger=logger.name):
        try:
            raise RuntimeError("boom")
        except RuntimeError:
            _log_ble_failure(
                _BLEFailureDisposition.RETRYABLE,
                "retry %s",
                "operation",
                level=logging.WARNING,
            )

    record = caplog.records[-1]
    assert record.getMessage() == "retry operation"
    assert record.ble_failure_disposition == "retryable"  # type: ignore[attr-defined]
    assert record.exc_info is not None


def test_failure_dispositions_are_stable_diagnostic_values() -> None:
    """Each internal policy should expose a distinct machine-readable value."""
    assert {item.value for item in _BLEFailureDisposition} == {
        "compatibility_fallback",
        "best_effort",
        "retryable",
        "terminal",
    }


def test_ownership_probe_failure_logs_compatibility_fallback(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A failed preferred ownership probe should log before legacy fallback."""

    class _Owner:
        def current_probe(self) -> bool:
            raise RuntimeError("probe failed")

        legacy_probe = True

    with caplog.at_level(logging.DEBUG, logger=logger.name):
        result = BLEConnectionOwnershipLifecycleCoordinator._probe_bool_member(
            _Owner(), "current_probe", "legacy_probe"
        )

    assert result is True
    record = next(
        record
        for record in caplog.records
        if record.getMessage() == "Error probing ownership member current_probe()"
    )
    assert record.ble_failure_disposition == "compatibility_fallback"  # type: ignore[attr-defined]
    assert record.exc_info is not None
