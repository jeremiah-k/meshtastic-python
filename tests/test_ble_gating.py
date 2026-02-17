"""Tests for BLE gating utilities."""

import gc

import pytest

from meshtastic.interfaces.ble.constants import BLEConfig
from meshtastic.interfaces.ble.gating import (
    _ADDR_LOCKS,
    _CONNECTED_ADDRS,
    _CONNECTED_MARKED_AT,
    _LOCK_HOLDERS,
    _REGISTRY_LOCK,
    _addr_key,
    _addr_lock_context,
    _cleanup_addr_lock,
    _get_addr_lock,
    _is_currently_connected_elsewhere,
    _mark_connected,
    _mark_disconnected,
    _release_addr_lock,
)


class TestAddrKey:
    """Test cases for _addr_key function."""

    def test_valid_address(self):
        """Test that valid addresses are normalized correctly."""
        assert _addr_key("AA:BB:CC:DD:EE:FF") == "aabbccddeeff"
        assert _addr_key("aa-bb-cc-dd-ee-ff") == "aabbccddeeff"
        assert _addr_key("AA_BB_CC_DD_EE_FF") == "aabbccddeeff"
        assert _addr_key("aa bb cc dd ee ff") == "aabbccddeeff"

    def test_none_address(self):
        """Test that None address returns None."""
        assert _addr_key(None) is None

    def test_empty_address(self):
        """Test that empty address returns None."""
        assert _addr_key("") is None

    def test_whitespace_address(self):
        """Test that whitespace-only address returns None."""
        assert _addr_key("   ") is None
        assert _addr_key("\t\n") is None

    def test_different_empty_inputs_have_different_keys(self):
        """Test that different empty/None inputs all return None (not same key)."""
        # This ensures they don't share the same registry key
        assert _addr_key(None) is None
        assert _addr_key("") is None
        assert _addr_key("   ") is None
        # All return None, which is handled specially by gating functions


@pytest.mark.usefixtures("clear_registry")
class TestAddrLock:
    """Test cases for _get_addr_lock function."""

    def test_get_lock_for_valid_address(self):
        """Test that locks are created for valid addresses."""
        lock1 = _get_addr_lock("aabbccddeeff")
        assert lock1 is not None
        # Check that it's a reentrant lock by attempting to acquire twice
        lock1.acquire()
        try:
            assert lock1.acquire(blocking=False)
        finally:
            lock1.release()
            lock1.release()

    def test_get_lock_for_none_address(self):
        """Test that None address returns registry lock."""
        lock = _get_addr_lock(None)
        assert lock is not None
        assert lock is _REGISTRY_LOCK

    def test_same_address_returns_same_lock(self):
        """Test that the same address returns the same lock."""
        lock1 = _get_addr_lock("aabbccddeeff")
        lock2 = _get_addr_lock("aabbccddeeff")
        assert lock1 is lock2

    def test_different_addresses_return_different_locks(self):
        """Test that different addresses return different locks."""
        lock1 = _get_addr_lock("aabbccddeeff")
        lock2 = _get_addr_lock("112233445566")
        assert lock1 is not lock2

    def test_lock_cleanup_removes_from_registry(self):
        """Test that _cleanup_addr_lock removes the lock from registry when no holders remain."""
        _get_addr_lock("testaddress")
        assert "testaddress" in _ADDR_LOCKS
        # Release the holder count that was incremented by _get_addr_lock
        _release_addr_lock("testaddress")
        # Now cleanup should work since no holders remain
        _cleanup_addr_lock("testaddress")
        assert "testaddress" not in _ADDR_LOCKS

    def test_addr_lock_context_cleans_unconnected_lock(self):
        """Address locks should be removed after context exit when not connected."""
        with _addr_lock_context("temp-address"):
            pass
        assert _addr_key("temp-address") not in _ADDR_LOCKS
        assert _addr_key("temp-address") not in _LOCK_HOLDERS

    def test_addr_lock_context_cleans_lock_on_exception(self):
        """Address-lock holder tracking should unwind correctly when the context exits via exception."""
        key = _addr_key("temp-exception-address")
        assert key is not None

        with pytest.raises(RuntimeError, match="boom"):
            with _addr_lock_context("temp-exception-address"):
                raise RuntimeError("boom")

        assert key not in _ADDR_LOCKS
        assert key not in _LOCK_HOLDERS


