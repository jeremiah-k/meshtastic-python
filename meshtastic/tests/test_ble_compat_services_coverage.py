"""Test coverage for BLE compatibility services.

Covers management_compat_service.py and receive_compat_service.py with focus on:
- Management service compatibility shims
- Receive service compatibility shims
- Deprecated API paths
- Compatibility wrapper error handling
- Service initialization edge cases
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from typing import Any
from unittest.mock import MagicMock, Mock, NonCallableMock, patch

import pytest

from meshtastic.interfaces.ble.client import BLEClient
from meshtastic.interfaces.ble.management_compat_service import (
    BLEManagementCommandsService,
)
from meshtastic.interfaces.ble.management_runtime import (
    BLEManagementCommandHandler,
    _ManagementStartContext,
)
from meshtastic.interfaces.ble.receive_compat_service import BLEReceiveRecoveryService
from meshtastic.interfaces.ble.receive_service import BLEReceiveRecoveryController
from meshtastic.interfaces.ble.utils import (
    _is_unconfigured_mock_callable,
    _is_unconfigured_mock_member,
)

pytestmark = pytest.mark.unit

# pylint: disable=attribute-defined-outside-init


def configured_mock(return_value: Any = None, side_effect: Any = None) -> MagicMock:
    """Create a configured MagicMock that won't be detected as unconfigured."""
    mock = MagicMock()
    if side_effect is not None:
        mock.side_effect = side_effect
    else:
        mock.return_value = return_value
    return mock


def unconfigured_mock() -> Mock:
    """Create an unconfigured mock for testing detection."""
    return Mock()


def create_configured_handler_mock():
    """Create a properly configured handler mock that passes _is_handler_like check."""

    # Use a simple class instance instead of MagicMock to avoid mock detection issues
    # pylint: disable=too-many-instance-attributes
    class HandlerLike:
        """Mock handler class for testing management service compatibility."""

        # Empty class body - attributes attached dynamically

    handler = HandlerLike()
    # Attach configured mocks as methods
    handler.resolve_target_address_for_management = configured_mock()
    handler.management_target_gate = configured_mock()
    handler.get_management_client_if_available = configured_mock()
    handler.get_management_client_for_target = configured_mock()
    handler.get_current_implicit_management_binding_locked = configured_mock()
    handler.get_current_implicit_management_address_locked = configured_mock()
    handler.revalidate_implicit_management_target = configured_mock()
    handler.start_management_phase = configured_mock()
    handler.resolve_management_target = configured_mock()
    handler.acquire_client_for_target = configured_mock()
    handler.execute_with_client = configured_mock()
    handler.execute_management_command = configured_mock()
    handler.begin_management_operation_locked = configured_mock()
    handler.finish_management_operation = configured_mock()
    handler.validate_management_await_timeout = configured_mock()
    handler.validate_trust_timeout = configured_mock()
    handler.validate_connect_timeout_override = configured_mock()
    handler.pair = configured_mock()
    handler.unpair = configured_mock()
    handler.run_bluetoothctl_trust_command = configured_mock()
    handler.trust = configured_mock()
    return handler


def create_configured_controller_mock():
    """Create a properly configured controller mock that passes _is_controller_like check."""

    class ControllerLike:
        """Mock controller class for testing receive service compatibility."""

        # Empty class body - attributes attached dynamically

    controller = ControllerLike()
    controller.handle_read_loop_disconnect = configured_mock()
    controller._wait_for_read_trigger = configured_mock()
    controller._snapshot_client_state = configured_mock()
    controller._process_client_state = configured_mock()
    controller._reset_recovery_after_stability = configured_mock()
    controller._read_and_handle_payload = configured_mock()
    controller._handle_payload_read = configured_mock()
    controller._run_receive_cycle = configured_mock()
    controller.receive_from_radio_impl = configured_mock()
    controller.recover_receive_thread = configured_mock()
    controller.read_from_radio_with_retries = configured_mock()
    controller.handle_transient_read_error = configured_mock()
    controller.log_empty_read_warning = configured_mock()
    controller._should_run_receive_loop = configured_mock()
    controller._set_receive_wanted = configured_mock()
    controller._handle_disconnect = configured_mock()
    controller._start_receive_thread = configured_mock()
    return controller


# =============================================================================
# BLEManagementCommandsService Tests
# =============================================================================


class TestBLEManagementCommandsServiceHandlerDetection:
    """Test handler detection methods for management compatibility service."""

    def test_has_required_handler_entrypoint_with_expected_method(self):
        """Test checking for a specific expected method on handler."""
        handler = create_configured_handler_mock()

        result = BLEManagementCommandsService._has_required_handler_entrypoint(
            handler, expected_method="execute_management_command"
        )
        assert result is True

    def test_has_required_handler_entrypoint_unconfigured_mock(self):
        """Test that unconfigured mock handler returns False."""
        mock_handler = unconfigured_mock()

        result = BLEManagementCommandsService._has_required_handler_entrypoint(
            mock_handler, expected_method="execute_management_command"
        )
        assert result is False

    def test_has_required_handler_entrypoint_any_method(self):
        """Test checking for any required entrypoint method."""
        handler = create_configured_handler_mock()

        result = BLEManagementCommandsService._has_required_handler_entrypoint(handler)
        assert result is True

    def test_has_required_handler_entrypoint_no_methods(self):
        """Test handler with no required methods returns False."""

        class EmptyHandler:
            """Empty handler for testing missing methods detection."""

            pass

        handler = EmptyHandler()

        result = BLEManagementCommandsService._has_required_handler_entrypoint(handler)
        assert result is False

    def test_is_handler_like_full_api(self):
        """Test checking for full handler API surface."""
        handler = create_configured_handler_mock()
        result = BLEManagementCommandsService._is_handler_like(handler)
        assert result is True

    def test_is_handler_like_missing_method(self):
        """Test handler missing one method returns False."""

        class PartialHandler:
            pass

        handler = PartialHandler()
        handler.pair = configured_mock()
        handler.unpair = configured_mock()
        # Missing other required methods

        result = BLEManagementCommandsService._is_handler_like(handler)
        assert result is False

    def test_is_handler_like_unconfigured_mock(self):
        """Test unconfigured mock handler returns False."""
        mock_handler = unconfigured_mock()

        result = BLEManagementCommandsService._is_handler_like(mock_handler)
        assert result is False


