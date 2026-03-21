"""Meshtastic unit tests for node_runtime/response_runtime.py."""

# pylint: disable=redefined-outer-name

import logging
import time
from typing import Any, cast
from unittest.mock import MagicMock

import pytest

from ..node_runtime.response_runtime import (
    _NodeChannelResponseRuntime,
    _NodeMetadataResponseRuntime,
)
from ..protobuf import admin_pb2, channel_pb2, config_pb2, mesh_pb2, portnums_pb2
from ..util import Acknowledgment


@pytest.fixture
def mock_node() -> MagicMock:
    """Create a minimal mock Node for response runtime tests."""
    node = MagicMock(spec=["iface", "_signal_metadata_stdout_event", "_timeout"])
    node.iface = MagicMock()
    node.iface._acknowledgment = Acknowledgment()
    node._timeout = MagicMock()
    node._timeout.expireTime = time.time() + 300
    node._timeout.reset = MagicMock()
    return node


@pytest.fixture
def mock_node_for_channel() -> MagicMock:
    """Create a minimal mock Node for channel response runtime tests."""
    node = MagicMock(
        spec=[
            "iface",
            "_timeout",
            "_channels_lock",
            "partialChannels",
            "channels",
            "_request_channel",
            "_fixup_channels_locked",
        ]
    )
    node._timeout = MagicMock()
    node._timeout.expireTime = time.time() + 300
    node._timeout.reset = MagicMock()
    node._channels_lock = MagicMock()
    node._channels_lock.__enter__ = MagicMock(return_value=None)
    node._channels_lock.__exit__ = MagicMock(return_value=None)
    node.partialChannels = []
    node.channels = None
    node._request_channel = MagicMock()
    node._fixup_channels_locked = MagicMock()
    return node