@pytest.mark.usefixtures("clear_registry")
class TestMarkConnected:
    """Test cases for _mark_connected function."""

    def test_mark_connected_adds_to_registry(self):
        """Test that marking an address as connected adds it to registry."""
        _mark_connected("aabbccddeeff")
        assert "aabbccddeeff" in _CONNECTED_ADDRS

    def test_mark_connected_with_none_does_nothing(self):
        """Test that marking None as connected does nothing."""
        _mark_connected(None)
        assert len(_CONNECTED_ADDRS) == 0

    def test_mark_connected_with_empty_string_does_nothing(self):
        """Test that marking empty string as connected does nothing (normalizes to None)."""
        # Empty strings are now normalized internally, so they are not added
        _mark_connected("")
        assert len(_CONNECTED_ADDRS) == 0
        # Same behavior as passing None directly
        _mark_connected(None)
        assert len(_CONNECTED_ADDRS) == 0


@pytest.mark.usefixtures("clear_registry")
class TestMarkDisconnected:
    """Test cases for _mark_disconnected function."""

    @pytest.fixture(autouse=True)
    def _mark_default_connected(self, clear_registry):
        """
        Mark a fixed test address as connected before each test.
        
        This autouse fixture ensures the registry is cleared (via the `clear_registry` fixture)
        and then records the address "aabbccddeeff" as connected for the duration of the test.
        
        Parameters:
            clear_registry: pytest fixture that clears gating registries before use.
        """
        _ = clear_registry
        _mark_connected("aabbccddeeff")

    def test_mark_disconnected_removes_from_registry(self):
        """Test that marking an address as disconnected removes it from registry."""
        assert "aabbccddeeff" in _CONNECTED_ADDRS
        _mark_disconnected("aabbccddeeff")
        assert "aabbccddeeff" not in _CONNECTED_ADDRS

    def test_mark_disconnected_with_none_does_nothing(self):
        """Test that marking None as disconnected does nothing."""
        initial_count = len(_CONNECTED_ADDRS)
        _mark_disconnected(None)
        assert len(_CONNECTED_ADDRS) == initial_count

    def test_mark_disconnected_with_empty_does_nothing(self):
        """Test that marking empty string as disconnected does nothing."""
        initial_count = len(_CONNECTED_ADDRS)
        _mark_disconnected("")
        assert len(_CONNECTED_ADDRS) == initial_count

    def test_mark_disconnected_cleanup_lock(self):
        """
        Verify that marking an address disconnected removes its per-address
        lock from the registry.

        The test enters `_addr_lock_context` for `"testaddress"`, marks it
        connected, and asserts the lock remains after the context exits. After
        calling `_mark_disconnected("testaddress")`, the test asserts the lock
        has been removed from `_ADDR_LOCKS`.
        """
        # Use context manager for proper holder count management
        with _addr_lock_context("testaddress") as lock:
            with lock:
                _mark_connected("testaddress")
        assert "testaddress" in _ADDR_LOCKS  # Lock still exists
        _mark_disconnected("testaddress")
        assert "testaddress" not in _ADDR_LOCKS  # Lock cleaned up

    def test_mark_disconnected_ignores_non_owner(self):
        """Disconnect from a different owner should not clear an active claim."""

        class Owner:
            pass

        owner_a = Owner()
        owner_b = Owner()
        key = _addr_key("aabbccddeeff")
        _mark_connected("aabbccddeeff", owner=owner_a)
        _mark_disconnected("aabbccddeeff", owner=owner_b)

        assert key in _CONNECTED_ADDRS


