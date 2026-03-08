"""Tests for BLE gating utilities."""

import gc

import pytest
from hypothesis import given
from hypothesis import strategies as st

from meshtastic.interfaces.ble.constants import BLEConfig
from meshtastic.interfaces.ble.gating import (
    _ADDR_LOCKS,
    _CONNECTED_ADDRS,
    _CONNECTED_MARKED_AT,
    _CONNECTING_ADDRS,
    _CONNECTING_MARKED_AT,
    _LOCK_HOLDERS,
    _REGISTRY_LOCK,
    _addr_key,
    _addr_lock_context,
    _cleanup_addr_lock,
    _clear_connecting,
    _get_addr_lock,
    _is_currently_connected_elsewhere,
    _mark_connected,
    _mark_connecting,
    _mark_disconnected,
    _release_addr_lock,
)

pytestmark = pytest.mark.unit


class _ConnectedOwner:
    """Owner stub that reports an active connection."""

    _is_connection_connected = True


class _DisconnectedOwner:
    """Owner stub that reports an inactive connection."""

    _is_connection_connected = False


def _distinct_owner_token(stale_id: int) -> object:
    """Return an owner token whose id does not match a stale reclaimed owner id."""
    keepalive: list[object] = []
    token = object()
    while id(token) == stale_id:
        keepalive.append(token)
        token = object()
    return token


class TestAddrKey:
    """Test cases for _addr_key function."""

    def test_valid_address(self) -> None:
        """Test that valid addresses are normalized correctly."""
        assert _addr_key("AA:BB:CC:DD:EE:FF") == "aabbccddeeff"
        assert _addr_key("aa-bb-cc-dd-ee-ff") == "aabbccddeeff"
        assert _addr_key("AA_BB_CC_DD_EE_FF") == "aabbccddeeff"
        assert _addr_key("aa bb cc dd ee ff") == "aabbccddeeff"

    @pytest.mark.parametrize("input_val", [None, "", "   ", "\t\n"])
    def test_empty_like_inputs_normalize_to_none(self, input_val: str | None) -> None:
        """Test that None/empty/whitespace inputs all normalize to None."""
        assert _addr_key(input_val) is None

    @given(st.from_regex(r"[0-9a-fA-F]{2}([:_\- ][0-9a-fA-F]{2}){5}", fullmatch=True))
    def test_valid_address_formats_normalize_to_lower_hex(self, addr: str) -> None:
        """Any valid MAC-like address should normalize to 12 lowercase hex chars."""
        normalized = _addr_key(addr)
        assert normalized is not None
        assert len(normalized) == 12
        assert normalized == normalized.lower()
        assert all(char in "0123456789abcdef" for char in normalized)