class TestNodeMetadataResponseRuntime:
    """Tests for _NodeMetadataResponseRuntime."""

    @pytest.mark.unit
    def test_handle_metadata_response_missing_decoded_logs_warning(
        self, mock_node: MagicMock, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Handle metadata response with missing decoded should log warning and signal event."""
        runtime = _NodeMetadataResponseRuntime(mock_node)
        packet: dict[str, Any] = {"decoded": None}

        with caplog.at_level(logging.WARNING):
            runtime.handle_metadata_response(packet)

        assert "Received malformed metadata response (missing decoded)" in caplog.text
        mock_node._signal_metadata_stdout_event.assert_called_once()

    @pytest.mark.unit
    def test_handle_metadata_response_with_routing_portnum_and_error(
        self, mock_node: MagicMock, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Handle metadata response with routing portnum and error should set Nak and expire timeout."""
        runtime = _NodeMetadataResponseRuntime(mock_node)
        packet: dict[str, Any] = {
            "decoded": {
                "portnum": portnums_pb2.PortNum.Name(portnums_pb2.PortNum.ROUTING_APP),
                "routing": {"errorReason": "NO_RESPONSE"},
            }
        }
        original_expire_time = time.time() + 300
        mock_node._timeout.expireTime = original_expire_time

        with caplog.at_level(logging.WARNING):
            runtime.handle_metadata_response(packet)

        assert "Metadata request failed, error reason: NO_RESPONSE" in caplog.text
        assert mock_node.iface._acknowledgment.receivedNak is True
        assert mock_node._timeout.expireTime <= original_expire_time
        mock_node._timeout.reset.assert_not_called()

    @pytest.mark.unit
    def test_handle_metadata_response_with_routing_portnum_no_error(
        self, mock_node: MagicMock, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Handle metadata response with routing portnum and no error should return waiting for payload."""
        runtime = _NodeMetadataResponseRuntime(mock_node)
        packet: dict[str, Any] = {
            "decoded": {
                "portnum": portnums_pb2.PortNum.Name(portnums_pb2.PortNum.ROUTING_APP),
                "routing": {"errorReason": "NONE"},
            }
        }

        with caplog.at_level(logging.DEBUG):
            runtime.handle_metadata_response(packet)

        assert (
            "Metadata request routed successfully; waiting for ADMIN_APP payload"
            in caplog.text
        )
        mock_node._signal_metadata_stdout_event.assert_not_called()

    @pytest.mark.unit
    def test_handle_metadata_response_missing_admin_logs_warning(
        self, mock_node: MagicMock, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Handle metadata response with missing admin should log warning."""
        runtime = _NodeMetadataResponseRuntime(mock_node)
        packet: dict[str, Any] = {
            "decoded": {
                "portnum": "ADMIN_APP",
            }
        }

        with caplog.at_level(logging.WARNING):
            runtime.handle_metadata_response(packet)

        assert "Received malformed metadata response (missing admin)" in caplog.text
        mock_node._signal_metadata_stdout_event.assert_called_once()

    @pytest.mark.unit
    def test_handle_metadata_response_missing_admin_raw_logs_warning(
        self, mock_node: MagicMock, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Handle metadata response with missing admin.raw should log warning."""
        runtime = _NodeMetadataResponseRuntime(mock_node)
        packet: dict[str, Any] = {
            "decoded": {
                "portnum": "ADMIN_APP",
                "admin": {"some_field": "value"},
            }
        }

        with caplog.at_level(logging.WARNING):
            runtime.handle_metadata_response(packet)

        assert "Received malformed metadata response (missing admin.raw)" in caplog.text
        mock_node._signal_metadata_stdout_event.assert_called_once()

    @pytest.mark.unit
    def test_handle_metadata_response_valid_sets_ack_and_stores_snapshot(
        self, mock_node: MagicMock, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Handle valid metadata response should set ack, store snapshot, and emit lines."""
        runtime = _NodeMetadataResponseRuntime(mock_node)

        raw = admin_pb2.AdminMessage()
        response = raw.get_device_metadata_response
        response.firmware_version = "2.7.19"
        response.device_state_version = 25
        response.role = config_pb2.Config.DeviceConfig.Role.CLIENT
        response.position_flags = 0
        response.hw_model = mesh_pb2.HardwareModel.PORTDUINO
        response.hasPKC = True

        packet: dict[str, Any] = {
            "decoded": {
                "portnum": "ADMIN_APP",
                "admin": {"raw": raw},
            }
        }

        mock_node._set_metadata_snapshot = MagicMock()
        mock_node._emit_metadata_line = MagicMock()
        mock_node.position_flags_list = MagicMock(return_value=["flag1"])
        mock_node.excluded_modules_list = MagicMock(return_value=["module1"])

        with caplog.at_level(logging.DEBUG):
            runtime.handle_metadata_response(packet)

        assert "Received metadata" in caplog.text
        assert mock_node.iface._acknowledgment.receivedAck is True
        mock_node._set_metadata_snapshot.assert_called_once()
        mock_node._timeout.reset.assert_called_once()
        mock_node._signal_metadata_stdout_event.assert_called_once()

    @pytest.mark.unit
    def test_handle_routing_portnum_malformed_routing_logs_warning(
        self, mock_node: MagicMock, caplog: pytest.LogCaptureFixture
    ) -> None:
        """_handle_routing_portnum with malformed routing should log warning and signal event."""
        runtime = _NodeMetadataResponseRuntime(mock_node)
        decoded: dict[str, Any] = {
            "portnum": portnums_pb2.PortNum.Name(portnums_pb2.PortNum.ROUTING_APP),
            "routing": None,
        }

        with caplog.at_level(logging.WARNING):
            result = runtime._handle_routing_portnum(decoded)

        assert result is True
        assert "Received malformed metadata response (missing routing)" in caplog.text
        mock_node._signal_metadata_stdout_event.assert_called_once()

    @pytest.mark.unit
    def test_handle_routing_portnum_invalid_error_reason_type(
        self, mock_node: MagicMock, caplog: pytest.LogCaptureFixture
    ) -> None:
        """_handle_routing_portnum with invalid errorReason type should log warning."""
        runtime = _NodeMetadataResponseRuntime(mock_node)
        decoded: dict[str, Any] = {
            "portnum": portnums_pb2.PortNum.Name(portnums_pb2.PortNum.ROUTING_APP),
            "routing": {"errorReason": 12345},
        }

        with caplog.at_level(logging.WARNING):
            result = runtime._handle_routing_portnum(decoded)

        assert result is True
        assert "invalid routing.errorReason" in caplog.text
        mock_node._signal_metadata_stdout_event.assert_called_once()

    @pytest.mark.unit
    def test_handle_routing_portnum_non_routing_returns_false(
        self, mock_node: MagicMock
    ) -> None:
        """_handle_routing_portnum should return False for non-ROUTING_APP portnum."""
        runtime = _NodeMetadataResponseRuntime(mock_node)
        decoded: dict[str, Any] = {
            "portnum": "ADMIN_APP",
        }

        result = runtime._handle_routing_portnum(decoded)

        assert result is False

    @pytest.mark.unit
    def test_handle_generic_routing_error_with_error_sets_nak(
        self, mock_node: MagicMock, caplog: pytest.LogCaptureFixture
    ) -> None:
        """_handle_generic_routing_error with error reason should set Nak and signal event."""
        runtime = _NodeMetadataResponseRuntime(mock_node)
        decoded: dict[str, Any] = {
            "portnum": "ADMIN_APP",
            "routing": {"errorReason": "NO_RESPONSE"},
        }

        with caplog.at_level(logging.ERROR):
            result = runtime._handle_generic_routing_error(decoded)

        assert result is True
        assert "Error on response: NO_RESPONSE" in caplog.text
        assert mock_node.iface._acknowledgment.receivedNak is True
        mock_node._signal_metadata_stdout_event.assert_called_once()

    @pytest.mark.unit
    def test_handle_generic_routing_error_no_error_returns_false(
        self, mock_node: MagicMock
    ) -> None:
        """_handle_generic_routing_error with NONE error reason should return False."""
        runtime = _NodeMetadataResponseRuntime(mock_node)
        decoded: dict[str, Any] = {
            "portnum": "ADMIN_APP",
            "routing": {"errorReason": "NONE"},
        }

        result = runtime._handle_generic_routing_error(decoded)

        assert result is False

    @pytest.mark.unit
    def test_handle_generic_routing_error_missing_routing_returns_false(
        self, mock_node: MagicMock
    ) -> None:
        """_handle_generic_routing_error with missing routing should return False."""
        runtime = _NodeMetadataResponseRuntime(mock_node)
        decoded: dict[str, Any] = {
            "portnum": "ADMIN_APP",
        }

        result = runtime._handle_generic_routing_error(decoded)

        assert result is False

    @pytest.mark.unit
    def test_handle_generic_routing_error_invalid_error_reason_type(
        self, mock_node: MagicMock, caplog: pytest.LogCaptureFixture
    ) -> None:
        """_handle_generic_routing_error with invalid errorReason type should log warning."""
        runtime = _NodeMetadataResponseRuntime(mock_node)
        decoded: dict[str, Any] = {
            "portnum": "ADMIN_APP",
            "routing": {"errorReason": None},
        }

        with caplog.at_level(logging.WARNING):
            result = runtime._handle_generic_routing_error(decoded)

        assert result is True
        assert "invalid routing.errorReason" in caplog.text
        mock_node._signal_metadata_stdout_event.assert_called_once()

    @pytest.mark.unit
    def test_emit_metadata_lines_emits_firmware_version(
        self, mock_node: MagicMock
    ) -> None:
        """_emit_metadata_lines should emit firmware version."""
        runtime = _NodeMetadataResponseRuntime(mock_node)
        mock_node._emit_metadata_line = MagicMock()
        mock_node.position_flags_list = MagicMock(return_value=[])
        mock_node.excluded_modules_list = MagicMock(return_value=[])

        metadata = mesh_pb2.DeviceMetadata(
            firmware_version="2.7.19",
            device_state_version=25,
            role=config_pb2.Config.DeviceConfig.Role.CLIENT,
            position_flags=0,
            hw_model=mesh_pb2.HardwareModel.PORTDUINO,
            hasPKC=True,
        )

        runtime._emit_metadata_lines(metadata)

        calls = [
            str(call.args[0]) for call in mock_node._emit_metadata_line.call_args_list
        ]
        assert any("firmware_version: 2.7.19" in call for call in calls)

    @pytest.mark.unit
    def test_emit_metadata_lines_emits_enum_names_for_valid_values(
        self, mock_node: MagicMock
    ) -> None:
        """_emit_metadata_lines should emit enum names for valid enum values."""
        runtime = _NodeMetadataResponseRuntime(mock_node)
        mock_node._emit_metadata_line = MagicMock()
        mock_node.position_flags_list = MagicMock(return_value=[])
        mock_node.excluded_modules_list = MagicMock(return_value=[])

        metadata = mesh_pb2.DeviceMetadata(
            firmware_version="2.7.19",
            device_state_version=25,
            role=config_pb2.Config.DeviceConfig.Role.CLIENT,
            position_flags=0,
            hw_model=mesh_pb2.HardwareModel.PORTDUINO,
            hasPKC=True,
        )

        runtime._emit_metadata_lines(metadata)

        calls = [
            str(call.args[0]) for call in mock_node._emit_metadata_line.call_args_list
        ]
        assert any("role: CLIENT" in call for call in calls)
        assert any("hw_model: PORTDUINO" in call for call in calls)

    @pytest.mark.unit
    def test_emit_metadata_lines_emits_numeric_for_unknown_enum_values(
        self, mock_node: MagicMock
    ) -> None:
        """_emit_metadata_lines should emit numeric values for unknown enum values."""
        runtime = _NodeMetadataResponseRuntime(mock_node)
        mock_node._emit_metadata_line = MagicMock()
        mock_node.position_flags_list = MagicMock(return_value=[])
        mock_node.excluded_modules_list = MagicMock(return_value=[])

        metadata = mesh_pb2.DeviceMetadata(
            firmware_version="2.7.19",
            device_state_version=25,
            role=cast(config_pb2.Config.DeviceConfig.Role.ValueType, 999),
            position_flags=0,
            hw_model=cast(mesh_pb2.HardwareModel.ValueType, 888),
            hasPKC=True,
        )

        runtime._emit_metadata_lines(metadata)

        calls = [
            str(call.args[0]) for call in mock_node._emit_metadata_line.call_args_list
        ]
        assert any("role: 999" in call for call in calls)
        assert any("hw_model: 888" in call for call in calls)

    @pytest.mark.unit
    def test_emit_metadata_lines_emits_excluded_modules_when_present(
        self, mock_node: MagicMock
    ) -> None:
        """_emit_metadata_lines should emit excluded_modules when non-zero."""
        runtime = _NodeMetadataResponseRuntime(mock_node)
        mock_node._emit_metadata_line = MagicMock()
        mock_node.position_flags_list = MagicMock(return_value=[])
        mock_node.excluded_modules_list = MagicMock(return_value=["mod1", "mod2"])

        metadata = mesh_pb2.DeviceMetadata(
            firmware_version="2.7.19",
            device_state_version=25,
            role=config_pb2.Config.DeviceConfig.Role.CLIENT,
            position_flags=0,
            hw_model=mesh_pb2.HardwareModel.PORTDUINO,
            hasPKC=True,
            excluded_modules=3,
        )

        runtime._emit_metadata_lines(metadata)

        calls = [
            str(call.args[0]) for call in mock_node._emit_metadata_line.call_args_list
        ]
        assert any("excluded_modules:" in call for call in calls)


class TestNodeChannelResponseRuntime:
    """Tests for _NodeChannelResponseRuntime."""

    @pytest.mark.unit
    def test_handle_channel_response_missing_decoded_returns_early(
        self, mock_node_for_channel: MagicMock, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Handle channel response with missing decoded should return early."""
        runtime = _NodeChannelResponseRuntime(mock_node_for_channel)
        packet: dict[str, Any] = {"decoded": None}

        with caplog.at_level(logging.WARNING):
            runtime.handle_channel_response(packet)

        assert "malformed channel response without decoded payload" in caplog.text

    @pytest.mark.unit
    def test_handle_channel_response_routing_error_retries_inflight_request(
        self, mock_node_for_channel: MagicMock, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Handle channel response with routing error should retry in-flight request."""
        runtime = _NodeChannelResponseRuntime(mock_node_for_channel)
        runtime.mark_channel_request_sent(3)
        packet: dict[str, Any] = {
            "decoded": {
                "portnum": portnums_pb2.PortNum.Name(portnums_pb2.PortNum.ROUTING_APP),
                "routing": {"errorReason": "NO_RESPONSE"},
            }
        }

        with caplog.at_level(logging.WARNING):
            runtime.handle_channel_response(packet)

        assert "Channel request failed, error reason: NO_RESPONSE" in caplog.text
        mock_node_for_channel._request_channel.assert_called_once_with(3)
        mock_node_for_channel._timeout.reset.assert_called_once()
        assert runtime.has_channel_request_failed() is False

    @pytest.mark.unit
    def test_handle_channel_response_routing_success_waits_for_admin_payload(
        self, mock_node_for_channel: MagicMock, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Handle channel response with routing success should wait for ADMIN_APP payload."""
        runtime = _NodeChannelResponseRuntime(mock_node_for_channel)

        packet: dict[str, Any] = {
            "decoded": {
                "portnum": portnums_pb2.PortNum.Name(portnums_pb2.PortNum.ROUTING_APP),
                "routing": {"errorReason": "NONE"},
            }
        }

        with caplog.at_level(logging.DEBUG):
            runtime.handle_channel_response(packet)

        assert (
            "Channel request routed successfully; waiting for ADMIN_APP payload."
            in caplog.text
        )
        mock_node_for_channel._request_channel.assert_not_called()

    @pytest.mark.unit
    def test_handle_channel_response_admin_message_appends_to_partial_channels(
        self, mock_node_for_channel: MagicMock, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Handle channel response with admin message should append to partialChannels."""
        runtime = _NodeChannelResponseRuntime(mock_node_for_channel)

        raw = admin_pb2.AdminMessage()
        channel_response = raw.get_channel_response
        channel_response.index = 0
        channel_response.role = channel_pb2.Channel.Role.PRIMARY
        channel_response.settings.name = "admin"

        packet: dict[str, Any] = {
            "decoded": {
                "portnum": "ADMIN_APP",
                "admin": {"raw": raw},
            }
        }

        with caplog.at_level(logging.DEBUG):
            runtime.handle_channel_response(packet)

        assert "Received channel index=0 role=PRIMARY" in caplog.text
        assert len(mock_node_for_channel.partialChannels) == 1
        mock_node_for_channel._timeout.reset.assert_called_once()

    @pytest.mark.unit
    def test_handle_channel_response_last_channel_finalizes_channels(
        self, mock_node_for_channel: MagicMock, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Handle channel response with last channel index should finalize channels."""
        runtime = _NodeChannelResponseRuntime(mock_node_for_channel)

        # MAX_CHANNELS is 8, so index 7 (the 8th channel) should finalize
        raw = admin_pb2.AdminMessage()
        channel_response = raw.get_channel_response
        channel_response.index = 7  # Last channel (MAX_CHANNELS - 1)
        channel_response.role = channel_pb2.Channel.Role.SECONDARY
        channel_response.settings.name = "ch8"

        packet: dict[str, Any] = {
            "decoded": {
                "portnum": "ADMIN_APP",
                "admin": {"raw": raw},
            }
        }

        with caplog.at_level(logging.DEBUG):
            runtime.handle_channel_response(packet)

        assert "Finished downloading channels" in caplog.text
        mock_node_for_channel._fixup_channels_locked.assert_called_once()
        # Channels should be set from partialChannels
        assert mock_node_for_channel.channels is not None

    @pytest.mark.unit
    def test_handle_channel_response_non_last_channel_requests_next(
        self, mock_node_for_channel: MagicMock, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Handle channel response with non-last channel should request next channel."""
        runtime = _NodeChannelResponseRuntime(mock_node_for_channel)

        raw = admin_pb2.AdminMessage()
        channel_response = raw.get_channel_response
        channel_response.index = 3
        channel_response.role = channel_pb2.Channel.Role.SECONDARY
        channel_response.settings.name = "ch4"

        packet: dict[str, Any] = {
            "decoded": {
                "portnum": "ADMIN_APP",
                "admin": {"raw": raw},
            }
        }

        with caplog.at_level(logging.DEBUG):
            runtime.handle_channel_response(packet)

        mock_node_for_channel._request_channel.assert_called_once_with(4)

    @pytest.mark.unit
    def test_handle_channel_response_missing_admin_logs_warning(
        self, mock_node_for_channel: MagicMock, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Handle channel response with missing admin should log warning."""
        runtime = _NodeChannelResponseRuntime(mock_node_for_channel)
        packet: dict[str, Any] = {
            "decoded": {
                "portnum": "ADMIN_APP",
            }
        }

        with caplog.at_level(logging.WARNING):
            runtime.handle_channel_response(packet)

        assert "malformed channel response without admin payload" in caplog.text

    @pytest.mark.unit
    def test_handle_channel_response_missing_admin_raw_logs_warning(
        self, mock_node_for_channel: MagicMock, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Handle channel response with missing admin.raw should log warning."""
        runtime = _NodeChannelResponseRuntime(mock_node_for_channel)
        packet: dict[str, Any] = {
            "decoded": {
                "portnum": "ADMIN_APP",
                "admin": {"some_field": "value"},
            }
        }

        with caplog.at_level(logging.WARNING):
            runtime.handle_channel_response(packet)

        assert "malformed channel response without admin.raw payload" in caplog.text

    @pytest.mark.unit
    def test_handle_routing_response_non_routing_returns_false(
        self, mock_node_for_channel: MagicMock
    ) -> None:
        """_handle_routing_response should return False for non-ROUTING_APP portnum."""
        runtime = _NodeChannelResponseRuntime(mock_node_for_channel)
        decoded: dict[str, Any] = {
            "portnum": "ADMIN_APP",
            "routing": {"errorReason": "NONE"},
        }

        result = runtime._handle_routing_response(decoded)

        assert result is False

    @pytest.mark.unit
    def test_handle_routing_response_missing_routing_logs_warning(
        self, mock_node_for_channel: MagicMock, caplog: pytest.LogCaptureFixture
    ) -> None:
        """_handle_routing_response should guard malformed routing payloads."""
        runtime = _NodeChannelResponseRuntime(mock_node_for_channel)
        decoded: dict[str, Any] = {
            "portnum": portnums_pb2.PortNum.Name(portnums_pb2.PortNum.ROUTING_APP),
        }

        with caplog.at_level(logging.WARNING):
            result = runtime._handle_routing_response(decoded)

        assert result is True
        assert "missing routing" in caplog.text
        mock_node_for_channel._request_channel.assert_not_called()
        assert runtime.has_channel_request_failed() is True

    @pytest.mark.unit
    def test_handle_routing_response_routing_success_waits_for_admin_payload(
        self, mock_node_for_channel: MagicMock, caplog: pytest.LogCaptureFixture
    ) -> None:
        """_handle_routing_response should wait for ADMIN_APP payload on routing success."""
        runtime = _NodeChannelResponseRuntime(mock_node_for_channel)

        decoded: dict[str, Any] = {
            "portnum": portnums_pb2.PortNum.Name(portnums_pb2.PortNum.ROUTING_APP),
            "routing": {"errorReason": "NONE"},
        }

        with caplog.at_level(logging.DEBUG):
            result = runtime._handle_routing_response(decoded)

        assert result is True
        assert (
            "Channel request routed successfully; waiting for ADMIN_APP payload."
            in caplog.text
        )
        mock_node_for_channel._request_channel.assert_not_called()

    @pytest.mark.unit
    def test_handle_routing_response_error_without_pending_index_marks_failure(
        self, mock_node_for_channel: MagicMock, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Routing errors without a pending index should set terminal channel-failure state."""
        runtime = _NodeChannelResponseRuntime(mock_node_for_channel)
        decoded: dict[str, Any] = {
            "portnum": portnums_pb2.PortNum.Name(portnums_pb2.PortNum.ROUTING_APP),
            "routing": {"errorReason": "NO_RESPONSE"},
        }

        with caplog.at_level(logging.WARNING):
            result = runtime._handle_routing_response(decoded)

        assert result is True
        assert "no pending index to retry" in caplog.text
        assert runtime.has_channel_request_failed() is True
        mock_node_for_channel._request_channel.assert_not_called()

    @pytest.mark.unit
    def test_handle_routing_response_retry_send_none_marks_failure(
        self, mock_node_for_channel: MagicMock, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Routing retry should mark failure if the retry send is skipped."""
        runtime = _NodeChannelResponseRuntime(mock_node_for_channel)
        runtime.mark_channel_request_sent(4)
        mock_node_for_channel._request_channel.return_value = None
        decoded: dict[str, Any] = {
            "portnum": portnums_pb2.PortNum.Name(portnums_pb2.PortNum.ROUTING_APP),
            "routing": {"errorReason": "NO_RESPONSE"},
        }

        with caplog.at_level(logging.WARNING):
            result = runtime._handle_routing_response(decoded)

        assert result is True
        assert "retry for index 4 was not started" in caplog.text.lower()
        assert runtime.has_channel_request_failed() is True

    @pytest.mark.unit
    def test_handle_routing_response_retry_limit_marks_failure(
        self, mock_node_for_channel: MagicMock, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Persistent routing failures should stop retrying after the configured retry limit."""
        runtime = _NodeChannelResponseRuntime(mock_node_for_channel)
        runtime.mark_channel_request_sent(2)
        decoded: dict[str, Any] = {
            "portnum": portnums_pb2.PortNum.Name(portnums_pb2.PortNum.ROUTING_APP),
            "routing": {"errorReason": "NO_RESPONSE"},
        }

        with caplog.at_level(logging.WARNING):
            first_result = runtime._handle_routing_response(decoded)
            second_result = runtime._handle_routing_response(decoded)

        assert first_result is True
        assert second_result is True
        assert mock_node_for_channel._request_channel.call_count == 1
        assert "retry limit reached for channel 2" in caplog.text
        assert runtime.has_channel_request_failed() is True
