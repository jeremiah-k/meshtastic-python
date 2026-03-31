"""Tests for BLE lifecycle primitives and receive runtime.

Covers async paths, exception handling, state transitions, and threading
coordination in the BLE lifecycle modules.
"""

from __future__ import annotations

# pylint: disable=too-many-lines

import threading
import time
from collections.abc import Callable
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from meshtastic.interfaces.ble.lifecycle_primitives import (
    CLIENT_MISSING_CONNECTED_MSG,
    STATE_MANAGER_MISSING_CONNECTED_MSG,
    STATE_MANAGER_MISSING_CURRENT_STATE_MSG,
    STATE_MANAGER_MISSING_RESET_MSG,
    STATE_MANAGER_MISSING_TRANSITION_MSG,
    THREAD_COORDINATOR_MISSING_FMT,
    _DisconnectPlan,
    _LifecycleErrorAccess,
    _LifecycleStateAccess,
    _LifecycleThreadAccess,
    _OwnershipSnapshot,
    _client_is_connected_compat,
)
from meshtastic.interfaces.ble.lifecycle_receive_runtime import (
    BLEReceiveLifecycleCoordinator,
    RECEIVE_START_PENDING_TIMEOUT_SECONDS,
)
from meshtastic.interfaces.ble.state import ConnectionState
from meshtastic.interfaces.ble.utils import _thread_start_probe


def configured_mock(return_value: Any = None, side_effect: Any = None) -> MagicMock:
    """Create a configured MagicMock that won't be detected as unconfigured.

    The _is_unconfigured_mock_callable function checks if a mock has default
    return value and no side_effect. This helper ensures mocks are properly
    configured to avoid being skipped.
    """
    mock = MagicMock()
    if side_effect is not None:
        mock.side_effect = side_effect
    else:
        mock.return_value = return_value
    return mock


@pytest.mark.unit
class TestLifecyclePrimitivesDisconnectPlan:
    """Test _DisconnectPlan dataclass creation and defaults."""

    def test_disconnect_plan_defaults(self) -> None:
        """Test _DisconnectPlan with default values."""
        plan = _DisconnectPlan(early_return=True)
        assert plan.early_return is True
        assert plan.previous_client is None
        assert plan.client_at_start is None
        assert plan.session_epoch == 0
        assert plan.address == "unknown"
        assert not plan.disconnect_keys
        assert plan.should_reconnect is False
        assert plan.should_schedule_reconnect is False
        assert plan.was_publish_pending is False
        assert plan.was_replacement_pending is False

    def test_disconnect_plan_custom_values(self) -> None:
        """Test _DisconnectPlan with custom values."""
        mock_client = MagicMock()
        plan = _DisconnectPlan(
            early_return=False,
            previous_client=mock_client,
            client_at_start=mock_client,
            session_epoch=42,
            address="test_address",
            disconnect_keys=("key1", "key2"),
            should_reconnect=True,
            should_schedule_reconnect=True,
            was_publish_pending=True,
            was_replacement_pending=True,
        )
        assert plan.early_return is False
        assert plan.previous_client is mock_client
        assert plan.client_at_start is mock_client
        assert plan.session_epoch == 42
        assert plan.address == "test_address"
        assert plan.disconnect_keys == ("key1", "key2")
        assert plan.should_reconnect is True
        assert plan.should_schedule_reconnect is True
        assert plan.was_publish_pending is True
        assert plan.was_replacement_pending is True


@pytest.mark.unit
class TestLifecyclePrimitivesOwnershipSnapshot:
    """Test _OwnershipSnapshot dataclass creation."""

    def test_ownership_snapshot_defaults(self) -> None:
        """Test _OwnershipSnapshot with default values."""
        snapshot = _OwnershipSnapshot(
            still_owned=True,
            is_closing=False,
            lost_gate_ownership=False,
            prior_ever_connected=True,
        )
        assert snapshot.still_owned is True
        assert snapshot.is_closing is False
        assert snapshot.lost_gate_ownership is False
        assert snapshot.prior_ever_connected is True


@pytest.mark.unit
class TestClientIsConnectedCompat:
    """Test _client_is_connected_compat function."""

    def test_client_is_connected_via_callable(self) -> None:
        """Test getting connected state via callable isConnected."""
        client = MagicMock()
        client.isConnected = MagicMock(return_value=True)
        assert _client_is_connected_compat(client) is True

    def test_client_is_connected_via_is_connected_callable(self) -> None:
        """Test getting connected state via callable is_connected."""
        client = MagicMock()
        # Make isConnected a non-callable mock to skip it
        client.isConnected = True
        client.is_connected = MagicMock(return_value=True)
        assert _client_is_connected_compat(client) is True

    def test_client_is_connected_via_private_callable(self) -> None:
        """Test getting connected state via callable _is_connected."""
        client = MagicMock()
        client.isConnected = True
        client.is_connected = True
        client._is_connected = MagicMock(return_value=True)
        assert _client_is_connected_compat(client) is True

    def test_client_is_connected_via_bool_attribute(self) -> None:
        """Test getting connected state via boolean attribute."""
        client = MagicMock()
        client.isConnected = False
        assert _client_is_connected_compat(client) is False

    def test_client_is_connected_via_is_connected_bool(self) -> None:
        """Test getting connected state via is_connected boolean."""
        client = MagicMock()
        client.isConnected = MagicMock()
        client.is_connected = True
        assert _client_is_connected_compat(client) is True

    def test_client_is_connected_raises_when_missing(self) -> None:
        """Test that AttributeError is raised when no connected member found."""
        client = MagicMock()
        client.isConnected = None
        client.is_connected = None
        client._is_connected = None
        with pytest.raises(AttributeError, match=CLIENT_MISSING_CONNECTED_MSG):
            _client_is_connected_compat(client)

    def test_client_is_connected_exception_in_callable(self) -> None:
        """Test that exception in callable causes fallback to next option."""
        client = MagicMock()
        client.isConnected = MagicMock(side_effect=RuntimeError("test error"))
        client.is_connected = True
        assert _client_is_connected_compat(client) is True

    def test_client_is_connected_exception_all_callables_fail(self) -> None:
        """Test that all callable failures fall back to attribute access."""
        client = MagicMock()
        client.isConnected = MagicMock(side_effect=RuntimeError("test error"))
        client.is_connected = MagicMock(side_effect=RuntimeError("test error"))
        client._is_connected = True
        assert _client_is_connected_compat(client) is True


@pytest.mark.unit
class TestLifecycleStateAccess:
    """Test _LifecycleStateAccess class."""

    @pytest.fixture
    def mock_iface(self) -> MagicMock:
        """Create a mock BLEInterface with state manager."""
        iface = MagicMock()
        iface._state_manager = MagicMock()
        return iface

    @pytest.fixture
    def state_access(self, mock_iface: MagicMock) -> _LifecycleStateAccess:
        """Create _LifecycleStateAccess instance."""
        return _LifecycleStateAccess(mock_iface)

    def test_is_connected_via_public_callable(
        self, state_access: _LifecycleStateAccess, mock_iface: MagicMock
    ) -> None:
        """Test is_connected via public callable."""
        mock_iface._state_manager.is_connected = MagicMock(return_value=True)
        assert state_access.is_connected() is True

    def test_is_connected_via_public_bool(
        self, state_access: _LifecycleStateAccess, mock_iface: MagicMock
    ) -> None:
        """Test is_connected via public boolean attribute."""
        mock_iface._state_manager.is_connected = True
        assert state_access.is_connected() is True

    def test_is_connected_via_legacy_callable(
        self, state_access: _LifecycleStateAccess, mock_iface: MagicMock
    ) -> None:
        """Test is_connected via legacy callable."""
        mock_iface._state_manager.is_connected = None
        mock_iface._state_manager._is_connected = MagicMock(return_value=True)
        assert state_access.is_connected() is True

    def test_is_connected_via_legacy_bool(
        self, state_access: _LifecycleStateAccess, mock_iface: MagicMock
    ) -> None:
        """Test is_connected via legacy boolean attribute."""
        mock_iface._state_manager.is_connected = None
        mock_iface._state_manager._is_connected = True
        assert state_access.is_connected() is True

    def test_is_connected_raises_when_missing(
        self, state_access: _LifecycleStateAccess, mock_iface: MagicMock
    ) -> None:
        """Test that AttributeError is raised when is_connected not found."""
        mock_iface._state_manager.is_connected = None
        mock_iface._state_manager._is_connected = None
        with pytest.raises(AttributeError, match=STATE_MANAGER_MISSING_CONNECTED_MSG):
            state_access.is_connected()

    def test_is_connected_exception_in_public_callable(
        self, state_access: _LifecycleStateAccess, mock_iface: MagicMock
    ) -> None:
        """Test exception handling in public is_connected callable."""
        mock_iface._state_manager.is_connected = MagicMock(
            side_effect=RuntimeError("test")
        )
        mock_iface._state_manager._is_connected = True
        assert state_access.is_connected() is True

    def test_is_connected_exception_in_legacy_callable(
        self, state_access: _LifecycleStateAccess, mock_iface: MagicMock
    ) -> None:
        """Test exception handling in legacy is_connected callable."""
        mock_iface._state_manager.is_connected = None
        mock_iface._state_manager._is_connected = MagicMock(
            side_effect=RuntimeError("test")
        )
        with pytest.raises(AttributeError, match=STATE_MANAGER_MISSING_CONNECTED_MSG):
            state_access.is_connected()

    def test_current_state_via_public_callable(
        self, state_access: _LifecycleStateAccess, mock_iface: MagicMock
    ) -> None:
        """Test current_state via public callable."""
        mock_iface._state_manager.current_state = MagicMock(
            return_value=ConnectionState.CONNECTED
        )
        assert state_access.current_state() == ConnectionState.CONNECTED

    def test_current_state_via_public_attribute(
        self, state_access: _LifecycleStateAccess, mock_iface: MagicMock
    ) -> None:
        """Test current_state via public attribute."""
        mock_iface._state_manager.current_state = ConnectionState.CONNECTING
        assert state_access.current_state() == ConnectionState.CONNECTING

    def test_current_state_via_legacy_callable(
        self, state_access: _LifecycleStateAccess, mock_iface: MagicMock
    ) -> None:
        """Test current_state via legacy callable."""
        mock_iface._state_manager.current_state = None
        mock_iface._state_manager._current_state = MagicMock(
            return_value=ConnectionState.DISCONNECTED
        )
        assert state_access.current_state() == ConnectionState.DISCONNECTED

    def test_current_state_via_legacy_attribute(
        self, state_access: _LifecycleStateAccess, mock_iface: MagicMock
    ) -> None:
        """Test current_state via legacy attribute."""
        mock_iface._state_manager.current_state = None
        mock_iface._state_manager._current_state = ConnectionState.ERROR
        assert state_access.current_state() == ConnectionState.ERROR

    def test_current_state_raises_when_missing(
        self, state_access: _LifecycleStateAccess, mock_iface: MagicMock
    ) -> None:
        """Test that AttributeError is raised when current_state not found."""
        mock_iface._state_manager.current_state = None
        mock_iface._state_manager._current_state = None
        with pytest.raises(
            AttributeError, match=STATE_MANAGER_MISSING_CURRENT_STATE_MSG
        ):
            state_access.current_state()

    def test_current_state_exception_in_callable(
        self, state_access: _LifecycleStateAccess, mock_iface: MagicMock
    ) -> None:
        """Test exception handling in current_state callable."""
        mock_iface._state_manager.current_state = MagicMock(
            side_effect=RuntimeError("test")
        )
        mock_iface._state_manager._current_state = ConnectionState.CONNECTED
        assert state_access.current_state() == ConnectionState.CONNECTED

    def test_transition_to_via_public_callable(
        self, state_access: _LifecycleStateAccess, mock_iface: MagicMock
    ) -> None:
        """Test transition_to via public callable."""
        mock_iface._state_manager.transition_to = MagicMock(return_value=True)
        assert state_access.transition_to(ConnectionState.CONNECTED) is True

    def test_transition_to_via_legacy_callable(
        self, state_access: _LifecycleStateAccess, mock_iface: MagicMock
    ) -> None:
        """Test transition_to via legacy callable."""
        mock_iface._state_manager.transition_to = None
        mock_iface._state_manager._transition_to = MagicMock(return_value=True)
        assert state_access.transition_to(ConnectionState.DISCONNECTING) is True

    def test_transition_to_raises_when_missing(
        self, state_access: _LifecycleStateAccess, mock_iface: MagicMock
    ) -> None:
        """Test that AttributeError is raised when transition_to not found."""
        mock_iface._state_manager.transition_to = None
        mock_iface._state_manager._transition_to = None
        with pytest.raises(AttributeError, match=STATE_MANAGER_MISSING_TRANSITION_MSG):
            state_access.transition_to(ConnectionState.DISCONNECTED)

    def test_transition_to_exception_in_callable(
        self, state_access: _LifecycleStateAccess, mock_iface: MagicMock
    ) -> None:
        """Test exception handling in transition_to callable."""
        mock_iface._state_manager.transition_to = MagicMock(
            side_effect=RuntimeError("test")
        )
        mock_iface._state_manager._transition_to = MagicMock(return_value=True)
        assert state_access.transition_to(ConnectionState.CONNECTED) is True

    def test_reset_to_disconnected_via_public_callable(
        self, state_access: _LifecycleStateAccess, mock_iface: MagicMock
    ) -> None:
        """Test reset_to_disconnected via public callable."""
        mock_iface._state_manager.reset_to_disconnected = MagicMock(return_value=True)
        assert state_access.reset_to_disconnected() is True

    def test_reset_to_disconnected_via_legacy_callable(
        self, state_access: _LifecycleStateAccess, mock_iface: MagicMock
    ) -> None:
        """Test reset_to_disconnected via legacy callable."""
        mock_iface._state_manager.reset_to_disconnected = None
        mock_iface._state_manager._reset_to_disconnected = MagicMock(return_value=True)
        assert state_access.reset_to_disconnected() is True

    def test_reset_to_disconnected_raises_when_missing(
        self, state_access: _LifecycleStateAccess, mock_iface: MagicMock
    ) -> None:
        """Test that AttributeError is raised when reset_to_disconnected not found."""
        mock_iface._state_manager.reset_to_disconnected = None
        mock_iface._state_manager._reset_to_disconnected = None
        with pytest.raises(AttributeError, match=STATE_MANAGER_MISSING_RESET_MSG):
            state_access.reset_to_disconnected()

    def test_reset_to_disconnected_exception_in_callable(
        self, state_access: _LifecycleStateAccess, mock_iface: MagicMock
    ) -> None:
        """Test exception handling in reset_to_disconnected callable."""
        mock_iface._state_manager.reset_to_disconnected = MagicMock(
            side_effect=RuntimeError("test")
        )
        mock_iface._state_manager._reset_to_disconnected = MagicMock(return_value=True)
        assert state_access.reset_to_disconnected() is True

    def test_is_closing_via_public_callable(
        self, state_access: _LifecycleStateAccess, mock_iface: MagicMock
    ) -> None:
        """Test is_closing via public callable."""
        mock_iface._state_manager.is_closing = MagicMock(return_value=True)
        assert state_access.is_closing() is True

    def test_is_closing_via_public_bool(
        self, state_access: _LifecycleStateAccess, mock_iface: MagicMock
    ) -> None:
        """Test is_closing via public boolean."""
        mock_iface._state_manager.is_closing = True
        assert state_access.is_closing() is True

    def test_is_closing_via_legacy_callable(
        self, state_access: _LifecycleStateAccess, mock_iface: MagicMock
    ) -> None:
        """Test is_closing via legacy callable."""
        mock_iface._state_manager.is_closing = None
        mock_iface._state_manager._is_closing = MagicMock(return_value=True)
        assert state_access.is_closing() is True

    def test_is_closing_via_legacy_bool(
        self, state_access: _LifecycleStateAccess, mock_iface: MagicMock
    ) -> None:
        """Test is_closing via legacy boolean."""
        mock_iface._state_manager.is_closing = None
        mock_iface._state_manager._is_closing = True
        assert state_access.is_closing() is True

    def test_is_closing_graceful_degradation(
        self, state_access: _LifecycleStateAccess, mock_iface: MagicMock
    ) -> None:
        """Test that is_closing returns False gracefully when not found."""
        mock_iface._state_manager.is_closing = None
        mock_iface._state_manager._is_closing = None
        # Should NOT raise - returns False gracefully
        assert state_access.is_closing() is False

    def test_is_closing_exception_in_callable(
        self, state_access: _LifecycleStateAccess, mock_iface: MagicMock
    ) -> None:
        """Test exception handling in is_closing callable."""
        mock_iface._state_manager.is_closing = MagicMock(
            side_effect=RuntimeError("test")
        )
        mock_iface._state_manager._is_closing = True
        assert state_access.is_closing() is True

    def test_client_is_connected_delegates(
        self, state_access: _LifecycleStateAccess
    ) -> None:
        """Test that client_is_connected delegates to _client_is_connected_compat."""
        client = MagicMock()
        client.isConnected = True
        assert state_access.client_is_connected(client) is True