@pytest.mark.usefixtures("clear_registry")
class TestIsCurrentlyConnectedElsewhere:
    """Test cases for _is_currently_connected_elsewhere function."""

    def test_returns_true_for_connected_address(self):
        """Test that it returns True for a connected address."""
        _mark_connected("aabbccddeeff")
        assert _is_currently_connected_elsewhere("aabbccddeeff")

    def test_returns_false_for_non_connected_address(self):
        """Test that it returns False for a non-connected address."""
        assert not _is_currently_connected_elsewhere("aabbccddeeff")

    def test_returns_false_for_none_address(self):
        """Test that it returns False for None address."""
        assert not _is_currently_connected_elsewhere(None)

    def test_returns_false_for_empty_address(self):
        """Test that it returns False for empty address."""
        # Empty string normalizes to None via _addr_key, same as passing None directly.
        assert not _is_currently_connected_elsewhere("")

    def test_returns_false_for_same_owner(self):
        """A claim owned by this interface is not considered connected elsewhere."""

        class Owner:
            is_connection_connected = True

        owner = Owner()
        _mark_connected("aabbccddeeff", owner=owner)

        assert not _is_currently_connected_elsewhere("aabbccddeeff", owner=owner)

    def test_returns_true_for_different_owner(self):
        """A live claim from another owner should be treated as connected elsewhere."""

        class Owner:
            is_connection_connected = True

        owner_a = Owner()
        owner_b = Owner()
        _mark_connected("aabbccddeeff", owner=owner_a)

        assert _is_currently_connected_elsewhere("aabbccddeeff", owner=owner_b)

    def test_prunes_dead_owner_claim(self):
        """Dead weakref owners should be pruned automatically."""

        class Owner:
            is_connection_connected = True

        owner = Owner()
        key = _addr_key("aabbccddeeff")
        _mark_connected("aabbccddeeff", owner=owner)
        del owner
        gc.collect()

        assert not _is_currently_connected_elsewhere("aabbccddeeff")
        assert key not in _CONNECTED_ADDRS

    def test_prunes_owner_claim_when_owner_not_connected(self):
        """Claims from owners no longer connected should be pruned."""

        class Owner:
            is_connection_connected = False

        owner = Owner()
        key = _addr_key("aabbccddeeff")
        _mark_connected("aabbccddeeff", owner=owner)

        assert not _is_currently_connected_elsewhere("aabbccddeeff")
        assert key not in _CONNECTED_ADDRS

    def test_prunes_owner_claim_when_owner_method_reports_disconnected(self):
        """Owner methods returning False should also be treated as stale claims."""

        class Owner:
            def is_connection_connected(self):
                """
                Report whether the associated connection is currently established.
                
                Returns:
                    `True` if the connection is established, `False` otherwise.
                """
                return False

        owner = Owner()
        key = _addr_key("aabbccddeeff")
        _mark_connected("aabbccddeeff", owner=owner)

        assert not _is_currently_connected_elsewhere("aabbccddeeff")
        assert key not in _CONNECTED_ADDRS

    def test_preserves_owner_claim_when_state_probe_raises(self):
        """State-probe exceptions should not aggressively prune active claims."""

        class Owner:
            def is_connection_connected(self):
                """
                Probe whether the owner's connection is active.

                Returns:
                    `True` if the owner's connection is active, `False` otherwise.

                Raises:
                    RuntimeError: If the probe cannot determine connection state (e.g., probe failure).

                """
                raise RuntimeError("probe failed")

        owner = Owner()
        key = _addr_key("aabbccddeeff")
        _mark_connected("aabbccddeeff", owner=owner)

        assert _is_currently_connected_elsewhere("aabbccddeeff", owner=object())
        assert key in _CONNECTED_ADDRS

    def test_prunes_stale_unowned_claim(self, monkeypatch):
        """Unowned claims should expire after a bounded stale window."""
        key = _addr_key("aabbccddeeff")
        assert key is not None
        _mark_connected("aabbccddeeff")
        stale_now = (
            _CONNECTED_MARKED_AT[key]
            + BLEConfig.CONNECTION_GATE_UNOWNED_STALE_SECONDS
            + 1.0
        )
        monkeypatch.setattr(
            "meshtastic.interfaces.ble.gating.time.monotonic",
            lambda: stale_now,
        )

        assert not _is_currently_connected_elsewhere("aabbccddeeff")
        assert key not in _CONNECTED_ADDRS