class TestBLEManagementCommandsServiceHandlerResolution:
    """Test handler resolution for management compatibility service."""

    def test_handler_for_shim_iface_owned_handler(self):
        """Test using iface-owned handler when available."""
        iface = MagicMock()
        handler = create_configured_handler_mock()
        iface._get_management_command_handler = configured_mock(return_value=handler)

        result = BLEManagementCommandsService._handler_for_shim(iface)
        assert result is handler

    def test_handler_for_shim_direct_handler_field(self):
        """Test fallback to direct handler field."""
        iface = MagicMock()
        handler = create_configured_handler_mock()
        # _get_management_command_handler is unconfigured mock (will be skipped)
        iface._get_management_command_handler = unconfigured_mock()
        iface._management_command_handler = handler

        result = BLEManagementCommandsService._handler_for_shim(iface)
        assert result is handler

    def test_handler_for_shim_create_new_handler(self):
        """Test creating new handler when no iface-owned handler exists."""
        iface = MagicMock()
        iface._get_management_command_handler = unconfigured_mock()
        iface._management_command_handler = unconfigured_mock()
        iface._connected_elsewhere_late_bound = None

        result = BLEManagementCommandsService._handler_for_shim(iface)
        assert isinstance(result, BLEManagementCommandHandler)

    def test_handler_for_shim_with_custom_factory(self):
        """Test using custom ble_client_factory."""
        iface = MagicMock()
        custom_factory = MagicMock(spec=Callable)

        result = BLEManagementCommandsService._handler_for_shim(
            iface, ble_client_factory=custom_factory
        )
        assert isinstance(result, BLEManagementCommandHandler)

    def test_handler_for_shim_getter_raises_exception(self):
        """Test fallback when _get_management_command_handler raises exception."""
        iface = MagicMock()
        iface._get_management_command_handler = configured_mock(
            side_effect=AttributeError("test error")
        )
        handler = create_configured_handler_mock()
        iface._management_command_handler = handler

        result = BLEManagementCommandsService._handler_for_shim(iface)
        assert result is handler


class TestBLEManagementCommandsServiceCompatibilityAliases:
    """Test deprecated API compatibility aliases."""

    def test_resolve_handler_alias(self):
        """Test _resolve_handler is an alias for _handler_for_shim."""
        iface = MagicMock()
        handler = create_configured_handler_mock()
        iface._management_command_handler = handler

        result = BLEManagementCommandsService._resolve_handler(iface)
        assert result is handler

    def test_make_handler_alias(self):
        """Test _make_handler is an alias for _handler_for_shim."""
        iface = MagicMock()
        handler = create_configured_handler_mock()
        iface._management_command_handler = handler

        result = BLEManagementCommandsService._make_handler(iface)
        assert result is handler