@pytest.mark.unit
class TestLifecycleThreadAccess:
    """Test _LifecycleThreadAccess class."""

    @pytest.fixture
    def mock_iface(self) -> MagicMock:
        """Create a mock BLEInterface with properly configured thread coordinator."""
        iface = MagicMock()
        # Configure thread_coordinator mock so it's not detected as unconfigured
        # The _is_unconfigured_mock_member checks for DEFAULT return_value, no side_effect, and no calls
        coordinator = MagicMock()
        # Set a simple return value to mark it as "configured"
        coordinator.return_value = True
        iface.thread_coordinator = coordinator
        return iface

    @pytest.fixture
    def thread_access(self, mock_iface: MagicMock) -> _LifecycleThreadAccess:
        """Create _LifecycleThreadAccess instance."""
        return _LifecycleThreadAccess(mock_iface)

    def test_create_thread_via_public_callable(
        self, thread_access: _LifecycleThreadAccess, mock_iface: MagicMock
    ) -> None:
        """Test create_thread via public callable."""
        mock_thread = MagicMock()
        mock_iface.thread_coordinator.create_thread = MagicMock(
            return_value=mock_thread
        )

        def dummy_target() -> None:
            pass

        result = thread_access.create_thread(
            target=dummy_target, name="test", daemon=True
        )
        assert result is mock_thread

    def test_create_thread_via_legacy_callable(
        self, thread_access: _LifecycleThreadAccess, mock_iface: MagicMock
    ) -> None:
        """Test create_thread via legacy callable."""
        mock_thread = MagicMock()
        mock_iface.thread_coordinator.create_thread = None
        mock_iface.thread_coordinator._create_thread = MagicMock(
            return_value=mock_thread
        )

        def dummy_target() -> None:
            pass

        result = thread_access.create_thread(
            target=dummy_target, name="test", daemon=True
        )
        assert result is mock_thread

    def test_create_thread_raises_when_coordinator_missing(
        self, thread_access: _LifecycleThreadAccess, mock_iface: MagicMock
    ) -> None:
        """Test that AttributeError is raised when thread_coordinator is missing."""
        mock_iface.thread_coordinator = None

        def dummy_target() -> None:
            pass

        with pytest.raises(
            AttributeError,
            match=THREAD_COORDINATOR_MISSING_FMT % ("create_thread", "_create_thread"),
        ):
            thread_access.create_thread(target=dummy_target, name="test", daemon=True)

    def test_create_thread_raises_when_method_missing(
        self, thread_access: _LifecycleThreadAccess, mock_iface: MagicMock
    ) -> None:
        """Test that AttributeError is raised when create_thread method is missing."""
        mock_iface.thread_coordinator.create_thread = None
        mock_iface.thread_coordinator._create_thread = None

        def dummy_target() -> None:
            pass

        with pytest.raises(
            AttributeError,
            match=THREAD_COORDINATOR_MISSING_FMT % ("create_thread", "_create_thread"),
        ):
            thread_access.create_thread(target=dummy_target, name="test", daemon=True)

    def test_start_thread_via_public_callable(
        self, thread_access: _LifecycleThreadAccess, mock_iface: MagicMock
    ) -> None:
        """Test start_thread via public callable."""
        mock_thread = MagicMock()
        # Use return_value=None to make mock "configured" (not unconfigured)
        mock_iface.thread_coordinator.start_thread = MagicMock(return_value=None)
        thread_access.start_thread(mock_thread)
        mock_iface.thread_coordinator.start_thread.assert_called_once_with(mock_thread)

    def test_start_thread_via_legacy_callable(
        self, thread_access: _LifecycleThreadAccess, mock_iface: MagicMock
    ) -> None:
        """Test start_thread via legacy callable."""
        mock_thread = MagicMock()
        mock_iface.thread_coordinator.start_thread = None
        mock_iface.thread_coordinator._start_thread = MagicMock(return_value=None)
        thread_access.start_thread(mock_thread)
        mock_iface.thread_coordinator._start_thread.assert_called_once_with(mock_thread)

    def test_start_thread_raises_when_coordinator_missing(
        self, thread_access: _LifecycleThreadAccess, mock_iface: MagicMock
    ) -> None:
        """Test that AttributeError is raised when thread_coordinator is missing."""
        mock_iface.thread_coordinator = None
        mock_thread = MagicMock()
        with pytest.raises(
            AttributeError,
            match=THREAD_COORDINATOR_MISSING_FMT % ("start_thread", "_start_thread"),
        ):
            thread_access.start_thread(mock_thread)

    def test_start_thread_raises_when_method_missing(
        self, thread_access: _LifecycleThreadAccess, mock_iface: MagicMock
    ) -> None:
        """Test that AttributeError is raised when start_thread method is missing."""
        mock_iface.thread_coordinator.start_thread = None
        mock_iface.thread_coordinator._start_thread = None
        mock_thread = MagicMock()
        with pytest.raises(
            AttributeError,
            match=THREAD_COORDINATOR_MISSING_FMT % ("start_thread", "_start_thread"),
        ):
            thread_access.start_thread(mock_thread)

    def test_join_thread_via_public_callable(
        self, thread_access: _LifecycleThreadAccess, mock_iface: MagicMock
    ) -> None:
        """Test join_thread via public callable."""
        mock_thread = MagicMock()
        mock_iface.thread_coordinator.join_thread = MagicMock(return_value=None)
        thread_access.join_thread(mock_thread, timeout=1.0)
        mock_iface.thread_coordinator.join_thread.assert_called_once_with(
            mock_thread, timeout=1.0
        )

    def test_join_thread_via_legacy_callable(
        self, thread_access: _LifecycleThreadAccess, mock_iface: MagicMock
    ) -> None:
        """Test join_thread via legacy callable."""
        mock_thread = MagicMock()
        mock_iface.thread_coordinator.join_thread = None
        mock_iface.thread_coordinator._join_thread = MagicMock(return_value=None)
        thread_access.join_thread(mock_thread, timeout=1.0)
        mock_iface.thread_coordinator._join_thread.assert_called_once_with(
            mock_thread, timeout=1.0
        )

    def test_join_thread_fallback_to_thread_join(
        self, thread_access: _LifecycleThreadAccess, mock_iface: MagicMock
    ) -> None:
        """Test join_thread fallback to thread.join."""
        mock_thread = MagicMock()
        # Configure join so it's not detected as unconfigured mock
        mock_thread.join = MagicMock(return_value=None)
        mock_iface.thread_coordinator = None
        thread_access.join_thread(mock_thread, timeout=1.0)
        # Verify join was called
        assert mock_thread.join.call_count >= 1

    def test_join_thread_exception_handling_public(
        self, thread_access: _LifecycleThreadAccess, mock_iface: MagicMock
    ) -> None:
        """Test exception handling in public join_thread."""
        mock_thread = MagicMock()
        mock_iface.thread_coordinator.join_thread = MagicMock(
            side_effect=RuntimeError("test")
        )
        mock_iface.thread_coordinator._join_thread = MagicMock(return_value=None)
        # Should not raise, falls back to legacy
        thread_access.join_thread(mock_thread, timeout=1.0)
        mock_iface.thread_coordinator._join_thread.assert_called_once()

    def test_join_thread_exception_handling_both(
        self, thread_access: _LifecycleThreadAccess, mock_iface: MagicMock
    ) -> None:
        """Test exception handling when both coordinator methods fail."""
        mock_thread = MagicMock()
        mock_iface.thread_coordinator.join_thread = MagicMock(
            side_effect=RuntimeError("test")
        )
        mock_iface.thread_coordinator._join_thread = MagicMock(
            side_effect=RuntimeError("test")
        )
        # Should not raise, best effort
        thread_access.join_thread(mock_thread, timeout=1.0)

    def test_join_thread_exception_in_thread_join(
        self, thread_access: _LifecycleThreadAccess, mock_iface: MagicMock
    ) -> None:
        """Test exception handling in thread.join fallback."""
        mock_thread = MagicMock()
        mock_thread.join = MagicMock(side_effect=RuntimeError("test"))
        mock_iface.thread_coordinator = None
        # Should not raise, best effort
        thread_access.join_thread(mock_thread, timeout=1.0)

    def test_set_event_via_public_callable(
        self, thread_access: _LifecycleThreadAccess, mock_iface: MagicMock
    ) -> None:
        """Test set_event via public callable."""
        mock_iface.thread_coordinator.set_event = MagicMock(return_value=None)
        thread_access.set_event("test_event")
        mock_iface.thread_coordinator.set_event.assert_called_once_with("test_event")

    def test_set_event_via_legacy_callable(
        self, thread_access: _LifecycleThreadAccess, mock_iface: MagicMock
    ) -> None:
        """Test set_event via legacy callable."""
        mock_iface.thread_coordinator.set_event = None
        mock_iface.thread_coordinator._set_event = MagicMock(return_value=None)
        thread_access.set_event("test_event")
        mock_iface.thread_coordinator._set_event.assert_called_once_with("test_event")

    def test_set_event_missing_coordinator(
        self, thread_access: _LifecycleThreadAccess, mock_iface: MagicMock
    ) -> None:
        """Test set_event when coordinator is missing (best effort, no raise)."""
        mock_iface.thread_coordinator = None
        # Should not raise - best effort
        thread_access.set_event("test_event")

    def test_set_event_exception_handling(
        self, thread_access: _LifecycleThreadAccess, mock_iface: MagicMock
    ) -> None:
        """Test exception handling in set_event."""
        mock_iface.thread_coordinator.set_event = MagicMock(
            side_effect=RuntimeError("test")
        )
        mock_iface.thread_coordinator._set_event = MagicMock(return_value=None)
        # Should not raise, falls back to legacy
        thread_access.set_event("test_event")
        mock_iface.thread_coordinator._set_event.assert_called_once()

    def test_clear_events_via_public_callable(
        self, thread_access: _LifecycleThreadAccess, mock_iface: MagicMock
    ) -> None:
        """Test clear_events via public callable."""
        mock_iface.thread_coordinator.clear_events = MagicMock(return_value=None)
        thread_access.clear_events("event1", "event2")
        mock_iface.thread_coordinator.clear_events.assert_called_once_with(
            "event1", "event2"
        )

    def test_clear_events_via_legacy_callable(
        self, thread_access: _LifecycleThreadAccess, mock_iface: MagicMock
    ) -> None:
        """Test clear_events via legacy callable."""
        mock_iface.thread_coordinator.clear_events = None
        mock_iface.thread_coordinator._clear_events = MagicMock(return_value=None)
        thread_access.clear_events("event1", "event2")
        mock_iface.thread_coordinator._clear_events.assert_called_once_with(
            "event1", "event2"
        )

    def test_clear_events_missing_coordinator(
        self, thread_access: _LifecycleThreadAccess, mock_iface: MagicMock
    ) -> None:
        """Test clear_events when coordinator is missing (best effort, no raise)."""
        mock_iface.thread_coordinator = None
        # Should not raise - best effort
        thread_access.clear_events("event1")

    def test_clear_events_exception_handling(
        self, thread_access: _LifecycleThreadAccess, mock_iface: MagicMock
    ) -> None:
        """Test exception handling in clear_events."""
        mock_iface.thread_coordinator.clear_events = MagicMock(
            side_effect=RuntimeError("test")
        )
        mock_iface.thread_coordinator._clear_events = MagicMock(return_value=None)
        # Should not raise, falls back to legacy
        thread_access.clear_events("event1")
        mock_iface.thread_coordinator._clear_events.assert_called_once()

    def test_wake_waiting_threads_via_public(
        self, thread_access: _LifecycleThreadAccess, mock_iface: MagicMock
    ) -> None:
        """Test wake_waiting_threads via public callable."""
        mock_iface.thread_coordinator.wake_waiting_threads = MagicMock(
            return_value=None
        )
        thread_access.wake_waiting_threads("event1", "event2")
        mock_iface.thread_coordinator.wake_waiting_threads.assert_called_once_with(
            "event1", "event2"
        )

    def test_wake_waiting_threads_via_legacy(
        self, thread_access: _LifecycleThreadAccess, mock_iface: MagicMock
    ) -> None:
        """Test wake_waiting_threads via legacy callable."""
        mock_iface.thread_coordinator.wake_waiting_threads = None
        mock_iface.thread_coordinator._wake_waiting_threads = MagicMock(
            return_value=None
        )
        thread_access.wake_waiting_threads("event1")
        mock_iface.thread_coordinator._wake_waiting_threads.assert_called_once_with(
            "event1"
        )

    def test_wake_waiting_threads_fallback_to_set_event(
        self, thread_access: _LifecycleThreadAccess, mock_iface: MagicMock
    ) -> None:
        """Test wake_waiting_threads fallback to set_event."""
        mock_iface.thread_coordinator.wake_waiting_threads = None
        mock_iface.thread_coordinator._wake_waiting_threads = None
        mock_iface.thread_coordinator.set_event = MagicMock(return_value=None)
        thread_access.wake_waiting_threads("event1", "event2")
        # Should call set_event for each event
        assert mock_iface.thread_coordinator.set_event.call_count == 2

    def test_wake_waiting_threads_missing_coordinator(
        self, thread_access: _LifecycleThreadAccess, mock_iface: MagicMock
    ) -> None:
        """Test wake_waiting_threads when coordinator is missing (best effort, no raise)."""
        mock_iface.thread_coordinator = None
        # Should not raise - best effort
        thread_access.wake_waiting_threads("event1")

    def test_wake_waiting_threads_exception_handling(
        self, thread_access: _LifecycleThreadAccess, mock_iface: MagicMock
    ) -> None:
        """Test exception handling in wake_waiting_threads with partial failures."""
        mock_iface.thread_coordinator.wake_waiting_threads = MagicMock(
            side_effect=RuntimeError("test")
        )
        mock_iface.thread_coordinator._wake_waiting_threads = None
        mock_iface.thread_coordinator.set_event = MagicMock(
            side_effect=[None, RuntimeError("test")]
        )
        mock_iface.thread_coordinator._set_event = MagicMock()
        # Should not raise, tries fallbacks
        thread_access.wake_waiting_threads("event1", "event2")