@pytest.mark.usefixtures("clear_registry")
class TestAddrLock:
    """Test cases for _get_addr_lock function."""

    def test_get_lock_for_valid_address(self) -> None:
        """Test that locks are created for valid addresses."""
        addr = "aabbccddeeff"
        lock1 = _get_addr_lock(addr)
        assert lock1 is not None
        # Check that it's a reentrant lock by attempting to acquire twice
        lock1.acquire()
        acquired_twice = False
        try:
            acquired_twice = lock1.acquire(blocking=False)
            assert acquired_twice
        finally:
            if acquired_twice:
                lock1.release()
            lock1.release()
            _release_addr_lock(addr)

    def test_get_lock_for_none_address(self) -> None:
        """Test that None address returns registry lock."""
        lock = _get_addr_lock(None)
        assert lock is not None
        assert lock is _REGISTRY_LOCK
        _cleanup_addr_lock(None)

    def test_same_address_returns_same_lock(self) -> None:
        """Test that the same address returns the same lock."""
        lock1 = _get_addr_lock("aabbccddeeff")
        lock2 = _get_addr_lock("aabbccddeeff")
        assert lock1 is lock2

    def test_different_addresses_return_different_locks(self) -> None:
        """Test that different addresses return different locks."""
        lock1 = _get_addr_lock("aabbccddeeff")
        lock2 = _get_addr_lock("112233445566")
        assert lock1 is not lock2

    def test_lock_cleanup_removes_from_registry(self) -> None:
        """Test that _cleanup_addr_lock removes the lock from registry when no holders remain."""
        address = "aabbccddeeff"
        key = _addr_key(address)
        _get_addr_lock(address)
        assert key in _ADDR_LOCKS
        # Release the holder count that was incremented by _get_addr_lock
        _release_addr_lock(address)
        # Now cleanup should work since no holders remain
        _cleanup_addr_lock(address)
        assert key not in _ADDR_LOCKS

    def test_addr_lock_context_cleans_unconnected_lock(self) -> None:
        """Address locks should be removed after context exit when not connected."""
        with _addr_lock_context("temp-address"):
            pass
        assert _addr_key("temp-address") not in _ADDR_LOCKS
        assert _addr_key("temp-address") not in _LOCK_HOLDERS

    def test_addr_lock_context_cleans_lock_on_exception(self) -> None:
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

    def test_mark_connected_adds_to_registry(self) -> None:
        """Test that marking an address as connected adds it to registry."""
        _mark_connected("aabbccddeeff")
        assert "aabbccddeeff" in _CONNECTED_ADDRS

    def test_mark_connected_with_none_does_nothing(self) -> None:
        """Test that marking None as connected does nothing."""
        _mark_connected(None)
        assert len(_CONNECTED_ADDRS) == 0

    def test_mark_connected_with_empty_string_does_nothing(self) -> None:
        """Test that marking empty string as connected does nothing (normalizes to None)."""
        # Empty strings are now normalized internally, so they are not added
        _mark_connected("")
        assert len(_CONNECTED_ADDRS) == 0

    def test_mark_connected_clears_matching_provisional_claim(self) -> None:
        """Final connected claims should replace provisional connecting claims."""
        key = _addr_key("aabbccddeeff")
        assert key is not None

        owner = _ConnectedOwner()
        _mark_connecting("aabbccddeeff", owner=owner)
        _mark_connected("aabbccddeeff", owner=owner)

        assert key in _CONNECTED_ADDRS
        assert key not in _CONNECTING_ADDRS

    def test_mark_connecting_keeps_address_lock_until_claim_clears(self) -> None:
        """Provisional claims should keep per-address lock entries alive."""
        key = _addr_key("aabbccddeeff")
        assert key is not None

        _mark_connecting("aabbccddeeff")
        _cleanup_addr_lock(key)
        assert key in _ADDR_LOCKS

        _mark_disconnected("aabbccddeeff")
        assert key not in _ADDR_LOCKS

    def test_mark_connecting_with_none_address_is_no_op(self) -> None:
        """Marking connecting state with None address should not create registry claims."""
        _mark_connecting(None, owner=_ConnectedOwner())
        assert len(_CONNECTING_ADDRS) == 0


