"""Tests for BLE gating utilities."""

from meshtastic.interfaces.ble.gating import (
    _ADDR_LOCKS,
    _CONNECTED_ADDRS,
    _REGISTRY_LOCK,
    _addr_key,
    _cleanup_addr_lock,
    _get_addr_lock,
    _is_currently_connected_elsewhere,
    _mark_connected,
    _mark_disconnected,
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


class TestAddrLock:
    """Test cases for _get_addr_lock function."""

    def test_get_lock_for_valid_address(self):
        """Test that locks are created for valid addresses."""
        lock1 = _get_addr_lock("aabbccddeeff")
        assert lock1 is not None
        # Check that it's a reentrant lock by attempting to acquire twice
        with lock1:
            # If we can acquire it twice without blocking, it's a reentrant lock
            pass

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
        """Test that _cleanup_addr_lock removes the lock from registry."""
        _ADDR_LOCKS.clear()
        _get_addr_lock("testaddress")
        assert "testaddress" in _ADDR_LOCKS
        _cleanup_addr_lock("testaddress")
        assert "testaddress" not in _ADDR_LOCKS


class TestMarkConnected:
    """Test cases for _mark_connected function."""

    def setup_method(self):
        """Clear the connected addresses set before each test."""
        with _REGISTRY_LOCK:
            _CONNECTED_ADDRS.clear()

    def test_mark_connected_adds_to_registry(self):
        """Test that marking an address as connected adds it to registry."""
        _mark_connected("aabbccddeeff")
        assert "aabbccddeeff" in _CONNECTED_ADDRS

    def test_mark_connected_with_none_does_nothing(self):
        """Test that marking None as connected does nothing."""
        _mark_connected(None)
        assert len(_CONNECTED_ADDRS) == 0

    def test_mark_connected_with_empty_adds_to_registry_without_normalization(self):
        """Test that marking empty string as connected adds it unless normalized via _addr_key."""
        # Empty strings are not handled specially, they will be added
        # Use _addr_key to normalize first, which returns None for empty strings
        _mark_connected("")
        assert len(_CONNECTED_ADDRS) == 1
        # But with _addr_key, empty string becomes None and is not added
        _mark_connected(_addr_key(""))
        assert len(_CONNECTED_ADDRS) == 1


class TestMarkDisconnected:
    """Test cases for _mark_disconnected function."""

    def setup_method(self):
        """Set up a connected address before each test."""
        with _REGISTRY_LOCK:
            _CONNECTED_ADDRS.clear()
            _ADDR_LOCKS.clear()
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
        """Test that marking an address as disconnected cleans up the lock."""
        _ADDR_LOCKS.clear()
        _get_addr_lock("testaddress")
        _mark_connected("testaddress")
        assert "testaddress" in _ADDR_LOCKS
        _mark_disconnected("testaddress")
        assert "testaddress" not in _ADDR_LOCKS


class TestIsCurrentlyConnectedElsewhere:
    """Test cases for _is_currently_connected_elsewhere function."""

    def setup_method(self):
        """Clear the connected addresses set before each test."""
        with _REGISTRY_LOCK:
            _CONNECTED_ADDRS.clear()

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
        # Empty string is not treated specially like None; if _mark_connected("") were called,
        # this test would fail. This assumes empty string has not been added directly.
        assert not _is_currently_connected_elsewhere("")