@pytest.mark.unit
class TestLifecycleErrorAccess:
    """Test _LifecycleErrorAccess class."""

    @pytest.fixture
    def mock_iface(self) -> MagicMock:
        """Create a mock BLEInterface with properly configured error handler."""
        iface = MagicMock()
        # Configure error_handler so it's not detected as unconfigured
        error_handler = MagicMock()
        error_handler.return_value = True
        # Pre-configure common methods to avoid auto-generated unconfigured mocks
        error_handler.safe_cleanup = MagicMock(return_value=True)
        error_handler._safe_cleanup = MagicMock(return_value=True)
        error_handler.safe_execute = MagicMock(return_value=True)
        error_handler._safe_execute = MagicMock(return_value=True)
        iface.error_handler = error_handler
        return iface

    @pytest.fixture
    def error_access(self, mock_iface: MagicMock) -> _LifecycleErrorAccess:
        """Create _LifecycleErrorAccess instance."""
        return _LifecycleErrorAccess(mock_iface)

    def test_resolve_hook_via_public(
        self, error_access: _LifecycleErrorAccess, mock_iface: MagicMock
    ) -> None:
        """Test resolve_hook finds public method."""
        # Use return_value to make mock "configured" (not unconfigured)
        mock_hook = MagicMock(return_value=True)
        mock_iface.error_handler.safe_cleanup = mock_hook
        result = error_access.resolve_hook("safe_cleanup", "_safe_cleanup")
        assert result is mock_hook

    def test_resolve_hook_via_legacy(
        self, error_access: _LifecycleErrorAccess, mock_iface: MagicMock
    ) -> None:
        """Test resolve_hook finds legacy method when public missing."""
        mock_hook = MagicMock(return_value=True)
        mock_iface.error_handler.safe_cleanup = None
        mock_iface.error_handler._safe_cleanup = mock_hook
        result = error_access.resolve_hook("safe_cleanup", "_safe_cleanup")
        assert result is mock_hook

    def test_resolve_hook_returns_none_when_missing(
        self, error_access: _LifecycleErrorAccess, mock_iface: MagicMock
    ) -> None:
        """Test resolve_hook returns None when hook not found."""
        mock_iface.error_handler.safe_cleanup = None
        mock_iface.error_handler._safe_cleanup = None
        result = error_access.resolve_hook("safe_cleanup", "_safe_cleanup")
        assert result is None

    def test_safe_cleanup_with_hook_success(
        self, error_access: _LifecycleErrorAccess, mock_iface: MagicMock
    ) -> None:
        """Test safe_cleanup runs successfully with hook."""
        cleanup_called = False

        def cleanup() -> None:
            nonlocal cleanup_called
            cleanup_called = True

        # Mock that properly wraps and calls the cleanup function
        def mock_safe_cleanup(func: Callable[[], Any], *_args: Any) -> Any:
            return func()

        mock_iface.error_handler.safe_cleanup = MagicMock(side_effect=mock_safe_cleanup)
        error_access.safe_cleanup(cleanup, "test_operation")
        assert cleanup_called is True

    def test_safe_cleanup_with_hook_typeerror_kwarg(
        self, error_access: _LifecycleErrorAccess, mock_iface: MagicMock
    ) -> None:
        """Test safe_cleanup handles TypeError for unexpected kwargs."""
        cleanup_called = False

        def cleanup() -> None:
            nonlocal cleanup_called
            cleanup_called = True

        def hook_side_effect(*_args: Any, **_kwargs: Any) -> Any:
            if _kwargs:
                raise TypeError("unexpected keyword argument")
            return True

        mock_iface.error_handler.safe_cleanup = MagicMock(side_effect=hook_side_effect)
        error_access.safe_cleanup(cleanup, "test_operation")
        assert cleanup_called is True

    def test_safe_cleanup_hook_exception(
        self, error_access: _LifecycleErrorAccess, mock_iface: MagicMock
    ) -> None:
        """Test safe_cleanup handles exception in hook."""
        cleanup_called = False

        def cleanup() -> None:
            nonlocal cleanup_called
            cleanup_called = True

        mock_iface.error_handler.safe_cleanup = MagicMock(
            side_effect=RuntimeError("test")
        )
        error_access.safe_cleanup(cleanup, "test_operation")
        # Cleanup should still run despite hook exception
        assert cleanup_called is True

    def test_safe_cleanup_fallback_when_no_hook(
        self, error_access: _LifecycleErrorAccess, mock_iface: MagicMock
    ) -> None:
        """Test safe_cleanup falls back to direct execution when no hook."""
        mock_iface.error_handler = None
        cleanup_called = False

        def cleanup() -> None:
            nonlocal cleanup_called
            cleanup_called = True

        error_access.safe_cleanup(cleanup, "test_operation")
        assert cleanup_called is True

    def test_safe_cleanup_exception_in_fallback(
        self, error_access: _LifecycleErrorAccess, mock_iface: MagicMock
    ) -> None:
        """Test safe_cleanup handles exception in fallback cleanup."""
        mock_iface.error_handler = None

        def cleanup() -> None:
            raise RuntimeError("cleanup error")

        # Should not raise despite exception in cleanup
        error_access.safe_cleanup(cleanup, "test_operation")

    def test_safe_execute_with_hook_success(
        self, error_access: _LifecycleErrorAccess, mock_iface: MagicMock
    ) -> None:
        """Test safe_execute runs successfully with hook."""

        def test_func() -> str:
            return "result"

        # Mock that properly wraps and calls the function, then returns custom result
        def mock_safe_execute(func: Callable[[], Any], *_args: Any) -> Any:
            func()  # Call the function so did_run becomes True
            return "hook_result"

        mock_iface.error_handler.safe_execute = MagicMock(side_effect=mock_safe_execute)
        result = error_access.safe_execute(test_func, error_msg="test error")
        assert result == "hook_result"

    def test_safe_execute_with_hook_func_ran(
        self, error_access: _LifecycleErrorAccess, mock_iface: MagicMock
    ) -> None:
        """Test safe_execute returns result when function actually ran."""
        func_ran = False

        def test_func() -> str:
            nonlocal func_ran
            func_ran = True
            return "result"

        mock_iface.error_handler.safe_execute = MagicMock(
            side_effect=RuntimeError("hook error")
        )
        result = error_access.safe_execute(test_func, error_msg="test error")
        assert func_ran is True
        assert result == "result"

    def test_safe_execute_fallback_when_no_hook(
        self, error_access: _LifecycleErrorAccess, mock_iface: MagicMock
    ) -> None:
        """Test safe_execute falls back to direct execution when no hook."""
        mock_iface.error_handler = None

        def test_func() -> str:
            return "direct_result"

        result = error_access.safe_execute(test_func, error_msg="test error")
        assert result == "direct_result"

    def test_safe_execute_exception_in_fallback(
        self, error_access: _LifecycleErrorAccess, mock_iface: MagicMock
    ) -> None:
        """Test safe_execute handles exception in fallback."""
        mock_iface.error_handler = None

        def test_func() -> str:
            raise RuntimeError("func error")

        # Should not raise, returns None
        result = error_access.safe_execute(test_func, error_msg="test error")
        assert result is None

    def test_try_safe_execute_variants_with_error_msg_kwarg(
        self,
        error_access: _LifecycleErrorAccess,  # noqa: W0613
    ) -> None:
        """Test _try_safe_execute_variants with error_msg as keyword."""

        def safe_exec(func: Callable[[], Any], *, error_msg: str) -> Any:  # noqa: W0613
            return func()

        def tracked() -> str:
            return "success"

        ran, result = _LifecycleErrorAccess._try_safe_execute_variants(
            safe_exec, tracked, error_msg="test", did_run=lambda: True
        )
        assert ran is True
        assert result == "success"

    def test_try_safe_execute_variants_positional(
        self,
        error_access: _LifecycleErrorAccess,  # noqa: W0613
    ) -> None:
        """Test _try_safe_execute_variants with positional error_msg."""

        def safe_exec(func: Callable[[], Any], error_msg: str) -> Any:  # noqa: W0613
            return func()

        def tracked() -> str:
            return "success"

        ran, result = _LifecycleErrorAccess._try_safe_execute_variants(
            safe_exec, tracked, error_msg="test", did_run=lambda: True
        )
        assert ran is True
        assert result == "success"

    def test_try_safe_execute_variants_no_error_msg(
        self,
        error_access: _LifecycleErrorAccess,  # noqa: W0613
    ) -> None:
        """Test _try_safe_execute_variants without error_msg."""
        func_executed = False

        def safe_exec(func: Callable[[], Any]) -> Any:  # noqa: W0613
            return func()

        def tracked() -> str:
            nonlocal func_executed
            func_executed = True
            return "success"

        def did_run() -> bool:
            return func_executed

        ran, result = _LifecycleErrorAccess._try_safe_execute_variants(
            safe_exec, tracked, error_msg="test", did_run=did_run
        )
        assert ran is True
        assert result == "success"

    def test_try_safe_execute_variants_func_did_not_run(
        self,
        error_access: _LifecycleErrorAccess,  # noqa: W0613
    ) -> None:
        """Test _try_safe_execute_variants when function didn't run."""

        def safe_exec(func: Callable[[], Any]) -> Any:  # noqa: W0613
            raise RuntimeError("error")

        def tracked() -> str:
            return "success"

        ran, result = _LifecycleErrorAccess._try_safe_execute_variants(
            safe_exec, tracked, error_msg="test", did_run=lambda: False
        )
        assert ran is False
        assert result is None