class TestMarkDisconnected:
    """Test cases for _mark_disconnected function."""

    @pytest.fixture(autouse=True)
    def _mark_default_connected(
        self, clear_registry: None  # pylint: disable=unused-argument
    ) -> None:
        """Mark a fixed test address as connected before each test.

        This autouse fixture ensures the registry is cleared (via the `clear_registry` fixture)
        and then records the address "aabbccddeeff" as connected for the duration of the test.
        """
        _mark_connected("aabbccddeeff")

    def test_mark_disconnected_removes_from_registry(self) -> None:
        """Test that marking an address as disconnected removes it from registry."""
        assert "aabbccddeeff" in _CONNECTED_ADDRS
        _mark_disconnected("aabbccddeeff")
        assert "aabbccddeeff" not in _CONNECTED_ADDRS

    def test_mark_disconnected_with_none_does_nothing(self) -> None:
        """Test that marking None as disconnected does nothing."""
        initial_count = len(_CONNECTED_ADDRS)
        _mark_disconnected(None)
        assert len(_CONNECTED_ADDRS) == initial_count

    def test_mark_disconnected_with_empty_does_nothing(self) -> None:
        """Test that marking empty string as disconnected does nothing."""
        initial_count = len(_CONNECTED_ADDRS)
        _mark_disconnected("")
        assert len(_CONNECTED_ADDRS) == initial_count

    def test_mark_disconnected_cleanup_lock(self) -> None:
        """Verify marking an address disconnected removes its per-address lock.

        The test enters `_addr_lock_context` for "aabbccddeeff", marks it
        connected, and asserts the lock remains after the context exits. After
        calling `_mark_disconnected("aabbccddeeff")`, the test asserts the lock
        has been removed from `_ADDR_LOCKS`.
        """
        address = "aabbccddeeff"
        key = _addr_key(address)
        assert key is not None
        # Use context manager for proper holder count management
        with _addr_lock_context(address) as lock:
            with lock:
                _mark_connected(address)
        assert key in _ADDR_LOCKS  # Lock still exists
        _mark_disconnected(address)
        assert key not in _ADDR_LOCKS  # Lock cleaned up

    def test_mark_disconnected_ignores_non_owner(self) -> None:
        """Disconnect from a different owner should not clear an active claim."""

        class Owner:
            """Test owner stub."""

        owner_a = Owner()
        owner_b = Owner()
        key = _addr_key("aabbccddeeff")
        _mark_connected("aabbccddeeff", owner=owner_a)
        _mark_disconnected("aabbccddeeff", owner=owner_b)

        assert key in _CONNECTED_ADDRS

    def test_mark_disconnected_ignores_owner_when_record_has_no_owner_id(self) -> None:
        """Owner-scoped disconnect should not clear claims without a matching owner id."""
        key = _addr_key("aabbccddeeff")
        assert key is not None

        _mark_connected("aabbccddeeff")
        _mark_disconnected("aabbccddeeff", owner=object())

        assert key in _CONNECTED_ADDRS

    def test_mark_disconnected_clears_matching_provisional_claim(self) -> None:
        """Disconnect cleanup should also clear provisional connecting claims."""
        key = _addr_key("aabbccddeeff")
        assert key is not None

        # Remove the fixture's default connected claim so this assertion
        # isolates provisional-claim cleanup behavior.
        _mark_disconnected("aabbccddeeff")
        owner = _ConnectedOwner()
        _mark_connecting("aabbccddeeff", owner=owner)
        _mark_disconnected("aabbccddeeff", owner=owner)

        assert key not in _CONNECTING_ADDRS

    def test_mark_disconnected_ignores_non_owner_for_provisional_claim(self) -> None:
        """Owner-scoped disconnect should not clear another owner's provisional claim."""
        key = _addr_key("aabbccddeeff")
        assert key is not None

        # Remove fixture default connected claim so this exercises provisional path.
        _mark_disconnected("aabbccddeeff")
        owner_a = _ConnectedOwner()
        owner_b = _ConnectedOwner()
        _mark_connecting("aabbccddeeff", owner=owner_a)
        _mark_disconnected("aabbccddeeff", owner=owner_b)

        assert key in _CONNECTING_ADDRS

    def test_mark_disconnected_prunes_dead_provisional_owner_before_id_fallback(
        self,
    ) -> None:
        """Dead provisional weakrefs should be pruned without stale id(owner) checks."""
        key = _addr_key("aabbccddeeff")
        assert key is not None

        # Remove fixture default connected claim to isolate provisional behavior.
        _mark_disconnected("aabbccddeeff")
        owner = _ConnectedOwner()
        _mark_connecting("aabbccddeeff", owner=owner)
        stale_id = id(owner)
        del owner
        gc.collect()

        _mark_disconnected("aabbccddeeff", owner=_distinct_owner_token(stale_id))

        assert key not in _CONNECTING_ADDRS

    def test_mark_disconnected_keeps_unowned_provisional_claim_for_owner_scoped_call(
        self,
    ) -> None:
        """Owner-scoped disconnect should preserve unowned provisional claims without owner id."""
        key = _addr_key("aabbccddeeff")
        assert key is not None

        _mark_disconnected("aabbccddeeff")
        _mark_connecting("aabbccddeeff", owner=None)
        _mark_disconnected("aabbccddeeff", owner=object())

        assert key in _CONNECTING_ADDRS


