"""
Tests for ReconnectPolicy and RetryPolicy classes.

These tests verify the centralized retry logic and backoff behavior
that was introduced to address async/thread mixing issues.
"""

import pytest  # pylint: disable=E0401


# Load policies only after bleak/pubsub/serial/tabulate are mocked by autouse fixtures.
@pytest.fixture(autouse=True)  # pylint: disable=R0917
def _load_policies_after_mocks(
    mock_bleak,  # pylint: disable=W0613
    mock_bleak_exc,  # pylint: disable=W0613
    mock_publishing_thread,  # pylint: disable=W0613
    mock_pubsub,  # pylint: disable=W0613
    mock_serial,  # pylint: disable=W0613
    mock_tabulate,  # pylint: disable=W0613
):
    _ = (mock_bleak, mock_bleak_exc, mock_publishing_thread, mock_pubsub, mock_serial, mock_tabulate)
    """
    Ensure meshtastic.ble_interface is imported after specified test mocks and bind its ReconnectPolicy and RetryPolicy into the module globals.

    Each parameter is unused by the function body and exists to ensure the corresponding test-side fixture/mocks are applied before importing the module.

    Parameters
    ----------
        mock_serial: Fixture that mocks serial interactions; included to control import ordering.
        mock_pubsub: Fixture that mocks pub/sub functionality; included to control import ordering.
        mock_tabulate: Fixture that mocks tabulate usage; included to control import ordering.
        mock_bleak: Fixture that mocks the bleak BLE library; included to control import ordering.
        mock_bleak_exc: Fixture that mocks bleak exceptions; included to control import ordering.
        mock_publishing_thread: Fixture that mocks the publishing thread; included to control import ordering.

    """
    global ReconnectPolicy, RetryPolicy  # pylint: disable=W0601
    import importlib

    ble_mod = importlib.import_module("meshtastic.ble_interface")
    ReconnectPolicy = ble_mod.ReconnectPolicy
    RetryPolicy = ble_mod.RetryPolicy


class TestReconnectPolicy:
    """Test the ReconnectPolicy class functionality."""

    def test_initialization(self):
        """Test ReconnectPolicy initialization with default values."""
        policy = ReconnectPolicy()

        assert policy.initial_delay == 1.0
        assert policy.max_delay == 30.0
        assert policy.backoff == 2.0
        assert policy.jitter_ratio == 0.1  # Default is 0.1
        assert policy.max_retries is None
        assert policy.get_attempt_count() == 0

    def test_initialization_with_custom_values(self):
        """Test ReconnectPolicy initialization with custom values."""
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

    def test_reset(self):
        """Test attempt counter reset functionality."""
        policy = ReconnectPolicy(max_retries=3)

        # Simulate some attempts
        policy.next_attempt()
        policy.next_attempt()
        assert policy.get_attempt_count() == 2

        # Reset and verify
        policy.reset()
        assert policy.get_attempt_count() == 0

    def test_delay_calculation_without_jitter(self):
        """Test delay calculation without jitter."""
        policy = ReconnectPolicy(
            initial_delay=1.0,
            max_delay=100.0,
            backoff=2.0,
            jitter_ratio=0.0,  # No jitter
            max_retries=5,
        )

        # Test exponential backoff
        assert policy.get_delay(0) == 1.0  # 1.0 * 2^0
        assert policy.get_delay(1) == 2.0  # 1.0 * 2^1
        assert policy.get_delay(2) == 4.0  # 1.0 * 2^2
        assert policy.get_delay(3) == 8.0  # 1.0 * 2^3

    def test_delay_calculation_with_max_delay(self):
        """Test delay calculation respects max_delay limit."""
        policy = ReconnectPolicy(
            initial_delay=1.0,
            max_delay=5.0,
            backoff=2.0,
            jitter_ratio=0.0,
            max_retries=5,
        )

        # Should be capped at max_delay
        assert policy.get_delay(0) == 1.0
        assert policy.get_delay(1) == 2.0
        assert policy.get_delay(2) == 4.0
        assert policy.get_delay(3) == 5.0  # Capped at max_delay
        assert policy.get_delay(4) == 5.0  # Still capped

    def test_delay_calculation_with_jitter(self):
        """Test delay calculation includes jitter."""
        import random  # pylint: disable=C0415

        rnd = random.Random(12345)  # noqa: S311 - deterministic jitter for tests
        policy = ReconnectPolicy(
            initial_delay=10.0,
            max_delay=100.0,
            backoff=1.1,  # Valid backoff > 1.0
            jitter_ratio=0.5,  # 50% jitter
            max_retries=5,
            random_source=rnd,
        )
        # With jitter, delay should be within expected range
        delay = policy.get_delay(0)
        assert 5.0 <= delay <= 15.0  # 10.0 ± 50%

    def test_should_retry_with_limit(self):
        """Test should_retry logic with retry limit."""
        policy = ReconnectPolicy(max_retries=3)

        assert policy.should_retry(0)
        assert policy.should_retry(1)
        assert policy.should_retry(2)
        assert not policy.should_retry(3)  # At limit
        assert not policy.should_retry(4)

    def test_should_retry_unlimited(self):
        """Test should_retry logic with unlimited retries."""
        policy = ReconnectPolicy(max_retries=None)

        # Should always retry with unlimited retries
        assert policy.should_retry(0)
        assert policy.should_retry(10)
        assert policy.should_retry(100)

    def test_next_attempt(self):
        """Test next_attempt method returns delay and increments counter."""
        policy = ReconnectPolicy(
            initial_delay=1.0,
            max_delay=10.0,
            backoff=2.0,
            jitter_ratio=0.0,
            max_retries=3,
        )

        # First attempt
        delay, should_retry = policy.next_attempt()
        assert delay == 1.0
        assert should_retry
        assert policy.get_attempt_count() == 1

        # Second attempt
        delay, should_retry = policy.next_attempt()
        assert delay == 2.0
        assert should_retry
        assert policy.get_attempt_count() == 2

        # Third attempt
        delay, should_retry = policy.next_attempt()
        assert delay == 4.0
        assert should_retry
        assert policy.get_attempt_count() == 3

        # Fourth attempt (should not retry)
        delay, should_retry = policy.next_attempt()
        assert delay == 8.0
        assert not should_retry
        assert policy.get_attempt_count() == 4

    def test_get_delay_with_none_uses_internal_counter(self):
        """Test get_delay with None uses internal attempt counter."""
        policy = ReconnectPolicy(
            initial_delay=1.0,
            max_delay=10.0,
            backoff=2.0,
            jitter_ratio=0.0,
            max_retries=3,
        )

        # Increment counter a few times
        policy.next_attempt()
        policy.next_attempt()

        # get_delay(None) should use current counter value
        delay = policy.get_delay(None)
        assert delay == 4.0  # 1.0 * 2^2