class TestBLEManagementCommandsServiceOperations:
    """Test management command operations through compatibility service."""

    def test_start_management_phase(self):
        """Test starting management phase through compatibility service."""
        iface = MagicMock()
        handler = create_configured_handler_mock()
        expected_context = _ManagementStartContext(
            expected_implicit_binding=None,
            target_address="AA:BB:CC:DD:EE:FF",
            use_existing_client_without_resolved_address=False,
        )
        handler.start_management_phase = configured_mock(return_value=expected_context)
        iface._management_command_handler = handler

        result = BLEManagementCommandsService._start_management_phase(
            iface, "AA:BB:CC:DD:EE:FF"
        )
        assert result is expected_context
        handler.start_management_phase.assert_called_once_with("AA:BB:CC:DD:EE:FF")

    def test_resolve_management_target(self):
        """Test resolving management target through compatibility service."""
        iface = MagicMock()
        handler = create_configured_handler_mock()
        expected_result = ("AA:BB:CC:DD:EE:FF", None)
        handler.resolve_management_target = configured_mock(
            return_value=expected_result
        )
        iface._management_command_handler = handler

        context = _ManagementStartContext(
            expected_implicit_binding=None,
            target_address=None,
            use_existing_client_without_resolved_address=False,
        )

        result = BLEManagementCommandsService._resolve_management_target(
            iface, "AA:BB:CC:DD:EE:FF", context
        )
        assert result == expected_result

    def test_acquire_client_for_target(self):
        """Test acquiring client for target through compatibility service."""
        iface = MagicMock()
        iface._management_inflight = 0
        iface.address = "AA:BB:CC:DD:EE:FF"
        iface.client = None
        iface.BLEError = RuntimeError
        iface._validate_management_preconditions = MagicMock()
        mock_client = MagicMock(spec=BLEClient)
        expected_result = (mock_client, None)
        iface._get_management_client_for_target = configured_mock(
            return_value=mock_client
        )

        result = BLEManagementCommandsService._acquire_client_for_target(
            iface,
            address=None,
            target_address="AA:BB:CC:DD:EE:FF",
            expected_implicit_binding=None,
            connected_elsewhere=lambda key, owner=None: False,
            ble_client_factory=lambda addr: mock_client,
        )

        assert result == expected_result

    def test_execute_with_client(self):
        """Test executing command with client through compatibility service."""
        iface = MagicMock()
        handler = create_configured_handler_mock()
        mock_client = MagicMock(spec=BLEClient)
        expected_result = b"test_data"
        handler.execute_with_client = configured_mock(return_value=expected_result)
        iface._management_command_handler = handler

        def test_command(_client):
            return expected_result

        result = BLEManagementCommandsService._execute_with_client(
            iface,
            client_to_use=mock_client,
            temporary_client=None,
            command=test_command,
        )
        assert result == expected_result

    def test_execute_management_command(self):
        """Test executing management command through compatibility service."""
        iface = MagicMock()
        iface._management_inflight = 0
        handler = create_configured_handler_mock()
        mock_client = MagicMock(spec=BLEClient)
        expected_result = b"response"
        handler.execute_management_command = configured_mock(
            return_value=expected_result
        )
        iface._management_command_handler = handler

        def test_command(_client):
            return expected_result

        def client_factory(_addr):
            return mock_client

        def connected_elsewhere(_key, _owner=None):
            return False

        result = BLEManagementCommandsService._execute_management_command(
            iface,
            "AA:BB:CC:DD:EE:FF",
            test_command,
            ble_client_factory=client_factory,
            connected_elsewhere=connected_elsewhere,
        )
        assert result == expected_result


class TestBLEManagementCommandsServiceValidation:
    """Test validation methods through compatibility service."""

    def test_validate_management_await_timeout(self):
        """Test validating await timeout through compatibility service."""
        iface = MagicMock()
        handler = create_configured_handler_mock()
        handler.validate_management_await_timeout = configured_mock(return_value=30.0)
        iface._management_command_handler = handler

        result = BLEManagementCommandsService._validate_management_await_timeout(
            iface, 30.0
        )
        assert result == 30.0

    def test_validate_trust_timeout(self):
        """Test validating trust timeout through compatibility service."""
        iface = MagicMock()
        handler = create_configured_handler_mock()
        handler.validate_trust_timeout = configured_mock(return_value=10.0)
        iface._management_command_handler = handler

        result = BLEManagementCommandsService._validate_trust_timeout(iface, 10.0)
        assert result == 10.0

    def test_validate_connect_timeout_override(self):
        """Test validating connect timeout override through compatibility service."""
        iface = MagicMock()
        handler = create_configured_handler_mock()
        handler.validate_connect_timeout_override = configured_mock()
        iface._management_command_handler = handler

        # Should not raise
        BLEManagementCommandsService._validate_connect_timeout_override(
            iface, 30.0, pair_on_connect=True
        )
        handler.validate_connect_timeout_override.assert_called_once_with(
            30.0, pair_on_connect=True
        )


class TestBLEManagementCommandsServicePairUnpairTrust:
    """Test pair, unpair, and trust operations through compatibility service."""

    def test_pair(self):
        """Test pair operation through compatibility service."""
        iface = MagicMock()
        handler = create_configured_handler_mock()
        handler.pair = configured_mock()
        iface._management_command_handler = handler

        BLEManagementCommandsService.pair(
            iface, "AA:BB:CC:DD:EE:FF", await_timeout=30.0, kwargs={}
        )
        handler.pair.assert_called_once_with(
            "AA:BB:CC:DD:EE:FF", await_timeout=30.0, kwargs={}
        )

    def test_unpair(self):
        """Test unpair operation through compatibility service."""
        iface = MagicMock()
        handler = create_configured_handler_mock()
        handler.unpair = configured_mock()
        iface._management_command_handler = handler

        BLEManagementCommandsService.unpair(
            iface, "AA:BB:CC:DD:EE:FF", await_timeout=30.0
        )
        handler.unpair.assert_called_once_with("AA:BB:CC:DD:EE:FF", await_timeout=30.0)

    def test_trust(self):
        """Test trust operation through compatibility service."""
        iface = MagicMock()
        handler = create_configured_handler_mock()
        handler.trust = configured_mock()
        iface._management_command_handler = handler

        BLEManagementCommandsService.trust(iface, "AA:BB:CC:DD:EE:FF", timeout=10.0)
        handler.trust.assert_called_once()

    def test_run_bluetoothctl_trust_command(self):
        """Test running bluetoothctl trust command through compatibility service."""
        iface = MagicMock()
        handler = create_configured_handler_mock()
        handler.run_bluetoothctl_trust_command = configured_mock()
        iface._management_command_handler = handler

        BLEManagementCommandsService._run_bluetoothctl_trust_command(
            iface,
            "/usr/bin/bluetoothctl",
            "AA:BB:CC:DD:EE:FF",
            10.0,
            subprocess_module=MagicMock(),
            trust_hex_blob_re=re.compile(r"\b[0-9A-Fa-f]{16,}\b"),
            trust_token_re=re.compile(r"\b[A-Za-z0-9+/=_-]{40,}\b"),
            trust_command_output_max_chars=200,
        )
        handler.run_bluetoothctl_trust_command.assert_called_once()