@pytest.mark.usefixtures("clear_registry")
class TestClearConnecting:
    """Test cases for _clear_connecting helper behavior."""

    def test_clear_connecting_prunes_dead_owner_weakref_without_id_fallback(
        self,
    ) -> None:
        """Owner-scoped clear should prune dead weakref claims immediately."""
        key = _addr_key("aabbccddeeff")
        assert key is not None
        owner = _ConnectedOwner()
        _mark_connecting("aabbccddeeff", owner=owner)
        stale_id = id(owner)
        del owner
        gc.collect()

        _clear_connecting("aabbccddeeff", owner=_distinct_owner_token(stale_id))

        assert key not in _CONNECTING_ADDRS

    def test_clear_connecting_ignores_non_owner(self) -> None:
        """Owner-scoped clear should preserve provisional claims owned by another object."""
        key = _addr_key("aabbccddeeff")
        assert key is not None
        owner_a = _ConnectedOwner()
        owner_b = _ConnectedOwner()
        _mark_connecting("aabbccddeeff", owner=owner_a)

        _clear_connecting("aabbccddeeff", owner=owner_b)

        assert key in _CONNECTING_ADDRS

    def test_clear_connecting_with_none_address_is_no_op(self) -> None:
        """Clearing provisional state with None address should not mutate connecting claims."""
        _clear_connecting(None, owner=_ConnectedOwner())
        assert len(_CONNECTING_ADDRS) == 0


