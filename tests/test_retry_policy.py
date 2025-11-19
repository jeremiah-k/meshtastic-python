"""Tests for the retry/backoff policies used by the BLE interface."""

import random

from meshtastic.ble_interface import BLEConfig, ReconnectPolicy, RetryPolicy


class TestReconnectPolicy:
    """Unit tests for the ReconnectPolicy helper."""

    def test_initialization_defaults(self):
        policy = ReconnectPolicy()

        assert policy.initial_delay == 1.0
        assert policy.max_delay == 30.0
        assert policy.backoff == 2.0
        assert policy.jitter_ratio == 0.1
        assert policy.max_retries is None
        assert policy.get_attempt_count() == 0

    def test_initialization_custom_values(self):
        policy = ReconnectPolicy(
            initial_delay=0.5,
            max_delay=10.0,
            backoff=1.5,
            jitter_ratio=0.2,
            max_retries=5,
        )

        assert policy.initial_delay == 0.5
        assert policy.max_delay == 10.0
        assert policy.backoff == 1.5
        assert policy.jitter_ratio == 0.2
        assert policy.max_retries == 5

    def test_reset_and_get_attempt_count(self):
        policy = ReconnectPolicy(max_retries=3)
        policy.next_attempt()
        policy.next_attempt()
        assert policy.get_attempt_count() == 2
        policy.reset()
        assert policy.get_attempt_count() == 0

    def test_delay_calculation_without_jitter(self):
        policy = ReconnectPolicy(
            initial_delay=1.0,
            max_delay=100.0,
            backoff=2.0,
            jitter_ratio=0.0,
            max_retries=5,
        )

        assert policy.get_delay(0) == 1.0
        assert policy.get_delay(1) == 2.0
        assert policy.get_delay(2) == 4.0

    def test_delay_respects_maximum(self):
        policy = ReconnectPolicy(
            initial_delay=1.0,
            max_delay=5.0,
            backoff=3.0,
            jitter_ratio=0.0,
            max_retries=5,
        )

        assert policy.get_delay(0) == 1.0
        assert policy.get_delay(1) == 3.0
        assert policy.get_delay(2) == 5.0  # capped at max_delay

    def test_delay_includes_jitter(self):
        rnd = random.Random(0)
        policy = ReconnectPolicy(
            initial_delay=10.0,
            max_delay=20.0,
            backoff=2.0,
            jitter_ratio=0.5,
            max_retries=1,
            random_source=rnd,
        )

        delay = policy.get_delay(0)
        assert 5.0 <= delay <= 15.0

    def test_should_retry_limit(self):
        policy = ReconnectPolicy(max_retries=2)
        assert policy.should_retry(0) is True
        assert policy.should_retry(1) is True
        assert policy.should_retry(2) is False

    def test_next_attempt_advances_state(self):
        policy = ReconnectPolicy(jitter_ratio=0.0, max_retries=3)

        delay, should_retry = policy.next_attempt()
        assert delay == 1.0
        assert should_retry
        delay, should_retry = policy.next_attempt()
        assert delay == 2.0
        delay, should_retry = policy.next_attempt()
        assert delay == 4.0

    def test_get_delay_without_argument(self):
        policy = ReconnectPolicy(jitter_ratio=0.0, max_retries=3)
        policy.next_attempt()
        policy.next_attempt()
        assert policy.get_delay(None) == 4.0


class TestRetryPolicy:
    """Ensure the shared RetryPolicy presets are wired correctly."""

    def test_empty_read_policy(self):
        policy = RetryPolicy.EMPTY_READ
        assert policy.initial_delay == BLEConfig.EMPTY_READ_RETRY_DELAY
        assert policy.max_retries == BLEConfig.EMPTY_READ_MAX_RETRIES

    def test_transient_error_policy(self):
        policy = RetryPolicy.TRANSIENT_ERROR
        assert policy.initial_delay == BLEConfig.TRANSIENT_READ_RETRY_DELAY
        assert policy.max_retries == BLEConfig.TRANSIENT_READ_MAX_RETRIES

    def test_auto_reconnect_policy(self):
        policy = RetryPolicy.AUTO_RECONNECT
        assert policy.initial_delay == BLEConfig.AUTO_RECONNECT_INITIAL_DELAY
        assert policy.max_delay == BLEConfig.AUTO_RECONNECT_MAX_DELAY
        assert policy.jitter_ratio == BLEConfig.AUTO_RECONNECT_JITTER_RATIO
        assert policy.max_retries is None

    def test_policy_instances_are_independent(self):
        empty = RetryPolicy.EMPTY_READ
        transient = RetryPolicy.TRANSIENT_ERROR
        empty.reset()
        transient.reset()
        empty.next_attempt()
        assert empty.get_attempt_count() == 1
        assert transient.get_attempt_count() == 0
        empty.reset()