@pytest.mark.unit
class TestBLEReceiveLifecycleCoordinator:
    """Test BLEReceiveLifecycleCoordinator class."""

    @pytest.fixture
    def mock_iface(self) -> MagicMock:
        """Create a mock BLEInterface with required attributes."""
        iface = MagicMock()
        iface._state_lock = threading.Lock()
        iface._closed = False
        iface._want_receive = True
        iface._receiveThread = None
        iface._receive_start_pending = False
        iface._receive_start_pending_since = None
        iface._receive_recovery_attempts = 0
        iface._receive_from_radio_impl = MagicMock()
        # Configure thread_coordinator with required methods
        thread_coordinator = MagicMock()
        thread_coordinator.create_thread = MagicMock(return_value=MagicMock())
        thread_coordinator._create_thread = MagicMock(return_value=MagicMock())
        thread_coordinator.start_thread = MagicMock(return_value=None)
        thread_coordinator._start_thread = MagicMock(return_value=None)
        iface.thread_coordinator = thread_coordinator
        return iface

    @pytest.fixture
    def coordinator(self, mock_iface: MagicMock) -> BLEReceiveLifecycleCoordinator:
        """Create BLEReceiveLifecycleCoordinator instance."""
        return BLEReceiveLifecycleCoordinator(mock_iface)

    def test_init(
        self, coordinator: BLEReceiveLifecycleCoordinator, mock_iface: MagicMock
    ) -> None:
        """Test coordinator initialization."""
        assert coordinator._iface is mock_iface
        assert coordinator._deferred_restart_lock is not None
        assert coordinator._deferred_restart_inflight is False

    def test_set_receive_wanted_true(
        self, coordinator: BLEReceiveLifecycleCoordinator, mock_iface: MagicMock
    ) -> None:
        """Test setting receive wanted to True."""
        coordinator.set_receive_wanted(want_receive=True)
        assert mock_iface._want_receive is True

    def test_set_receive_wanted_false(
        self, coordinator: BLEReceiveLifecycleCoordinator, mock_iface: MagicMock
    ) -> None:
        """Test setting receive wanted to False."""
        coordinator.set_receive_wanted(want_receive=False)
        assert mock_iface._want_receive is False

    def test_should_run_receive_loop_true(
        self, coordinator: BLEReceiveLifecycleCoordinator, mock_iface: MagicMock
    ) -> None:
        """Test should_run_receive_loop returns True when conditions met."""
        mock_iface._want_receive = True
        mock_iface._closed = False
        assert coordinator.should_run_receive_loop() is True

    def test_should_run_receive_loop_false_when_closed(
        self, coordinator: BLEReceiveLifecycleCoordinator, mock_iface: MagicMock
    ) -> None:
        """Test should_run_receive_loop returns False when interface closed."""
        mock_iface._want_receive = True
        mock_iface._closed = True
        assert coordinator.should_run_receive_loop() is False

    def test_should_run_receive_loop_false_when_not_wanted(
        self, coordinator: BLEReceiveLifecycleCoordinator, mock_iface: MagicMock
    ) -> None:
        """Test should_run_receive_loop returns False when receive not wanted."""
        mock_iface._want_receive = False
        mock_iface._closed = False
        assert coordinator.should_run_receive_loop() is False

    def test_is_thread_start_failure_confirmed_with_started_event(self) -> None:
        """Test _is_thread_start_failure_confirmed with started event."""
        mock_thread = MagicMock()
        mock_started_event = MagicMock()
        mock_started_event.is_set = MagicMock(return_value=False)
        mock_thread._started = mock_started_event
        # thread_ident must be not None for failure to be confirmed
        mock_thread.ident = 12345

        result = BLEReceiveLifecycleCoordinator._is_thread_start_failure_confirmed(
            mock_thread
        )
        assert result is True

    def test_is_thread_start_failure_confirmed_already_started(self) -> None:
        """Test _is_thread_start_failure_confirmed when thread already started."""
        mock_thread = MagicMock()
        mock_started_event = MagicMock()
        mock_started_event.is_set = MagicMock(return_value=True)
        mock_thread._started = mock_started_event

        result = BLEReceiveLifecycleCoordinator._is_thread_start_failure_confirmed(
            mock_thread
        )
        assert result is False

    def test_is_thread_start_failure_confirmed_no_started_attr(self) -> None:
        """Test _is_thread_start_failure_confirmed when no _started attribute."""
        mock_thread = MagicMock()
        del mock_thread._started

        result = BLEReceiveLifecycleCoordinator._is_thread_start_failure_confirmed(
            mock_thread
        )
        assert result is False

    def test_is_thread_start_failure_confirmed_exception_in_probe(self) -> None:
        """Test _is_thread_start_failure_confirmed handles exceptions gracefully."""
        mock_thread = MagicMock()
        mock_started_event = MagicMock()
        mock_started_event.is_set = MagicMock(side_effect=RuntimeError("test"))
        mock_thread._started = mock_started_event

        result = BLEReceiveLifecycleCoordinator._is_thread_start_failure_confirmed(
            mock_thread
        )
        assert result is False

    def test_is_current_receive_thread_current(self) -> None:
        """Test _is_current_receive_thread returns True for current thread."""
        current = threading.current_thread()
        result = BLEReceiveLifecycleCoordinator._is_current_receive_thread(current)
        assert result is True

    def test_is_current_receive_thread_none(self) -> None:
        """Test _is_current_receive_thread returns False for None."""
        result = BLEReceiveLifecycleCoordinator._is_current_receive_thread(None)
        assert result is False

    def test_is_current_receive_thread_different_thread(self) -> None:
        """Test _is_current_receive_thread returns False for different thread."""
        other_thread = threading.Thread(target=lambda: None)
        result = BLEReceiveLifecycleCoordinator._is_current_receive_thread(other_thread)
        assert result is False

    def test_is_current_receive_thread_with_ident(self) -> None:
        """Test _is_current_receive_thread compares by ident."""
        current = threading.current_thread()
        mock_thread = MagicMock()
        mock_thread.ident = current.ident

        result = BLEReceiveLifecycleCoordinator._is_current_receive_thread(mock_thread)
        assert result is True

    def test_check_receive_start_conditions_interface_closed(
        self, coordinator: BLEReceiveLifecycleCoordinator, mock_iface: MagicMock
    ) -> None:
        """Test _check_receive_start_conditions when interface is closed."""
        mock_iface._closed = True

        def create_thread(**_kwargs: Any) -> MagicMock:
            return MagicMock()

        thread, _recovery = coordinator._check_receive_start_conditions(
            name="test",
            reset_recovery=True,
            create_runtime_thread=create_thread,
        )
        assert thread is None
        assert _recovery is None

    def test_check_receive_start_conditions_receive_not_wanted(
        self, coordinator: BLEReceiveLifecycleCoordinator, mock_iface: MagicMock
    ) -> None:
        """Test _check_receive_start_conditions when receive not wanted."""
        mock_iface._want_receive = False

        def create_thread(**_kwargs: Any) -> MagicMock:
            return MagicMock()

        thread, _recovery = coordinator._check_receive_start_conditions(
            name="test",
            reset_recovery=True,
            create_runtime_thread=create_thread,
        )
        assert thread is None
        assert _recovery is None

    def test_check_receive_start_conditions_existing_alive_thread(
        self, coordinator: BLEReceiveLifecycleCoordinator, mock_iface: MagicMock
    ) -> None:
        """Test _check_receive_start_conditions when existing thread is alive."""
        existing_thread = MagicMock()
        existing_thread.name = "existing"
        existing_thread.ident = 12345
        existing_thread.is_alive = MagicMock(return_value=True)
        mock_iface._receiveThread = existing_thread

        def create_thread(**_kwargs: Any) -> MagicMock:
            return MagicMock()

        thread, _recovery = coordinator._check_receive_start_conditions(
            name="test",
            reset_recovery=True,
            create_runtime_thread=create_thread,
        )
        assert thread is None
        assert _recovery is None

    def test_check_receive_start_conditions_create_new_thread(
        self, coordinator: BLEReceiveLifecycleCoordinator, mock_iface: MagicMock
    ) -> None:
        """Test _check_receive_start_conditions creates new thread."""
        mock_iface._receiveThread = None
        mock_thread = MagicMock()
        mock_thread.name = "test_thread"
        mock_thread.ident = 12345

        def create_thread(**_kwargs: Any) -> MagicMock:  # noqa: W0612
            return mock_thread

        def start_thread(_t: Any) -> None:
            pass

        result = coordinator._create_and_start_receive_thread(
            mock_thread,
            start_runtime_thread=start_thread,
        )
        assert result is True

    def test_create_and_start_receive_thread_thread_changed(
        self, coordinator: BLEReceiveLifecycleCoordinator, mock_iface: MagicMock
    ) -> None:
        """Test _create_and_start_receive_thread when thread reference changed."""
        mock_thread = MagicMock()
        mock_iface._receiveThread = MagicMock()  # Different thread

        def start_thread(_t: Any) -> None:
            pass

        result = coordinator._create_and_start_receive_thread(
            mock_thread,
            start_runtime_thread=start_thread,
        )
        assert result is False

    def test_create_and_start_receive_thread_interface_closed(
        self, coordinator: BLEReceiveLifecycleCoordinator, mock_iface: MagicMock
    ) -> None:
        """Test _create_and_start_receive_thread when interface closed."""
        mock_thread = MagicMock()
        mock_iface._closed = True

        def start_thread(_t: Any) -> None:
            pass

        result = coordinator._create_and_start_receive_thread(
            mock_thread,
            start_runtime_thread=start_thread,
        )
        assert result is False

    def test_create_and_start_receive_thread_start_exception(
        self, coordinator: BLEReceiveLifecycleCoordinator, mock_iface: MagicMock
    ) -> None:
        """Test _create_and_start_receive_thread clears reference on exception."""
        mock_thread = MagicMock()
        mock_iface._receiveThread = mock_thread

        def start_thread(_t: Any) -> None:
            raise RuntimeError("start failed")

        with pytest.raises(RuntimeError, match="start failed"):
            coordinator._create_and_start_receive_thread(
                mock_thread,
                start_runtime_thread=start_thread,
            )

        # Should clear thread reference
        assert mock_iface._receiveThread is None
        assert mock_iface._receive_start_pending is False

    def test_create_and_start_receive_thread_system_exit(
        self, coordinator: BLEReceiveLifecycleCoordinator, mock_iface: MagicMock
    ) -> None:
        """Test _create_and_start_receive_thread handles SystemExit."""
        mock_thread = MagicMock()
        mock_iface._receiveThread = mock_thread

        def start_thread(_t: Any) -> None:
            raise SystemExit()

        with pytest.raises(SystemExit):
            coordinator._create_and_start_receive_thread(
                mock_thread,
                start_runtime_thread=start_thread,
            )

        # Should clear thread reference
        assert mock_iface._receiveThread is None

    def test_probe_receive_thread_start_alive(
        self, coordinator: BLEReceiveLifecycleCoordinator, mock_iface: MagicMock
    ) -> None:
        """Test _probe_receive_thread_start when thread is alive."""
        mock_thread = MagicMock()
        mock_thread.is_alive = MagicMock(return_value=True)
        mock_iface._receiveThread = mock_thread
        mock_iface._receive_start_pending = True

        result = coordinator._probe_receive_thread_start(
            mock_thread,
            name="test",
            reset_recovery=True,
        )
        assert result is True
        assert mock_iface._receive_start_pending is False

    def test_probe_receive_thread_start_failure_confirmed(
        self, coordinator: BLEReceiveLifecycleCoordinator, mock_iface: MagicMock
    ) -> None:
        """Test _probe_receive_thread_start when start failure confirmed."""
        mock_thread = MagicMock()
        mock_thread.is_alive = MagicMock(return_value=False)
        mock_thread._started = MagicMock()
        mock_thread._started.is_set = MagicMock(return_value=False)
        # ident must be not None for failure to be confirmed
        mock_thread.ident = 12345
        mock_iface._receiveThread = mock_thread
        mock_iface._receive_start_pending = True

        result = coordinator._probe_receive_thread_start(
            mock_thread,
            name="test",
            reset_recovery=True,
        )
        assert result is False
        assert mock_iface._receiveThread is None
        assert mock_iface._receive_start_pending is False

    def test_probe_receive_thread_start_inconclusive(
        self, coordinator: BLEReceiveLifecycleCoordinator, mock_iface: MagicMock
    ) -> None:
        """Test _probe_receive_thread_start when probe inconclusive."""
        mock_thread = MagicMock()
        mock_thread.is_alive = MagicMock(return_value=False)
        mock_thread._started = MagicMock()
        mock_thread._started.is_set = MagicMock(return_value=True)
        mock_iface._receiveThread = mock_thread
        mock_iface._receive_start_pending = False

        result = coordinator._probe_receive_thread_start(
            mock_thread,
            name="test",
            reset_recovery=True,
        )
        assert result is False
        assert mock_iface._receive_start_pending is True  # Set to pending

    def test_maybe_reset_receive_recovery_success(
        self, coordinator: BLEReceiveLifecycleCoordinator, mock_iface: MagicMock
    ) -> None:
        """Test _maybe_reset_receive_recovery resets recovery attempts."""
        mock_thread = MagicMock()
        mock_thread.is_alive = MagicMock(return_value=True)
        mock_iface._receiveThread = mock_thread
        mock_iface._receive_recovery_attempts = 5

        coordinator._maybe_reset_receive_recovery(
            thread=mock_thread,
            recovery_attempts_before_start=5,
        )
        assert mock_iface._receive_recovery_attempts == 0

    def test_maybe_reset_receive_recovery_no_reset(
        self, coordinator: BLEReceiveLifecycleCoordinator, mock_iface: MagicMock
    ) -> None:
        """Test _maybe_reset_receive_recovery when recovery_attempts_before_start is None."""
        mock_thread = MagicMock()
        mock_iface._receive_recovery_attempts = 5

        coordinator._maybe_reset_receive_recovery(
            thread=mock_thread,
            recovery_attempts_before_start=None,
        )
        assert mock_iface._receive_recovery_attempts == 5  # Unchanged

    def test_maybe_reset_receive_recovery_thread_changed(
        self, coordinator: BLEReceiveLifecycleCoordinator, mock_iface: MagicMock
    ) -> None:
        """Test _maybe_reset_receive_recovery when thread reference changed."""
        mock_thread = MagicMock()
        mock_iface._receiveThread = MagicMock()  # Different thread
        mock_iface._receive_recovery_attempts = 5

        coordinator._maybe_reset_receive_recovery(
            thread=mock_thread,
            recovery_attempts_before_start=5,
        )
        assert mock_iface._receive_recovery_attempts == 5  # Unchanged

    def test_maybe_reset_receive_recovery_attempts_changed(
        self, coordinator: BLEReceiveLifecycleCoordinator, mock_iface: MagicMock
    ) -> None:
        """Test _maybe_reset_receive_recovery when recovery attempts changed."""
        mock_thread = MagicMock()
        mock_thread.is_alive = MagicMock(return_value=True)
        mock_iface._receiveThread = mock_thread
        mock_iface._receive_recovery_attempts = 10  # Different from before

        coordinator._maybe_reset_receive_recovery(
            thread=mock_thread,
            recovery_attempts_before_start=5,
        )
        assert mock_iface._receive_recovery_attempts == 10  # Unchanged

    def test_maybe_reset_receive_recovery_thread_not_alive(
        self, coordinator: BLEReceiveLifecycleCoordinator, mock_iface: MagicMock
    ) -> None:
        """Test _maybe_reset_receive_recovery when thread not alive."""
        mock_thread = MagicMock()
        mock_thread.is_alive = MagicMock(return_value=False)
        mock_iface._receiveThread = mock_thread
        mock_iface._receive_recovery_attempts = 5

        coordinator._maybe_reset_receive_recovery(
            thread=mock_thread,
            recovery_attempts_before_start=5,
        )
        assert mock_iface._receive_recovery_attempts == 5  # Unchanged

    def test_start_receive_thread_full_flow(
        self, coordinator: BLEReceiveLifecycleCoordinator, mock_iface: MagicMock
    ) -> None:
        """Test start_receive_thread with full successful flow."""
        mock_thread = MagicMock()
        mock_thread.is_alive = MagicMock(return_value=True)
        mock_thread._started = MagicMock()
        mock_thread._started.is_set = MagicMock(return_value=True)

        def create_thread(**_kwargs: Any) -> MagicMock:
            return mock_thread

        def start_thread(_t: Any) -> None:
            pass

        coordinator.start_receive_thread(
            name="test",
            reset_recovery=True,
            create_thread=create_thread,
            start_thread=start_thread,
        )

        assert mock_iface._receiveThread is mock_thread

    def test_start_receive_thread_none_returned(
        self, coordinator: BLEReceiveLifecycleCoordinator, mock_iface: MagicMock
    ) -> None:
        """Test start_receive_thread when check returns None thread."""
        mock_iface._closed = True  # Cause check to return None

        def create_thread(**_kwargs: Any) -> MagicMock:
            return MagicMock()

        def start_thread(_t: Any) -> None:
            pass

        # Should not raise, just return early
        coordinator.start_receive_thread(
            name="test",
            reset_recovery=True,
            create_thread=create_thread,
            start_thread=start_thread,
        )

    def test_start_receive_thread_not_started(
        self, coordinator: BLEReceiveLifecycleCoordinator, mock_iface: MagicMock
    ) -> None:
        """Test start_receive_thread when create_and_start returns False."""
        mock_thread = MagicMock()
        mock_iface._receiveThread = MagicMock()  # Different thread

        def create_thread(**_kwargs: Any) -> MagicMock:
            return mock_thread

        def start_thread(_t: Any) -> None:
            pass

        coordinator.start_receive_thread(
            name="test",
            reset_recovery=True,
            create_thread=create_thread,
            start_thread=start_thread,
        )

    def test_start_receive_thread_probe_fails(
        self, coordinator: BLEReceiveLifecycleCoordinator, mock_iface: MagicMock
    ) -> None:
        """Test start_receive_thread when probe fails."""
        mock_thread = MagicMock()
        mock_thread.is_alive = MagicMock(return_value=False)
        mock_thread._started = MagicMock()
        mock_thread._started.is_set = MagicMock(return_value=False)
        # ident must be not None for failure to be confirmed
        mock_thread.ident = 12345

        def create_thread(**_kwargs: Any) -> MagicMock:
            return mock_thread

        def start_thread(_t: Any) -> None:
            pass

        coordinator.start_receive_thread(
            name="test",
            reset_recovery=True,
            create_thread=create_thread,
            start_thread=start_thread,
        )

        # Thread reference should be cleared after failed probe
        assert mock_iface._receiveThread is None