@pytest.mark.usefixtures("clear_registry")
class TestIsCurrentlyConnectedElsewhere:
    """Test cases for _is_currently_connected_elsewhere function."""

    def test_returns_true_for_connected_address(self) -> None:
        """Test that it returns True for a connected address."""
        _mark_connected("aabbccddeeff")
        assert _is_currently_connected_elsewhere("aabbccddeeff")

    def test_returns_true_for_provisional_connecting_claim(self) -> None:
        """Provisional connect claims should also block duplicate connects."""
        owner = _ConnectedOwner()
        _mark_connecting("aabbccddeeff", owner=owner)
        assert _is_currently_connected_elsewhere("aabbccddeeff", owner=object())

    def test_returns_false_for_non_connected_address(self) -> None:
        """Test that it returns False for a non-connected address."""
        assert not _is_currently_connected_elsewhere("aabbccddeeff")

    def test_returns_false_for_none_address(self) -> None:
        """Test that it returns False for None address."""
        assert not _is_currently_connected_elsewhere(None)

    def test_returns_false_for_empty_address(self) -> None:
        """Test that it returns False for empty address."""
        # Empty string normalizes to None via _addr_key, same as passing None directly.
        assert not _is_currently_connected_elsewhere("")

    def test_returns_false_for_same_owner(self) -> None:
        """A claim owned by this interface is not considered connected elsewhere."""
        owner = _ConnectedOwner()
        _mark_connected("aabbccddeeff", owner=owner)

        assert not _is_currently_connected_elsewhere("aabbccddeeff", owner=owner)

    def test_returns_false_for_same_owner_provisional_claim(self) -> None:
        """A provisional claim owned by this interface is not connected elsewhere."""
        owner = _ConnectedOwner()
        _mark_connecting("aabbccddeeff", owner=owner)

        assert not _is_currently_connected_elsewhere("aabbccddeeff", owner=owner)

    def test_returns_true_for_different_owner(self) -> None:
        """A live claim from another owner should be treated as connected elsewhere."""
        owner_a = _ConnectedOwner()
        owner_b = _ConnectedOwner()
        _mark_connected("aabbccddeeff", owner=owner_a)

        assert _is_currently_connected_elsewhere("aabbccddeeff", owner=owner_b)

    def test_prunes_dead_owner_claim(self) -> None:
        """Dead weakref owners should be pruned automatically."""
        owner = _ConnectedOwner()
        key = _addr_key("aabbccddeeff")
        _mark_connected("aabbccddeeff", owner=owner)
        del owner
        gc.collect()

        assert not _is_currently_connected_elsewhere("aabbccddeeff")
        assert key not in _CONNECTED_ADDRS

    def test_prunes_stale_unowned_provisional_claim(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Unowned provisional claims should expire after the stale window."""
        key = _addr_key("aabbccddeeff")
        assert key is not None
        _mark_connecting("aabbccddeeff")
        stale_now = (
            _CONNECTING_MARKED_AT[key]
            + BLEConfig.CONNECTION_GATE_UNOWNED_STALE_SECONDS
            + 1.0
        )
        monkeypatch.setattr(
            "meshtastic.interfaces.ble.gating.time.monotonic",
            lambda: stale_now,
        )

        assert not _is_currently_connected_elsewhere("aabbccddeeff")
        assert key not in _CONNECTING_ADDRS

    def test_unowned_provisional_claim_blocks_while_fresh(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Fresh unowned provisional claims should still block duplicate connections."""
        key = _addr_key("aabbccddeeff")
        assert key is not None
        _mark_connecting("aabbccddeeff")
        marked_at = _CONNECTING_MARKED_AT[key]
        monkeypatch.setattr(
            "meshtastic.interfaces.ble.gating.time.monotonic",
            lambda: marked_at,
        )

        assert _is_currently_connected_elsewhere("aabbccddeeff")
        assert key in _CONNECTING_ADDRS

    def test_prunes_dead_provisional_owner_weakref_in_connecting_claim_check(
        self,
    ) -> None:
        """Dead provisional owner weakrefs should be pruned before same-owner fallback checks."""
        key = _addr_key("aabbccddeeff")
        assert key is not None
        owner = _ConnectedOwner()
        _mark_connecting("aabbccddeeff", owner=owner)
        stale_id = id(owner)
        del owner
        gc.collect()

        assert not _is_currently_connected_elsewhere(
            "aabbccddeeff",
            owner=_distinct_owner_token(stale_id),
        )
        assert key not in _CONNECTING_ADDRS

    def test_phase2_recheck_observes_provisional_claim_after_connected_drop(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Phase-2 TOCTOU recheck should still block on fresh provisional claims."""
        address = "aabbccddeeff"
        key = _addr_key(address)
        assert key is not None
        connected_owner = _ConnectedOwner()
        provisional_owner = _ConnectedOwner()
        _mark_connected(address, owner=connected_owner)

        def _owner_state_probe(_owner: object) -> bool:
            _mark_disconnected(address)
            _mark_connecting(address, owner=provisional_owner)
            return True

        monkeypatch.setattr(
            "meshtastic.interfaces.ble.gating._owner_connected_state",
            _owner_state_probe,
        )

        assert _is_currently_connected_elsewhere(address, owner=object())
        assert key in _CONNECTING_ADDRS

    def test_prunes_owner_claim_when_owner_not_connected(self) -> None:
        """Claims from owners no longer connected should be pruned."""
        owner = _DisconnectedOwner()
        key = _addr_key("aabbccddeeff")
        _mark_connected("aabbccddeeff", owner=owner)

        assert not _is_currently_connected_elsewhere("aabbccddeeff")
        assert key not in _CONNECTED_ADDRS

    def test_prunes_owner_claim_when_owner_method_reports_disconnected(self) -> None:
        """Owner methods returning False should also be treated as stale claims."""

        class Owner:
            """Test owner stub."""

            def _is_connection_connected(self) -> bool:
                """Report whether the associated connection is currently established.

                Returns
                -------
                bool
                    `True` if the connection is established, `False` otherwise.
                """
                return False

        owner = Owner()
        key = _addr_key("aabbccddeeff")
        _mark_connected("aabbccddeeff", owner=owner)

        assert not _is_currently_connected_elsewhere("aabbccddeeff")
        assert key not in _CONNECTED_ADDRS

    def test_preserves_owner_claim_when_state_probe_raises(self) -> None:
        """State-probe exceptions should not aggressively prune active claims."""

        class Owner:
            """Test owner stub."""

            def _is_connection_connected(self) -> bool:
                """Probe whether the owner's connection is active.

                Returns
                -------
                bool
                    `True` if the owner's connection is active, `False` otherwise.
                """
                raise RuntimeError("probe failed")

        owner = Owner()
        key = _addr_key("aabbccddeeff")
        _mark_connected("aabbccddeeff", owner=owner)

        assert _is_currently_connected_elsewhere("aabbccddeeff", owner=object())
        assert key in _CONNECTED_ADDRS

    def test_prunes_stale_unowned_claim(self, monkeypatch: pytest.MonkeyPatch) -> None:
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
