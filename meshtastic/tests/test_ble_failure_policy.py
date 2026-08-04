"""Behavioral tests for explicit BLE failure dispositions."""

from __future__ import annotations

import logging

import pytest

from meshtastic.interfaces.ble.constants import logger
from meshtastic.interfaces.ble.failure_policy import (
    _BLEFailureDisposition,
    _log_ble_failure,
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