class TestBLEManagementCommandsServiceEdgeCases:
    """Test edge cases and error handling for management compatibility service."""

    def test_handler_for_shim_iface_owned_partial_handler(self):
        """Test using iface-owned partial handler with expected method."""
        iface = MagicMock()

        # Create a partial handler that only has one method
        class PartialHandler:
            """Partial handler with single method for expected method testing."""

            pass

        partial_handler = PartialHandler()
        partial_handler.pair = configured_mock()
        iface._get_management_command_handler = configured_mock(
            return_value=partial_handler
        )

        # When looking for "pair", should find the partial handler
        result = BLEManagementCommandsService._handler_for_shim(
            iface, expected_method="pair"
        )
        assert result is partial_handler

    def test_handler_for_shim_with_connected_elsewhere_override(self):
        """Test using custom connected_elsewhere function."""
        iface = MagicMock()
        custom_checker = MagicMock(return_value=False)

        result = BLEManagementCommandsService._handler_for_shim(
            iface, connected_elsewhere=custom_checker
        )
        assert isinstance(result, BLEManagementCommandHandler)


# =============================================================================
# BLEReceiveRecoveryService Tests
# =============================================================================


class TestBLEReceiveRecoveryServiceControllerDetection:
    """Test controller detection methods for receive compatibility service."""

    def test_controller_class_resolution(self):
        """Test controller class resolution."""
        result = BLEReceiveRecoveryService._controller_class()
        assert result is BLEReceiveRecoveryController

    def test_is_controller_like_with_required_method(self):
        """Test checking for a specific required method."""
        controller = create_configured_controller_mock()

        result = BLEReceiveRecoveryService._is_controller_like(
            controller, required_method="handle_read_loop_disconnect"
        )
        assert result is True

    def test_is_controller_like_unconfigured_mock(self):
        """Test unconfigured mock controller returns False."""
        mock_controller = unconfigured_mock()

        result = BLEReceiveRecoveryService._is_controller_like(mock_controller)
        assert result is False

    def test_is_controller_like_full_api(self):
        """Test checking for full controller API surface."""
        controller = create_configured_controller_mock()

        result = BLEReceiveRecoveryService._is_controller_like(controller)
        assert result is True

    def test_is_controller_like_missing_method(self):
        """Test controller missing one method returns False."""

        class PartialController:
            """Partial controller with limited methods for testing."""

            pass

        controller = PartialController()
        controller.handle_read_loop_disconnect = configured_mock()
        # Missing other required methods

        result = BLEReceiveRecoveryService._is_controller_like(controller)
        assert result is False

    def test_resolve_controller_candidate_direct_instance(self):
        """Test resolving direct controller instance."""
        controller = create_configured_controller_mock()

        result = BLEReceiveRecoveryService._resolve_controller_candidate(
            controller, iface=MagicMock()
        )
        # Returns the controller if it's controller-like
        assert result is not None

    def test_resolve_controller_candidate_from_factory(self):
        """Test resolving controller from factory callable."""
        controller = create_configured_controller_mock()

        # Factory that works with or without iface argument
        def factory(iface=None):
            return controller

        mock_iface = MagicMock()
        result = BLEReceiveRecoveryService._resolve_controller_candidate(
            factory, iface=mock_iface
        )
        assert result is controller

    def test_resolve_controller_candidate_none(self):
        """Test resolving None returns None."""
        result = BLEReceiveRecoveryService._resolve_controller_candidate(None)
        assert result is None

    def test_resolve_controller_candidate_unconfigured_mock(self):
        """Test resolving unconfigured mock returns None."""
        mock_controller = unconfigured_mock()

        result = BLEReceiveRecoveryService._resolve_controller_candidate(
            mock_controller
        )
        assert result is None


class TestBLEReceiveRecoveryServiceControllerForShim:
    """Test controller resolution for receive compatibility service."""

    def test_controller_for_shim_getter_method(self):
        """Test using _get_receive_recovery_controller getter method."""
        iface = MagicMock()
        controller = create_configured_controller_mock()

        iface._get_receive_recovery_controller = configured_mock(
            return_value=controller
        )

        result = BLEReceiveRecoveryService._controller_for_shim(iface)
        assert result is not None

    def test_controller_for_shim_cached_controller(self):
        """Test using cached controller from iface."""
        iface = MagicMock()
        controller = create_configured_controller_mock()

        iface._get_receive_recovery_controller = unconfigured_mock()
        iface._receive_recovery_controller = controller

        result = BLEReceiveRecoveryService._controller_for_shim(iface)
        assert result is not None

    def test_controller_for_shim_create_new_controller(self):
        """Test creating new controller when no cached controller exists."""
        iface = MagicMock()
        iface._get_receive_recovery_controller = unconfigured_mock()
        iface._receive_recovery_controller = unconfigured_mock()
        iface._state_lock = None

        result = BLEReceiveRecoveryService._controller_for_shim(iface)
        assert isinstance(result, BLEReceiveRecoveryController)

    def test_controller_for_shim_getter_raises_exception(self):
        """Test fallback when getter raises exception."""
        iface = MagicMock()
        controller = create_configured_controller_mock()

        iface._get_receive_recovery_controller = configured_mock(
            side_effect=AttributeError("test error")
        )
        iface._receive_recovery_controller = controller

        result = BLEReceiveRecoveryService._controller_for_shim(iface)
        assert result is not None

    def test_controller_for_shim_with_state_lock(self):
        """Test controller creation with state lock."""
        iface = MagicMock()
        iface._get_receive_recovery_controller = unconfigured_mock()
        iface._receive_recovery_controller = unconfigured_mock()
        mock_lock = MagicMock()
        mock_lock.__enter__ = MagicMock(return_value=None)
        mock_lock.__exit__ = MagicMock(return_value=None)
        iface._state_lock = mock_lock

        result = BLEReceiveRecoveryService._controller_for_shim(iface)
        assert isinstance(result, BLEReceiveRecoveryController)