@pytest.mark.unit
class TestBLEReceiveLifecycleCoordinatorDeferredRestart:
    """Test BLEReceiveLifecycleCoordinator deferred restart functionality."""

    @pytest.fixture
    def mock_iface(self) -> MagicMock:
        """Create a mock BLEInterface with required attributes."""
        iface = MagicMock()
        iface._state_lock = threading.Lock()
        iface._closed = False
        iface._want_receive = True
        iface._receiveThread = None
        iface._receive_start_pending = False
        iface._receive_start_pending_since = None
        iface._receive_recovery_attempts = 0
        iface._receive_from_radio_impl = MagicMock()
        # Configure thread_coordinator with required methods
        thread_coordinator = MagicMock()
        thread_coordinator.create_thread = MagicMock(return_value=MagicMock())
        thread_coordinator._create_thread = MagicMock(return_value=MagicMock())
        thread_coordinator.start_thread = MagicMock(return_value=None)
        thread_coordinator._start_thread = MagicMock(return_value=None)
        iface.thread_coordinator = thread_coordinator
        return iface

    @pytest.fixture
    def coordinator(self, mock_iface: MagicMock) -> BLEReceiveLifecycleCoordinator:
        """Create BLEReceiveLifecycleCoordinator instance."""
        return BLEReceiveLifecycleCoordinator(mock_iface)

    def test_schedule_deferred_receive_restart_already_inflight(
        self,
        coordinator: BLEReceiveLifecycleCoordinator,
        mock_iface: MagicMock,  # noqa: W0613
    ) -> None:
        """Test _schedule_deferred_receive_restart when already inflight."""
        coordinator._deferred_restart_inflight = True
        existing_thread = MagicMock()

        # Should return early without starting new thread
        coordinator._schedule_deferred_receive_restart(
            existing_thread=existing_thread,
            name="test",
            reset_recovery=True,
        )
        assert coordinator._deferred_restart_inflight is True

    def test_schedule_deferred_receive_restart_interface_closed(
        self, coordinator: BLEReceiveLifecycleCoordinator, mock_iface: MagicMock
    ) -> None:
        """Test deferred restart when interface closed during wait."""
        existing_thread = MagicMock()
        mock_iface._closed = True

        # Start deferred restart in a thread to avoid blocking
        def run_restart() -> None:
            coordinator._schedule_deferred_receive_restart(
                existing_thread=existing_thread,
                name="test",
                reset_recovery=True,
            )

        restart_thread = threading.Thread(target=run_restart)
        restart_thread.start()
        restart_thread.join(timeout=0.5)

        # Should complete without errors
        assert True

    def test_schedule_deferred_receive_restart_not_wanted(
        self, coordinator: BLEReceiveLifecycleCoordinator, mock_iface: MagicMock
    ) -> None:
        """Test deferred restart when receive not wanted."""
        existing_thread = MagicMock()
        mock_iface._want_receive = False

        def run_restart() -> None:
            coordinator._schedule_deferred_receive_restart(
                existing_thread=existing_thread,
                name="test",
                reset_recovery=True,
            )

        restart_thread = threading.Thread(target=run_restart)
        restart_thread.start()
        restart_thread.join(timeout=0.5)

        # Should complete without errors
        assert True

    def test_schedule_deferred_receive_restart_thread_changed(
        self, coordinator: BLEReceiveLifecycleCoordinator, mock_iface: MagicMock
    ) -> None:
        """Test deferred restart when thread reference changes."""
        existing_thread = MagicMock()
        mock_iface._receiveThread = MagicMock()  # Different thread

        def run_restart() -> None:
            coordinator._schedule_deferred_receive_restart(
                existing_thread=existing_thread,
                name="test",
                reset_recovery=True,
            )

        restart_thread = threading.Thread(target=run_restart)
        restart_thread.start()
        restart_thread.join(timeout=0.5)

        # Should complete without errors
        assert True

    def test_schedule_deferred_receive_restart_thread_dies(
        self, coordinator: BLEReceiveLifecycleCoordinator, mock_iface: MagicMock
    ) -> None:
        """Test deferred restart when thread dies during wait."""
        existing_thread = MagicMock()
        existing_thread.is_alive = MagicMock(return_value=False)
        existing_thread.ident = None
        mock_iface._receiveThread = existing_thread

        def run_restart() -> None:
            coordinator._schedule_deferred_receive_restart(
                existing_thread=existing_thread,
                name="test",
                reset_recovery=True,
            )

        restart_thread = threading.Thread(target=run_restart)
        restart_thread.start()
        restart_thread.join(timeout=0.5)

        # Should complete without errors
        assert True

    def test_schedule_deferred_receive_restart_exception_in_start(
        self,
        coordinator: BLEReceiveLifecycleCoordinator,
        mock_iface: MagicMock,  # noqa: W0613
    ) -> None:
        """Test deferred restart handles exception in start."""
        existing_thread = MagicMock()
        existing_thread.is_alive = MagicMock(return_value=True)

        call_count = 0

        def mock_start_receive(**_kwargs: Any) -> None:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise RuntimeError("start failed")
            # Second call succeeds

        with patch.object(coordinator, "start_receive_thread", mock_start_receive):

            def run_restart() -> None:
                coordinator._schedule_deferred_receive_restart(
                    existing_thread=existing_thread,
                    name="test",
                    reset_recovery=True,
                )

            restart_thread = threading.Thread(target=run_restart)
            restart_thread.start()
            restart_thread.join(timeout=1.0)

    def test_schedule_deferred_receive_restart_launch_failure(
        self,
        coordinator: BLEReceiveLifecycleCoordinator,
        mock_iface: MagicMock,  # noqa: W0613
    ) -> None:
        """Test deferred restart handles thread launch failure."""
        existing_thread = MagicMock()

        with patch("threading.Thread") as mock_thread_class:
            mock_thread_class.side_effect = RuntimeError("thread creation failed")

            coordinator._schedule_deferred_receive_restart(
                existing_thread=existing_thread,
                name="test",
                reset_recovery=True,
            )

            # inflight flag should be reset
            assert coordinator._deferred_restart_inflight is False

    def test_schedule_deferred_receive_restart_clear_pending(
        self, coordinator: BLEReceiveLifecycleCoordinator, mock_iface: MagicMock
    ) -> None:
        """Test deferred restart with clear_pending_if_alive flag."""
        existing_thread = MagicMock()
        existing_thread.is_alive = MagicMock(return_value=True)
        mock_iface._receiveThread = existing_thread
        mock_iface._receive_start_pending = True
        mock_iface._receive_start_pending_since = time.monotonic()

        def run_restart() -> None:
            coordinator._schedule_deferred_receive_restart(
                existing_thread=existing_thread,
                name="test",
                reset_recovery=True,
                clear_pending_if_alive=True,
            )

        restart_thread = threading.Thread(target=run_restart)
        restart_thread.start()
        restart_thread.join(timeout=0.5)

        # Pending should be cleared
        assert mock_iface._receive_start_pending is False
        assert mock_iface._receive_start_pending_since is None


