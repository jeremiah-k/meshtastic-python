"""Unit tests for BLE reconnect policy naming and behavior."""

import pytest

from meshtastic.interfaces.ble.policies import ReconnectPolicy


@pytest.mark.unit
def test_reconnect_policy_camelcase_methods_work() -> None:
    """ReconnectPolicy camelCase accessors should expose attempt progression."""
    policy = ReconnectPolicy(
        initial_delay=1.0,
        max_delay=10.0,
        backoff=2.0,
        jitter_ratio=0.0,
        max_retries=2,
    )

    delay, should_retry = policy.nextAttempt()
    assert delay == pytest.approx(1.0)
    assert should_retry is True
    assert policy.getAttemptCount() == 1


@pytest.mark.unit
def test_reconnect_policy_snake_case_methods_delegate() -> None:
    """ReconnectPolicy snake_case methods should delegate to camelCase."""
    policy = ReconnectPolicy(
        initial_delay=1.0,
        max_delay=10.0,
        backoff=2.0,
        jitter_ratio=0.0,
        max_retries=2,
    )

    delay, should_retry = policy.next_attempt()
    assert delay == pytest.approx(1.0)
    assert should_retry is True

    attempts = policy.get_attempt_count()
    assert attempts == 1