class TestBLEReceiveRecoveryServiceOperations:
    """Test receive/recovery operations through compatibility service."""

    def test_handle_read_loop_disconnect(self):
        """Test handling read loop disconnect through compatibility service."""
        iface = MagicMock()
        controller = create_configured_controller_mock()
        controller.handle_read_loop_disconnect = configured_mock(return_value=True)
        iface._receive_recovery_controller = controller
        iface._get_receive_recovery_controller = unconfigured_mock()

        mock_client = MagicMock(spec=BLEClient)
        result = BLEReceiveRecoveryService._handle_read_loop_disconnect(
            iface, "test disconnect", mock_client
        )
        assert result is True

    def test_should_poll_without_notify_true(self):
        """Test should poll without notify returns True when notify disabled."""
        iface = MagicMock()
        iface._fromnum_notify_enabled = False
        mock_lock = MagicMock()
        mock_lock.__enter__ = MagicMock(return_value=None)
        mock_lock.__exit__ = MagicMock(return_value=None)
        iface._state_lock = mock_lock

        result = BLEReceiveRecoveryService._should_poll_without_notify(iface)
        assert result is True

    def test_should_poll_without_notify_false(self):
        """Test should poll without notify returns False when notify enabled."""
        iface = MagicMock()
        iface._fromnum_notify_enabled = True
        mock_lock = MagicMock()
        mock_lock.__enter__ = MagicMock(return_value=None)
        mock_lock.__exit__ = MagicMock(return_value=None)
        iface._state_lock = mock_lock

        result = BLEReceiveRecoveryService._should_poll_without_notify(iface)
        assert result is False

    def test_wait_for_read_trigger(self):
        """Test waiting for read trigger through compatibility service."""
        iface = MagicMock()
        controller = create_configured_controller_mock()
        controller._wait_for_read_trigger = configured_mock(return_value=(True, False))
        iface._receive_recovery_controller = controller
        iface._get_receive_recovery_controller = unconfigured_mock()

        coordinator = MagicMock()
        result = BLEReceiveRecoveryService._wait_for_read_trigger(
            iface, coordinator=coordinator, wait_timeout=1.0
        )
        assert result == (True, False)

    def test_snapshot_client_state(self):
        """Test snapshotting client state through compatibility service."""
        iface = MagicMock()
        controller = create_configured_controller_mock()
        expected_result = (None, False, False, False)
        controller._snapshot_client_state = configured_mock(
            return_value=expected_result
        )
        iface._receive_recovery_controller = controller
        iface._get_receive_recovery_controller = unconfigured_mock()

        result = BLEReceiveRecoveryService._snapshot_client_state(iface)
        assert result == expected_result

    def test_process_client_state(self):
        """Test processing client state through compatibility service."""
        iface = MagicMock()
        controller = create_configured_controller_mock()
        controller._process_client_state = configured_mock(return_value=False)
        iface._receive_recovery_controller = controller
        iface._get_receive_recovery_controller = unconfigured_mock()

        coordinator = MagicMock()
        mock_client = MagicMock(spec=BLEClient)
        result = BLEReceiveRecoveryService._process_client_state(
            iface,
            coordinator=coordinator,
            wait_timeout=1.0,
            client=mock_client,
            is_connecting=False,
            publish_pending=False,
            is_closing=False,
        )
        assert result is False

    def test_reset_recovery_after_stability(self):
        """Test resetting recovery after stability through compatibility service."""
        iface = MagicMock()
        controller = create_configured_controller_mock()
        controller._reset_recovery_after_stability = configured_mock()
        iface._receive_recovery_controller = controller
        iface._get_receive_recovery_controller = unconfigured_mock()

        # Should not raise
        BLEReceiveRecoveryService._reset_recovery_after_stability(iface)
        controller._reset_recovery_after_stability.assert_called_once()

    def test_read_and_handle_payload(self):
        """Test reading and handling payload through compatibility service."""
        iface = MagicMock()
        controller = create_configured_controller_mock()
        controller._read_and_handle_payload = configured_mock(return_value=True)
        iface._receive_recovery_controller = controller
        iface._get_receive_recovery_controller = unconfigured_mock()

        mock_client = MagicMock(spec=BLEClient)
        result = BLEReceiveRecoveryService._read_and_handle_payload(
            iface, mock_client, poll_without_notify=False
        )
        assert result is True

    def test_handle_payload_read(self):
        """Test handling payload read through compatibility service."""
        iface = MagicMock()
        controller = create_configured_controller_mock()
        controller._handle_payload_read = configured_mock(return_value=(False, False))
        iface._receive_recovery_controller = controller
        iface._get_receive_recovery_controller = unconfigured_mock()

        mock_client = MagicMock(spec=BLEClient)
        result = BLEReceiveRecoveryService._handle_payload_read(
            iface, mock_client, poll_without_notify=False
        )
        assert result == (False, False)

    def test_run_receive_cycle(self):
        """Test running receive cycle through compatibility service."""
        iface = MagicMock()
        controller = create_configured_controller_mock()
        controller._run_receive_cycle = configured_mock(return_value=True)
        iface._receive_recovery_controller = controller
        iface._get_receive_recovery_controller = unconfigured_mock()

        coordinator = MagicMock()
        result = BLEReceiveRecoveryService._run_receive_cycle(
            iface, coordinator=coordinator, wait_timeout=1.0
        )
        assert result is True

    def test_receive_from_radio_impl(self):
        """Test receive from radio impl through compatibility service."""
        iface = MagicMock()
        controller = create_configured_controller_mock()
        controller.receive_from_radio_impl = configured_mock()
        iface._receive_recovery_controller = controller
        iface._get_receive_recovery_controller = unconfigured_mock()

        # Should not raise
        BLEReceiveRecoveryService._receive_from_radio_impl(iface)
        controller.receive_from_radio_impl.assert_called_once()

    def test_recover_receive_thread(self):
        """Test recovering receive thread through compatibility service."""
        iface = MagicMock()
        controller = create_configured_controller_mock()
        controller.recover_receive_thread = configured_mock()
        iface._receive_recovery_controller = controller
        iface._get_receive_recovery_controller = unconfigured_mock()

        # Should not raise
        BLEReceiveRecoveryService._recover_receive_thread(iface, "test disconnect")
        controller.recover_receive_thread.assert_called_once_with("test disconnect")

    def test_read_from_radio_with_retries(self):
        """Test reading from radio with retries through compatibility service."""
        iface = MagicMock()
        controller = create_configured_controller_mock()
        expected_data = b"test_data"
        controller.read_from_radio_with_retries = configured_mock(
            return_value=expected_data
        )
        iface._receive_recovery_controller = controller
        iface._get_receive_recovery_controller = unconfigured_mock()

        mock_client = MagicMock(spec=BLEClient)
        result = BLEReceiveRecoveryService._read_from_radio_with_retries(
            iface, mock_client, retry_on_empty=True
        )
        assert result == expected_data

    def test_handle_transient_read_error(self):
        """Test handling transient read error through compatibility service."""
        iface = MagicMock()
        controller = create_configured_controller_mock()
        controller.handle_transient_read_error = configured_mock()
        iface._receive_recovery_controller = controller
        iface._get_receive_recovery_controller = unconfigured_mock()

        mock_error = MagicMock()
        # Should not raise
        BLEReceiveRecoveryService._handle_transient_read_error(iface, mock_error)
        controller.handle_transient_read_error.assert_called_once_with(mock_error)

    def test_log_empty_read_warning(self):
        """Test logging empty read warning through compatibility service."""
        iface = MagicMock()
        controller = create_configured_controller_mock()
        controller.log_empty_read_warning = configured_mock()
        iface._receive_recovery_controller = controller
        iface._get_receive_recovery_controller = unconfigured_mock()

        # Should not raise
        BLEReceiveRecoveryService._log_empty_read_warning(iface)
        controller.log_empty_read_warning.assert_called_once()


