"""Unit tests for BLE reconnect policy naming and behavior."""

import pytest

from meshtastic.interfaces.ble.policies import ReconnectPolicy


@pytest.mark.unit
def test_reconnect_policy_snake_case_methods_work() -> None:
    """ReconnectPolicy snake_case accessors should expose attempt progression."""
    policy = ReconnectPolicy(
        initial_delay=1.0,
        max_delay=10.0,
        backoff=2.0,
        jitter_ratio=0.0,
        max_retries=3,
    )

    delay, should_retry = policy.next_attempt()
    assert delay == pytest.approx(1.0)
    assert should_retry is True
    assert policy.get_attempt_count() == 1

    delay, should_retry = policy.next_attempt()
    assert delay == pytest.approx(2.0)
    assert should_retry is True
    assert policy.get_attempt_count() == 2

    delay, should_retry = policy.next_attempt()
    assert delay == pytest.approx(4.0)
    assert should_retry is False
    assert policy.get_attempt_count() == 3