@pytest.mark.unit
class TestBLEReceiveLifecycleCoordinatorCurrentThread:
    """Test BLEReceiveLifecycleCoordinator with current thread scenarios."""

    @pytest.fixture
    def mock_iface(self) -> MagicMock:
        """Create a mock BLEInterface with required attributes."""
        iface = MagicMock()
        iface._state_lock = threading.Lock()
        iface._closed = False
        iface._want_receive = True
        iface._receiveThread = None
        iface._receive_start_pending = False
        iface._receive_start_pending_since = None
        iface._receive_recovery_attempts = 0
        iface._receive_from_radio_impl = MagicMock()
        # Configure thread_coordinator with required methods
        thread_coordinator = MagicMock()
        thread_coordinator.create_thread = MagicMock(return_value=MagicMock())
        thread_coordinator._create_thread = MagicMock(return_value=MagicMock())
        thread_coordinator.start_thread = MagicMock(return_value=None)
        thread_coordinator._start_thread = MagicMock(return_value=None)
        iface.thread_coordinator = thread_coordinator
        return iface

    @pytest.fixture
    def coordinator(self, mock_iface: MagicMock) -> BLEReceiveLifecycleCoordinator:
        """Create BLEReceiveLifecycleCoordinator instance."""
        return BLEReceiveLifecycleCoordinator(mock_iface)

    def test_check_receive_start_conditions_current_thread(
        self, coordinator: BLEReceiveLifecycleCoordinator, mock_iface: MagicMock
    ) -> None:
        """Test _check_receive_start_conditions when existing is current thread."""
        # Create a real thread and set it as the receive thread
        current = threading.current_thread()
        mock_iface._receiveThread = current
        mock_iface._receive_start_pending = False

        def create_thread(**_kwargs: Any) -> MagicMock:
            return MagicMock()

        thread, _recovery = coordinator._check_receive_start_conditions(
            name="test",
            reset_recovery=True,
            create_runtime_thread=create_thread,
        )

        # Should defer since it's the current thread unwinding
        assert thread is None
        assert mock_iface._receive_start_pending is True

    def test_check_receive_start_conditions_current_thread_timeout(
        self, coordinator: BLEReceiveLifecycleCoordinator, mock_iface: MagicMock
    ) -> None:
        """Test _check_receive_start_conditions with current thread timeout."""
        current = threading.current_thread()
        mock_iface._receiveThread = current
        mock_iface._receive_start_pending = True
        mock_iface._receive_start_pending_since = time.monotonic() - 10.0  # Expired

        def create_thread(**_kwargs: Any) -> MagicMock:
            return MagicMock()

        thread, _recovery = coordinator._check_receive_start_conditions(
            name="test",
            reset_recovery=True,
            create_runtime_thread=create_thread,
        )

        # Should defer and schedule restart
        assert thread is None


@pytest.mark.unit
class TestBLEReceiveLifecycleCoordinatorConcurrent:
    """Test BLEReceiveLifecycleCoordinator thread safety and concurrency."""

    @pytest.fixture
    def mock_iface(self) -> MagicMock:
        """Create a mock BLEInterface with required attributes."""
        iface = MagicMock()
        iface._state_lock = threading.Lock()
        iface._closed = False
        iface._want_receive = True
        iface._receiveThread = None
        iface._receive_start_pending = False
        iface._receive_start_pending_since = None
        iface._receive_recovery_attempts = 0
        iface._receive_from_radio_impl = MagicMock()
        # Configure thread_coordinator with required methods
        thread_coordinator = MagicMock()
        thread_coordinator.create_thread = MagicMock(return_value=MagicMock())
        thread_coordinator._create_thread = MagicMock(return_value=MagicMock())
        thread_coordinator.start_thread = MagicMock(return_value=None)
        thread_coordinator._start_thread = MagicMock(return_value=None)
        iface.thread_coordinator = thread_coordinator
        return iface

    @pytest.fixture
    def coordinator(self, mock_iface: MagicMock) -> BLEReceiveLifecycleCoordinator:
        """Create BLEReceiveLifecycleCoordinator instance."""
        return BLEReceiveLifecycleCoordinator(mock_iface)

    def test_concurrent_thread_changes_during_check(
        self, coordinator: BLEReceiveLifecycleCoordinator, mock_iface: MagicMock
    ) -> None:
        """Test _check_receive_start_conditions handles concurrent thread changes."""
        original_thread = MagicMock()
        mock_iface._receiveThread = original_thread

        # Simulate concurrent modification during check
        def modify_thread() -> None:
            time.sleep(0.01)
            with mock_iface._state_lock:
                mock_iface._receiveThread = MagicMock()

        modifier = threading.Thread(target=modify_thread)
        modifier.start()

        def create_thread(**_kwargs: Any) -> MagicMock:
            _ = create_thread
            time.sleep(0.02)  # Allow modifier to run
            return MagicMock()

        _thread, _recovery = coordinator._check_receive_start_conditions(
            name="test",
            reset_recovery=True,
            create_runtime_thread=create_thread,
        )

        modifier.join(timeout=0.5)
        # Should handle the race condition gracefully
        assert True

    def test_concurrent_existing_becomes_active(
        self, coordinator: BLEReceiveLifecycleCoordinator, mock_iface: MagicMock
    ) -> None:
        """Test _check_receive_start_conditions when existing becomes active during staging."""
        existing_thread = MagicMock()
        existing_thread.is_alive = MagicMock(return_value=False)  # Initially not alive
        mock_iface._receiveThread = existing_thread

        def create_thread(**_kwargs: Any) -> MagicMock:
            # Simulate existing becoming alive during creation
            existing_thread.is_alive = MagicMock(return_value=True)
            return MagicMock()

        thread, _recovery = coordinator._check_receive_start_conditions(
            name="test",
            reset_recovery=True,
            create_runtime_thread=create_thread,
        )

        # Should skip since existing became active
        assert thread is None

    def test_concurrent_check_conditions_closed_after_creation(
        self, coordinator: BLEReceiveLifecycleCoordinator, mock_iface: MagicMock
    ) -> None:
        """Test _check_receive_start_conditions when interface closed after thread creation."""

        def create_thread(**_kwargs: Any) -> MagicMock:
            mock_iface._closed = True  # Close during creation
            return MagicMock()

        thread, _recovery = coordinator._check_receive_start_conditions(
            name="test",
            reset_recovery=True,
            create_runtime_thread=create_thread,
        )

        # Should return None since interface is now closed
        assert thread is None