class TestBLEReceiveRecoveryControllerEmptyReadWarnings:
    """Test empty-read warning behavior for polling and notify-driven modes."""

    class _Iface:
        def __init__(self, *, fromnum_notify_enabled: bool) -> None:
            self._fromnum_notify_enabled = fromnum_notify_enabled
            self._last_empty_read_warning = 0.0
            self._suppressed_empty_read_warnings = 0

    def test_log_empty_read_warning_uses_debug_in_polling_mode(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Polling mode without FROMNUM notifications should avoid warning-level noise."""
        iface = self._Iface(fromnum_notify_enabled=False)
        controller = BLEReceiveRecoveryController(iface)

        with patch(
            "meshtastic.interfaces.ble.receive_service.time.monotonic",
            return_value=100.0,
        ):
            with caplog.at_level(logging.DEBUG):
                controller.log_empty_read_warning()

        assert "Exceeded max retries for empty BLE read" in caplog.text
        assert "polling mode without FROMNUM notifications" in caplog.text
        assert not any(record.levelno >= logging.WARNING for record in caplog.records)

    def test_log_empty_read_warning_uses_warning_with_fromnum_notify(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """When FROMNUM notifications are enabled, empty-read exhaustion remains warning-level."""
        iface = self._Iface(fromnum_notify_enabled=True)
        controller = BLEReceiveRecoveryController(iface)

        with patch(
            "meshtastic.interfaces.ble.receive_service.time.monotonic",
            return_value=100.0,
        ):
            with caplog.at_level(logging.WARNING):
                controller.log_empty_read_warning()

        assert "Exceeded max retries for empty BLE read" in caplog.text
        assert any(record.levelno == logging.WARNING for record in caplog.records)


class TestBLEReceiveRecoveryServiceCoordinatorOperations:
    """Test coordinator operations through receive compatibility service."""

    def test_coordinator_wait_for_event(self):
        """Test coordinator wait for event through compatibility service."""
        coordinator = MagicMock()

        # Mock the controller class method
        with patch.object(
            BLEReceiveRecoveryController,
            "_coordinator_wait_for_event",
            return_value=True,
        ) as mock_wait:
            result = BLEReceiveRecoveryService._coordinator_wait_for_event(
                coordinator, "test_event", timeout=1.0
            )
            assert result is True
            mock_wait.assert_called_once_with(coordinator, "test_event", timeout=1.0)

    def test_coordinator_check_and_clear_event(self):
        """Test coordinator check and clear event through compatibility service."""
        coordinator = MagicMock()

        with patch.object(
            BLEReceiveRecoveryController,
            "_coordinator_check_and_clear_event",
            return_value=True,
        ) as mock_check:
            result = BLEReceiveRecoveryService._coordinator_check_and_clear_event(
                coordinator, "test_event"
            )
            assert result is True
            mock_check.assert_called_once_with(coordinator, "test_event")

    def test_coordinator_clear_event(self):
        """Test coordinator clear event through compatibility service."""
        coordinator = MagicMock()

        with patch.object(
            BLEReceiveRecoveryController, "_coordinator_clear_event"
        ) as mock_clear:
            BLEReceiveRecoveryService._coordinator_clear_event(
                coordinator, "test_event"
            )
            mock_clear.assert_called_once_with(coordinator, "test_event")


class TestBLEReceiveRecoveryServiceEdgeCases:
    """Test edge cases and error handling for receive compatibility service."""

    def test_should_poll_without_notify_mock_value(self):
        """Test handling mock value for notify enabled flag."""
        iface = MagicMock()
        # Set to unconfigured mock (should be treated as False)
        iface._fromnum_notify_enabled = unconfigured_mock()
        mock_lock = MagicMock()
        mock_lock.__enter__ = MagicMock(return_value=None)
        mock_lock.__exit__ = MagicMock(return_value=None)
        iface._state_lock = mock_lock

        result = BLEReceiveRecoveryService._should_poll_without_notify(iface)
        assert result is True  # Should return True when notify is effectively disabled

    def test_resolve_controller_candidate_factory_raises(self):
        """Test handling factory that raises exception."""

        def failing_factory(iface):
            raise RuntimeError("test error")

        mock_iface = MagicMock()
        result = BLEReceiveRecoveryService._resolve_controller_candidate(
            failing_factory, iface=mock_iface
        )
        assert result is None

    def test_controller_for_shim_class_cannot_be_resolved(self):
        """Test when controller class resolution returns a class (not instance)."""

        # This tests the edge case where resolved is still a class after factory probe
        def class_factory(iface):
            # Return the class itself instead of instance
            return BLEReceiveRecoveryController

        mock_iface = MagicMock()
        result = BLEReceiveRecoveryService._resolve_controller_candidate(
            class_factory, iface=mock_iface
        )
        # Should return None when the result is still a class
        assert result is None

    def test_resolve_controller_candidate_with_required_method(self):
        """Test resolving controller with specific required method check."""
        controller = create_configured_controller_mock()

        result = BLEReceiveRecoveryService._resolve_controller_candidate(
            controller, required_method="handle_read_loop_disconnect"
        )
        assert result is not None


# =============================================================================
# Mock Utility Function Tests
# =============================================================================


class TestMockUtilityFunctions:
    """Test the mock utility functions used by compatibility services."""

    def test_is_unconfigured_mock_member_with_mock(self):
        """Test detecting unconfigured mock member."""
        mock = Mock()
        # Default mock without configuration should be detected
        result = _is_unconfigured_mock_member(mock)
        assert result is True

    def test_is_unconfigured_mock_member_configured(self):
        """Test configured mock is not detected as unconfigured."""
        mock = MagicMock()
        mock.return_value = "test"
        result = _is_unconfigured_mock_member(mock)
        assert result is False

    def test_is_unconfigured_mock_member_non_callable(self):
        """Test non-callable mock member detection."""
        mock = NonCallableMock()
        result = _is_unconfigured_mock_member(mock)
        assert result is True

    def test_is_unconfigured_mock_member_regular_object(self):
        """Test regular object is not detected as unconfigured mock."""
        regular_object = {"key": "value"}
        result = _is_unconfigured_mock_member(regular_object)
        assert result is False

    def test_is_unconfigured_mock_callable_with_callable(self):
        """Test detecting unconfigured callable mock."""
        mock = Mock()
        result = _is_unconfigured_mock_callable(mock)
        assert result is True

    def test_is_unconfigured_mock_callable_non_callable(self):
        """Test non-callable mock returns False."""
        mock = NonCallableMock()
        result = _is_unconfigured_mock_callable(mock)
        assert result is False

    def test_is_unconfigured_mock_callable_regular_callable(self):
        """Test regular callable is not detected as unconfigured mock."""

        def regular_function():
            pass

        result = _is_unconfigured_mock_callable(regular_function)
        assert result is False


# =============================================================================
# Integration Tests
# =============================================================================


class TestBLECompatServicesIntegration:
    """Integration tests combining both compatibility services."""

    def test_management_service_with_real_handler_pattern(self):
        """Test management service with a realistic handler-like object."""
        iface = MagicMock()

        # Create a realistic handler pattern
        class RealisticHandler:
            """Realistic handler mock with complete management API surface."""

            def pair(self, _address, _await_timeout, _kwargs):
                """Mock pair operation."""
                return None

            def unpair(self, _address, _await_timeout):
                """Mock unpair operation."""
                return None

            def trust(self, _address, **_kwargs):
                """Mock trust operation."""
                return None

            def start_management_phase(self, address):
                """Mock start management phase."""
                return _ManagementStartContext(None, address, False)

            def resolve_management_target(self, address, _context):
                """Mock resolve management target."""
                return (address, None)

            def acquire_client_for_target(self, **_kwargs):
                """Mock acquire client for target."""
                mock_client = MagicMock(spec=BLEClient)
                return (mock_client, None)

            def execute_with_client(self, **_kwargs):
                """Mock execute with client."""
                return b"result"

            def execute_management_command(self, _address, _command):
                """Mock execute management command."""
                return b"result"

            def validate_management_await_timeout(self, timeout):
                """Mock validate management await timeout."""
                return float(timeout)

            def validate_trust_timeout(self, timeout):
                """Mock validate trust timeout."""
                return float(timeout)

            def validate_connect_timeout_override(self, _timeout, _pair_on_connect):
                """Mock validate connect timeout override."""
                return None

            def run_bluetoothctl_trust_command(self, **_kwargs):
                """Mock run bluetoothctl trust command."""
                return None

            # Full API methods
            def resolve_target_address_for_management(self):
                """Mock resolve target address for management."""
                return None

            def management_target_gate(self):
                """Mock management target gate."""
                return None

            def get_management_client_if_available(self):
                """Mock get management client if available."""
                return None

            def get_management_client_for_target(self):
                """Mock get management client for target."""
                return None

            def get_current_implicit_management_binding_locked(self):
                """Mock get current implicit management binding locked."""
                return None

            def get_current_implicit_management_address_locked(self):
                """Mock get current implicit management address locked."""
                return None

            def revalidate_implicit_management_target(self):
                """Mock revalidate implicit management target."""
                return None

            def begin_management_operation_locked(self):
                """Mock begin management operation locked."""
                return None

            def finish_management_operation(self):
                """Mock finish management operation."""
                return None

        handler = RealisticHandler()
        iface._management_command_handler = handler

        # Test various operations
        result = BLEManagementCommandsService._start_management_phase(
            iface, "AA:BB:CC:DD:EE:FF"
        )
        assert result.target_address == "AA:BB:CC:DD:EE:FF"

    def test_receive_service_with_real_controller_pattern(self):
        """Test receive service with a realistic controller-like object."""
        iface = MagicMock()
        mock_lock = MagicMock()
        mock_lock.__enter__ = MagicMock(return_value=None)
        mock_lock.__exit__ = MagicMock(return_value=None)
        iface._state_lock = mock_lock

        # Create a realistic controller pattern
        class RealisticController:
            """Realistic controller mock with complete receive recovery API surface."""

            def handle_read_loop_disconnect(self, _error_message, _previous_client):
                """Mock handle read loop disconnect."""
                return True

            def _wait_for_read_trigger(self, **_kwargs):
                """Mock wait for read trigger."""
                return (True, False)

            def _snapshot_client_state(self):
                """Mock snapshot client state."""
                return (None, False, False, False)

            def _process_client_state(self, **_kwargs):
                """Mock process client state."""
                return False

            def _reset_recovery_after_stability(self):
                """Mock reset recovery after stability."""
                return None

            def _read_and_handle_payload(self, _client, _poll_without_notify):
                """Mock read and handle payload."""
                return True

            def _handle_payload_read(self, _client, _poll_without_notify):
                """Mock handle payload read."""
                return (False, False)

            def _run_receive_cycle(self, **_kwargs):
                """Mock run receive cycle."""
                return True

            def receive_from_radio_impl(self):
                """Mock receive from radio impl."""
                return None

            def recover_receive_thread(self, _disconnect_reason):
                """Mock recover receive thread."""
                return None

            def read_from_radio_with_retries(self, _client, _retry_on_empty):
                """Mock read from radio with retries."""
                return b"data"

            def handle_transient_read_error(self, _error):
                """Mock handle transient read error."""
                return None

            def log_empty_read_warning(self):
                """Mock log empty read warning."""
                return None

            def _should_run_receive_loop(self):
                """Mock check if should run receive loop."""
                return True

            def _set_receive_wanted(self, _value):
                """Mock set receive wanted."""
                return None

            def _handle_disconnect(self):
                """Mock handle disconnect."""
                return None

            def _start_receive_thread(self):
                """Mock start receive thread."""
                return None

        controller = RealisticController()
        iface._receive_recovery_controller = controller
        iface._get_receive_recovery_controller = unconfigured_mock()

        # Test various operations
        result = BLEReceiveRecoveryService._handle_read_loop_disconnect(
            iface, "test", MagicMock(spec=BLEClient)
        )
        assert result is True

    def test_both_services_with_none_iface(self):
        """Test behavior when iface is None or has no handlers."""
        iface = MagicMock()
        # No handlers configured, should create new ones

        # Management service should create new handler
        result = BLEManagementCommandsService._handler_for_shim(iface)
        assert isinstance(result, BLEManagementCommandHandler)

        # Receive service should create new controller
        iface._state_lock = None
        result = BLEReceiveRecoveryService._controller_for_shim(iface)
        assert isinstance(result, BLEReceiveRecoveryController)


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--faulthandler-timeout=30"])