class TestRetryPolicy:
    """Test the RetryPolicy class and its predefined policies."""

    def test_empty_read_policy(self):
        """Test EMPTY_READ retry policy configuration."""
        policy = RetryPolicy.EMPTY_READ

        assert policy.initial_delay == 0.1  # EMPTY_READ_RETRY_DELAY
        assert policy.max_delay == 1.0
        assert policy.backoff == 1.5
        assert policy.jitter_ratio == 0.1
        assert policy.max_retries == 5  # EMPTY_READ_MAX_RETRIES

    def test_transient_error_policy(self):
        """Test TRANSIENT_ERROR retry policy configuration."""
        policy = RetryPolicy.TRANSIENT_ERROR

        assert policy.initial_delay == 0.1  # TRANSIENT_READ_RETRY_DELAY
        assert policy.max_delay == 2.0
        assert policy.backoff == 1.5
        assert policy.jitter_ratio == 0.1
        assert policy.max_retries == 3  # TRANSIENT_READ_MAX_RETRIES

    def test_auto_reconnect_policy(self):
        """Test AUTO_RECONNECT retry policy configuration."""
        policy = RetryPolicy.AUTO_RECONNECT

        assert policy.initial_delay == 1.0  # AUTO_RECONNECT_INITIAL_DELAY
        assert policy.max_delay == 30.0  # AUTO_RECONNECT_MAX_DELAY
        assert policy.backoff == 2.0  # AUTO_RECONNECT_BACKOFF
        assert policy.jitter_ratio == 0.15  # AUTO_RECONNECT_JITTER_RATIO
        assert policy.max_retries is None  # Unlimited retries

    def test_policy_independence(self):
        """Test that different policy instances are independent."""
        empty_policy = RetryPolicy.EMPTY_READ
        transient_policy = RetryPolicy.TRANSIENT_ERROR

        # Reset one policy shouldn't affect the other
        empty_policy.reset()
        assert empty_policy.get_attempt_count() == 0
        assert transient_policy.get_attempt_count() == 0

        # Use one policy shouldn't affect the other
        empty_policy.next_attempt()
        assert empty_policy.get_attempt_count() == 1
        assert transient_policy.get_attempt_count() == 0
        # Cleanup to avoid state bleed into other tests
        empty_policy.reset()
        transient_policy.reset()