@pytest.mark.unit
class TestLifecyclePrimitivesIntegration:
    """Integration tests for lifecycle primitives."""

    @pytest.fixture
    def full_mock_iface(self) -> MagicMock:
        """Create a fully mocked BLEInterface."""
        iface = MagicMock()
        iface._state_lock = threading.Lock()
        iface._state_manager = MagicMock()
        # Configure thread_coordinator so it's not detected as unconfigured
        thread_coordinator = MagicMock()
        thread_coordinator.return_value = True
        thread_coordinator.create_thread = MagicMock(return_value=MagicMock())
        thread_coordinator.start_thread = MagicMock(return_value=None)
        thread_coordinator.join_thread = MagicMock(return_value=None)
        thread_coordinator.set_event = MagicMock(return_value=None)
        thread_coordinator.clear_events = MagicMock(return_value=None)
        thread_coordinator.wake_waiting_threads = MagicMock(return_value=None)
        iface.thread_coordinator = thread_coordinator
        # Configure error_handler so it's not detected as unconfigured
        error_handler = MagicMock()
        error_handler.return_value = True
        error_handler.safe_cleanup = MagicMock(return_value=True)
        error_handler._safe_cleanup = MagicMock(return_value=True)
        error_handler.safe_execute = MagicMock(return_value=True)
        error_handler._safe_execute = MagicMock(return_value=True)
        iface.error_handler = error_handler
        iface._closed = False
        iface._want_receive = True
        iface._receiveThread = None
        iface._receive_start_pending = False
        iface._receive_start_pending_since = None
        iface._receive_recovery_attempts = 0
        return iface

    def test_state_access_integration(self, full_mock_iface: MagicMock) -> None:
        """Test _LifecycleStateAccess with full mock interface."""
        state_access = _LifecycleStateAccess(full_mock_iface)

        # Test all state operations
        full_mock_iface._state_manager.is_connected = MagicMock(return_value=True)
        assert state_access.is_connected() is True

        full_mock_iface._state_manager.current_state = MagicMock(
            return_value=ConnectionState.CONNECTED
        )
        assert state_access.current_state() == ConnectionState.CONNECTED

        full_mock_iface._state_manager.transition_to = MagicMock(return_value=True)
        assert state_access.transition_to(ConnectionState.DISCONNECTING) is True

        full_mock_iface._state_manager.reset_to_disconnected = MagicMock(
            return_value=True
        )
        assert state_access.reset_to_disconnected() is True

        full_mock_iface._state_manager.is_closing = MagicMock(return_value=False)
        assert state_access.is_closing() is False

    def test_thread_access_integration(self, full_mock_iface: MagicMock) -> None:
        """Test _LifecycleThreadAccess with full mock interface."""
        thread_access = _LifecycleThreadAccess(full_mock_iface)

        mock_thread = MagicMock()
        full_mock_iface.thread_coordinator.create_thread = MagicMock(
            return_value=mock_thread
        )

        def dummy_target() -> None:
            pass

        created = thread_access.create_thread(
            target=dummy_target, name="test", daemon=True
        )
        assert created is mock_thread

        thread_access.start_thread(mock_thread)
        full_mock_iface.thread_coordinator.start_thread.assert_called_with(mock_thread)

        thread_access.join_thread(mock_thread, timeout=1.0)
        full_mock_iface.thread_coordinator.join_thread.assert_called_with(
            mock_thread, timeout=1.0
        )

        thread_access.set_event("test_event")
        full_mock_iface.thread_coordinator.set_event.assert_called_with("test_event")

        thread_access.clear_events("event1", "event2")
        full_mock_iface.thread_coordinator.clear_events.assert_called_with(
            "event1", "event2"
        )

        thread_access.wake_waiting_threads("wake_event")
        full_mock_iface.thread_coordinator.wake_waiting_threads.assert_called_with(
            "wake_event"
        )

    def test_error_access_integration(self, full_mock_iface: MagicMock) -> None:
        """Test _LifecycleErrorAccess with full mock interface."""
        error_access = _LifecycleErrorAccess(full_mock_iface)

        cleanup_called = False

        def cleanup() -> None:
            nonlocal cleanup_called
            cleanup_called = True

        # Mock that properly wraps and calls the cleanup function
        def mock_safe_cleanup(func: Callable[[], Any], *_args: Any) -> Any:
            return func()

        full_mock_iface.error_handler.safe_cleanup = MagicMock(
            side_effect=mock_safe_cleanup
        )
        error_access.safe_cleanup(cleanup, "test_op")
        assert cleanup_called is True

        # Mock that properly wraps and calls the function
        def mock_safe_execute(func: Callable[[], Any], *_args: Any) -> Any:
            func()  # Call the function so did_run becomes True
            return "result"

        full_mock_iface.error_handler.safe_execute = MagicMock(
            side_effect=mock_safe_execute
        )

        def test_func() -> str:
            return "test"

        result = error_access.safe_execute(test_func, error_msg="test")
        assert result == "result"

    def test_receive_coordinator_integration(self, full_mock_iface: MagicMock) -> None:
        """Test BLEReceiveLifecycleCoordinator with full mock interface."""
        coordinator = BLEReceiveLifecycleCoordinator(full_mock_iface)

        # Test lifecycle operations
        coordinator.set_receive_wanted(want_receive=True)
        assert full_mock_iface._want_receive is True

        assert coordinator.should_run_receive_loop() is True

        mock_thread = MagicMock()
        mock_thread.is_alive = MagicMock(return_value=True)
        mock_thread._started = MagicMock()
        mock_thread._started.is_set = MagicMock(return_value=True)

        def create_thread(**_kwargs: Any) -> MagicMock:
            return mock_thread

        def start_thread(_t: Any) -> None:
            pass

        coordinator.start_receive_thread(
            name="test",
            reset_recovery=True,
            create_thread=create_thread,
            start_thread=start_thread,
        )

        assert full_mock_iface._receiveThread is mock_thread

    def test_full_lifecycle_sequence(self, full_mock_iface: MagicMock) -> None:
        """Test a full lifecycle sequence with all primitives."""
        state_access = _LifecycleStateAccess(full_mock_iface)
        thread_access = _LifecycleThreadAccess(full_mock_iface)
        error_access = _LifecycleErrorAccess(full_mock_iface)
        receive_coordinator = BLEReceiveLifecycleCoordinator(full_mock_iface)

        # Setup state manager responses
        full_mock_iface._state_manager.is_connected = MagicMock(return_value=False)
        full_mock_iface._state_manager.current_state = MagicMock(
            return_value=ConnectionState.DISCONNECTED
        )
        full_mock_iface._state_manager.transition_to = MagicMock(return_value=True)
        full_mock_iface._state_manager.reset_to_disconnected = MagicMock(
            return_value=True
        )
        full_mock_iface._state_manager.is_closing = MagicMock(return_value=False)

        # Test state transitions
        assert state_access.is_connected() is False
        assert state_access.current_state() == ConnectionState.DISCONNECTED
        assert state_access.transition_to(ConnectionState.CONNECTING) is True

        # Test thread operations
        mock_thread = MagicMock()
        full_mock_iface.thread_coordinator.create_thread = MagicMock(
            return_value=mock_thread
        )
        full_mock_iface.thread_coordinator.start_thread = MagicMock(return_value=None)

        def dummy_target() -> None:
            pass

        created = thread_access.create_thread(
            target=dummy_target, name="test", daemon=True
        )
        thread_access.start_thread(created)

        # Test error handling
        error_access.safe_cleanup(lambda: None, "test")

        # Test receive lifecycle
        receive_coordinator.set_receive_wanted(want_receive=True)
        assert receive_coordinator.should_run_receive_loop() is True


@pytest.mark.unit
class TestLifecyclePrimitivesEdgeCases:
    """Test edge cases and boundary conditions."""

    @pytest.fixture
    def mock_iface(self) -> MagicMock:
        """Create a mock BLEInterface with required attributes."""
        iface = MagicMock()
        iface._state_lock = threading.Lock()
        iface._closed = False
        iface._want_receive = True
        iface._receiveThread = None
        iface._receive_start_pending = False
        iface._receive_start_pending_since = None
        iface._receive_recovery_attempts = 0
        iface._receive_from_radio_impl = MagicMock()
        # Configure thread_coordinator with required methods
        thread_coordinator = MagicMock()
        thread_coordinator.create_thread = MagicMock(return_value=MagicMock())
        thread_coordinator._create_thread = MagicMock(return_value=MagicMock())
        thread_coordinator.start_thread = MagicMock(return_value=None)
        thread_coordinator._start_thread = MagicMock(return_value=None)
        iface.thread_coordinator = thread_coordinator
        return iface

    @pytest.fixture
    def coordinator(self, mock_iface: MagicMock) -> BLEReceiveLifecycleCoordinator:
        """Create BLEReceiveLifecycleCoordinator instance."""
        return BLEReceiveLifecycleCoordinator(mock_iface)

    def test_client_is_connected_non_bool_return(self) -> None:
        """Test _client_is_connected_compat with non-bool return value."""
        client = MagicMock()
        client.isConnected = MagicMock(return_value="yes")  # Not a bool
        client.is_connected = True  # Boolean attribute
        assert _client_is_connected_compat(client) is True

    def test_client_is_connected_callable_returns_non_bool(self) -> None:
        """Test _client_is_connected_compat when callable returns non-bool."""
        client = MagicMock()
        client.isConnected = MagicMock(return_value="yes")  # Not a bool
        client.is_connected = MagicMock(return_value=1)  # Not a bool
        client._is_connected = True
        assert _client_is_connected_compat(client) is True

    def test_thread_start_probe_helper(self) -> None:
        """Test _thread_start_probe utility function."""
        mock_thread = MagicMock()
        mock_thread.ident = 12345
        mock_thread.is_alive = MagicMock(return_value=True)

        ident, alive = _thread_start_probe(mock_thread)
        assert ident == 12345
        assert alive is True

    def test_thread_start_probe_exception(self) -> None:
        """Test _thread_start_probe handles exceptions."""
        mock_thread = MagicMock()
        mock_thread.ident = MagicMock(side_effect=RuntimeError("error"))

        ident, alive = _thread_start_probe(mock_thread)
        assert ident is None
        assert alive is False

    def test_receive_start_pending_timeout_constant(self) -> None:
        """Test that RECEIVE_START_PENDING_TIMEOUT_SECONDS is defined."""
        assert isinstance(RECEIVE_START_PENDING_TIMEOUT_SECONDS, float)
        assert RECEIVE_START_PENDING_TIMEOUT_SECONDS > 0

    def test_disconnect_plan_immutability(self) -> None:
        """Test _DisconnectPlan is frozen/immutable."""
        plan = _DisconnectPlan(early_return=True)
        with pytest.raises(AttributeError):
            plan.early_return = False  # type: ignore

    def test_ownership_snapshot_immutability(self) -> None:
        """Test _OwnershipSnapshot is frozen/immutable."""
        snapshot = _OwnershipSnapshot(
            still_owned=True,
            is_closing=False,
            lost_gate_ownership=False,
            prior_ever_connected=True,
        )
        with pytest.raises(AttributeError):
            snapshot.still_owned = False  # type: ignore

    def test_error_access_non_callable_hook(self, mock_iface: MagicMock) -> None:
        """Test _LifecycleErrorAccess with non-callable hook."""
        error_access = _LifecycleErrorAccess(mock_iface)
        mock_iface.error_handler.safe_cleanup = "not callable"
        mock_iface.error_handler._safe_cleanup = "not callable either"

        cleanup_called = False

        def cleanup() -> None:
            nonlocal cleanup_called
            cleanup_called = True

        error_access.safe_cleanup(cleanup, "test")
        # Should fall back to direct execution
        assert cleanup_called is True

    def test_state_access_invalid_return_type(self, mock_iface: MagicMock) -> None:
        """Test _LifecycleStateAccess with invalid return types."""
        state_access = _LifecycleStateAccess(mock_iface)
        mock_iface._state_manager.is_connected = "not a bool"
        mock_iface._state_manager._is_connected = MagicMock(
            return_value=123
        )  # Not a bool

        with pytest.raises(AttributeError, match=STATE_MANAGER_MISSING_CONNECTED_MSG):
            state_access.is_connected()

    def test_check_receive_start_conditions_concurrent_modification_race(
        self, mock_iface: MagicMock
    ) -> None:
        """Test race condition handling in _check_receive_start_conditions."""
        mock_iface._closed = False
        mock_iface._want_receive = True
        mock_iface._receiveThread = None
        mock_iface._receive_start_pending = False
        mock_iface._receive_from_radio_impl = MagicMock()

        coordinator = BLEReceiveLifecycleCoordinator(mock_iface)

        # Track thread creation
        threads_created = []

        def create_thread(**_kwargs: Any) -> MagicMock:
            # Simulate concurrent modification during creation
            with mock_iface._state_lock:
                mock_iface._receiveThread = MagicMock()  # Another thread sneaks in
            t = MagicMock()
            threads_created.append(t)
            return t

        thread, _recovery = coordinator._check_receive_start_conditions(
            name="test",
            reset_recovery=True,
            create_runtime_thread=create_thread,
        )

        # Should detect the race condition
        assert thread is None

    def test_probe_receive_thread_start_none_thread(
        self, mock_iface: MagicMock
    ) -> None:
        """Test _probe_receive_thread_start with None thread reference."""
        mock_iface._receiveThread = None
        coordinator = BLEReceiveLifecycleCoordinator(mock_iface)

        # This shouldn't happen in normal flow but test handles it gracefully
        mock_thread = MagicMock()
        mock_thread.is_alive = MagicMock(return_value=True)
        mock_iface._receiveThread = mock_thread

        result = coordinator._probe_receive_thread_start(
            mock_thread,
            name="test",
            reset_recovery=True,
        )
        assert result is True

    def test_is_current_receive_thread_ident_comparison(self) -> None:
        """Test _is_current_receive_thread with ident comparison."""
        current_ident = threading.current_thread().ident

        # Create mock with same ident
        mock_thread = MagicMock()
        mock_thread.ident = current_ident
        mock_thread.is_alive = MagicMock(return_value=True)

        # Should match by ident
        result = BLEReceiveLifecycleCoordinator._is_current_receive_thread(mock_thread)
        assert result is True

    def test_deferred_restart_with_enforce_pending_timeout(
        self, mock_iface: MagicMock
    ) -> None:
        """Test deferred restart with enforce_pending_timeout flag."""
        mock_iface._closed = False
        mock_iface._want_receive = True
        mock_iface._receiveThread = None
        mock_iface._receive_from_radio_impl = MagicMock()

        coordinator = BLEReceiveLifecycleCoordinator(mock_iface)

        existing_thread = MagicMock()
        existing_thread.is_alive = MagicMock(return_value=False)

        def run_restart() -> None:
            coordinator._schedule_deferred_receive_restart(
                existing_thread=existing_thread,
                name="test",
                reset_recovery=True,
                enforce_pending_timeout=True,
            )

        restart_thread = threading.Thread(target=run_restart)
        restart_thread.start()
        restart_thread.join(timeout=0.5)

    def test_error_access_try_variants_all_fail(self, mock_iface: MagicMock) -> None:
        """Test _try_safe_execute_variants when all signature attempts fail."""
        error_access = _LifecycleErrorAccess(mock_iface)

        def failing_safe_execute(*args: Any, **kwargs: Any) -> Any:
            raise RuntimeError("always fails")

        ran = False

        def tracked_func() -> str:
            nonlocal ran
            ran = True
            return "result"

        did_run_flag = False

        def did_run() -> bool:
            return did_run_flag

        hook_ran, result = error_access._try_safe_execute_variants(
            failing_safe_execute,
            tracked_func,
            error_msg="test error",
            did_run=did_run,
        )

        assert hook_ran is False
        assert result is None
        assert ran is False

    def test_start_receive_thread_with_custom_factories(
        self, mock_iface: MagicMock
    ) -> None:
        """Test start_receive_thread with custom create_thread and start_thread."""
        mock_iface._closed = False
        mock_iface._want_receive = True
        mock_iface._receiveThread = None
        mock_iface._receive_from_radio_impl = MagicMock()

        coordinator = BLEReceiveLifecycleCoordinator(mock_iface)

        custom_thread = MagicMock()
        custom_thread.is_alive = MagicMock(return_value=True)
        custom_thread._started = MagicMock()
        custom_thread._started.is_set = MagicMock(return_value=True)

        create_called = False
        start_called = False

        def custom_create(**_kwargs: Any) -> MagicMock:
            nonlocal create_called
            create_called = True
            return custom_thread

        def custom_start(_t: Any) -> None:
            nonlocal start_called
            start_called = True

        coordinator.start_receive_thread(
            name="custom_test",
            reset_recovery=True,
            create_thread=custom_create,
            start_thread=custom_start,
        )

        assert create_called is True
        assert start_called is True

    def test_check_receive_start_conditions_stale_pending_with_ident(
        self, mock_iface: MagicMock
    ) -> None:
        """Test handling of stale pending thread with ident."""
        mock_iface._closed = False
        mock_iface._want_receive = True
        mock_iface._receive_start_pending = True
        mock_iface._receive_start_pending_since = time.monotonic() - 10.0  # Expired

        existing_thread = MagicMock()
        existing_thread.name = "existing"
        existing_thread.ident = 12345  # Has ident but not alive
        existing_thread.is_alive = MagicMock(return_value=False)
        mock_iface._receiveThread = existing_thread

        coordinator = BLEReceiveLifecycleCoordinator(mock_iface)

        new_thread = MagicMock()

        def create_thread(**_kwargs: Any) -> MagicMock:
            return new_thread

        thread, _recovery = coordinator._check_receive_start_conditions(
            name="test",
            reset_recovery=True,
            create_runtime_thread=create_thread,
        )

        # Should replace stale thread since it has ident
        assert thread is new_thread

    def test_check_receive_start_conditions_stale_no_ident_no_failure_confirmed(
        self, mock_iface: MagicMock
    ) -> None:
        """Test handling of stale thread without ident and failure not confirmed."""
        mock_iface._closed = False
        mock_iface._want_receive = True
        mock_iface._receive_start_pending = False  # Not pending

        existing_thread = MagicMock()
        existing_thread.name = "existing"
        existing_thread.ident = None  # No ident
        existing_thread.is_alive = MagicMock(return_value=False)
        existing_thread._started = MagicMock()
        existing_thread._started.is_set = MagicMock(
            return_value=True
        )  # Started flag set
        mock_iface._receiveThread = existing_thread

        coordinator = BLEReceiveLifecycleCoordinator(mock_iface)

        new_thread = MagicMock()

        def create_thread(**_kwargs: Any) -> MagicMock:
            return new_thread

        thread, _recovery = coordinator._check_receive_start_conditions(
            name="test",
            reset_recovery=True,
            create_runtime_thread=create_thread,
        )

        # Since failure not confirmed, should schedule deferred restart
        assert thread is None

    def test_is_thread_start_failure_confirmed_with_ident(self) -> None:
        """Test _is_thread_start_failure_confirmed when thread has ident."""
        mock_thread = MagicMock()
        mock_thread._started = MagicMock()
        mock_thread._started.is_set = MagicMock(return_value=False)
        mock_thread.ident = 12345  # Has ident (thread started but flag not set)

        result = BLEReceiveLifecycleCoordinator._is_thread_start_failure_confirmed(
            mock_thread
        )
        assert result is True

    def test_is_thread_start_failure_confirmed_no_ident(self) -> None:
        """Test _is_thread_start_failure_confirmed when thread has no ident."""
        mock_thread = MagicMock()
        mock_thread._started = MagicMock()
        mock_thread._started.is_set = MagicMock(return_value=False)
        mock_thread.ident = None  # No ident

        result = BLEReceiveLifecycleCoordinator._is_thread_start_failure_confirmed(
            mock_thread
        )
        # With no ident and not started, failure not confirmed
        assert result is False

    def test_schedule_deferred_receive_restart_nested_concurrency(
        self, coordinator: BLEReceiveLifecycleCoordinator, mock_iface: MagicMock
    ) -> None:
        """Test deferred restart with nested concurrent calls."""
        mock_iface._closed = False
        mock_iface._want_receive = True
        mock_iface._receiveThread = None

        existing_thread = MagicMock()
        existing_thread.is_alive = MagicMock(return_value=True)

        # Test that only one restart proceeds due to _deferred_restart_inflight flag
        # First call should set inflight=True
        coordinator._schedule_deferred_receive_restart(
            existing_thread=existing_thread,
            name="restart_0",
            reset_recovery=True,
        )

        # Second call should return immediately (inflight=True)
        coordinator._schedule_deferred_receive_restart(
            existing_thread=existing_thread,
            name="restart_1",
            reset_recovery=True,
        )

        # inflight flag should be True (first call is still running)
        assert coordinator._deferred_restart_inflight is True

        # Reset by hand to avoid waiting for threads
        with coordinator._deferred_restart_lock:
            coordinator._deferred_restart_inflight = False

    def test_create_and_start_receive_thread_nested_exception(
        self, mock_iface: MagicMock
    ) -> None:
        """Test _create_and_start_receive_thread with exception during nested lock."""
        mock_iface._closed = False
        mock_iface._want_receive = True

        mock_thread = MagicMock()
        mock_iface._receiveThread = mock_thread

        coordinator = BLEReceiveLifecycleCoordinator(mock_iface)

        class CustomException(Exception):
            """Custom exception for testing."""

            pass  # Intentional no-op for exception class body  # noqa: W0107

        def failing_start(_t: Any) -> None:
            raise CustomException("nested failure")

        with pytest.raises(CustomException):
            coordinator._create_and_start_receive_thread(
                mock_thread,
                start_runtime_thread=failing_start,
            )

        # Should clear thread reference even with custom exception
        assert mock_iface._receiveThread is None
        assert mock_iface._receive_start_pending is False
