"""Additional tests for node_runtime/content_runtime.py covering specific line ranges."""

# pylint: disable=redefined-outer-name,protected-access

import logging
from unittest.mock import MagicMock

import pytest

from meshtastic.mesh_interface import MeshInterface
from meshtastic.node_runtime.content_runtime import (
    CANNED_MESSAGE_CHUNK_SIZE,
    _NodeAdminContentRuntime,
    _NodeContentCacheStore,
    _NodeContentResponseRuntime,
)
from meshtastic.protobuf import admin_pb2

# Tests for line 15: TYPE_CHECKING import
# This is implicitly tested by the fact that the module imports work


@pytest.fixture
def mock_node_for_read_gen() -> MagicMock:
    """Create a minimal mock Node for read generation tests."""
    node = MagicMock(
        spec=[
            "ringtone",
            "ringtonePart",
            "cannedPluginMessage",
            "cannedPluginMessageMessages",
            "module_available",
            "_send_admin",
            "_timeout",
            "_raise_interface_error",
            "onAckNak",
            "ensureSessionKey",
            "iface",
        ]
    )
    node.ringtone = None
    node.ringtonePart = None
    node.cannedPluginMessage = None
    node.cannedPluginMessageMessages = None
    node.module_available = MagicMock(return_value=True)
    node._send_admin = MagicMock(return_value=MagicMock())
    node._timeout = MagicMock()
    node._timeout.expireTimeout = 300.0
    node._raise_interface_error = MagicMock(
        side_effect=MeshInterface.MeshInterfaceError("interface error raised")
    )
    node.onAckNak = MagicMock()
    node.ensureSessionKey = MagicMock()
    node.iface = MagicMock()
    node.iface.localNode = node
    return node


class TestValidateAdminResponsePacket:
    """Tests for _validate_admin_response_packet method (lines 179-186)."""

    @pytest.mark.unit
    def test_validate_admin_response_packet_no_decoded(
        self, mock_node_for_read_gen: MagicMock
    ) -> None:
        """Test validation when decoded is None."""
        cache_store = _NodeContentCacheStore(mock_node_for_read_gen)
        runtime = _NodeContentResponseRuntime(
            mock_node_for_read_gen, cache_store=cache_store
        )

        is_terminal, has_routing_ack, raw_admin = (
            runtime._validate_admin_response_packet(None, "ringtone")
        )

        assert is_terminal is True
        assert has_routing_ack is False
        assert raw_admin is None

    @pytest.mark.unit
    def test_validate_admin_response_packet_routing_error(
        self, mock_node_for_read_gen: MagicMock, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Test validation when routing has an error."""
        cache_store = _NodeContentCacheStore(mock_node_for_read_gen)
        runtime = _NodeContentResponseRuntime(
            mock_node_for_read_gen, cache_store=cache_store
        )

        decoded = {"routing": {"errorReason": "NO_RESPONSE"}}

        with caplog.at_level(logging.ERROR):
            is_terminal, has_routing_ack, raw_admin = (
                runtime._validate_admin_response_packet(decoded, "ringtone")
            )

        assert is_terminal is True
        assert has_routing_ack is False
        assert raw_admin is None
        assert "Error on response: NO_RESPONSE" in caplog.text

    @pytest.mark.unit
    def test_validate_admin_response_packet_routing_ack_only(
        self, mock_node_for_read_gen: MagicMock
    ) -> None:
        """Test validation when only routing ACK is present."""
        cache_store = _NodeContentCacheStore(mock_node_for_read_gen)
        runtime = _NodeContentResponseRuntime(
            mock_node_for_read_gen, cache_store=cache_store
        )

        decoded = {"routing": {"errorReason": "NONE"}}

        is_terminal, has_routing_ack, raw_admin = (
            runtime._validate_admin_response_packet(decoded, "ringtone")
        )

        assert is_terminal is False
        assert has_routing_ack is True
        assert raw_admin is None

    @pytest.mark.unit
    def test_validate_admin_response_packet_with_admin_raw(
        self, mock_node_for_read_gen: MagicMock
    ) -> None:
        """Test validation when admin raw data is present."""
        cache_store = _NodeContentCacheStore(mock_node_for_read_gen)
        runtime = _NodeContentResponseRuntime(
            mock_node_for_read_gen, cache_store=cache_store
        )

        raw_admin = admin_pb2.AdminMessage()
        raw_admin.get_ringtone_response = "test"

        decoded = {"admin": {"raw": raw_admin}}

        is_terminal, has_routing_ack, raw_admin_out = (
            runtime._validate_admin_response_packet(decoded, "ringtone")
        )

        assert is_terminal is False
        assert has_routing_ack is False
        assert raw_admin_out is raw_admin


class TestReadGenerationMethods:
    """Tests for read generation management methods (lines 199, 208-255)."""

    @pytest.mark.unit
    def test_begin_ringtone_read_increments_generation(
        self, mock_node_for_read_gen: MagicMock
    ) -> None:
        """Test that _begin_ringtone_read increments generation counter (line 199)."""
        cache_store = _NodeContentCacheStore(mock_node_for_read_gen)
        runtime = _NodeAdminContentRuntime(
            mock_node_for_read_gen,
            cache_store=cache_store,
            response_runtime=MagicMock(),
        )

        gen1 = runtime._begin_ringtone_read()
        gen2 = runtime._begin_ringtone_read()

        assert gen2 == gen1 + 1
        assert runtime._active_ringtone_read_generation == gen2

    @pytest.mark.unit
    def test_retire_ringtone_read_clears_active(
        self, mock_node_for_read_gen: MagicMock
    ) -> None:
        """Test that _retire_ringtone_read clears active generation."""
        cache_store = _NodeContentCacheStore(mock_node_for_read_gen)
        runtime = _NodeAdminContentRuntime(
            mock_node_for_read_gen,
            cache_store=cache_store,
            response_runtime=MagicMock(),
        )

        gen = runtime._begin_ringtone_read()
        assert runtime._active_ringtone_read_generation == gen

        runtime._retire_ringtone_read(gen)
        assert runtime._active_ringtone_read_generation is None

    @pytest.mark.unit
    def test_is_ringtone_read_active_returns_correct_state(
        self, mock_node_for_read_gen: MagicMock
    ) -> None:
        """Test _is_ringtone_read_active returns correct state."""
        cache_store = _NodeContentCacheStore(mock_node_for_read_gen)
        runtime = _NodeAdminContentRuntime(
            mock_node_for_read_gen,
            cache_store=cache_store,
            response_runtime=MagicMock(),
        )

        gen = runtime._begin_ringtone_read()
        assert runtime._is_ringtone_read_active(gen) is True
        assert runtime._is_ringtone_read_active(gen + 1) is False

        runtime._retire_ringtone_read(gen)
        assert runtime._is_ringtone_read_active(gen) is False

    @pytest.mark.unit
    def test_begin_canned_message_read_increments_generation(
        self, mock_node_for_read_gen: MagicMock
    ) -> None:
        """Test that _begin_canned_message_read increments generation counter."""
        cache_store = _NodeContentCacheStore(mock_node_for_read_gen)
        runtime = _NodeAdminContentRuntime(
            mock_node_for_read_gen,
            cache_store=cache_store,
            response_runtime=MagicMock(),
        )

        gen1 = runtime._begin_canned_message_read()
        gen2 = runtime._begin_canned_message_read()

        assert gen2 == gen1 + 1
        assert runtime._active_canned_message_read_generation == gen2

    @pytest.mark.unit
    def test_retire_canned_message_read_clears_active(
        self, mock_node_for_read_gen: MagicMock
    ) -> None:
        """Test that _retire_canned_message_read clears active generation."""
        cache_store = _NodeContentCacheStore(mock_node_for_read_gen)
        runtime = _NodeAdminContentRuntime(
            mock_node_for_read_gen,
            cache_store=cache_store,
            response_runtime=MagicMock(),
        )

        gen = runtime._begin_canned_message_read()
        assert runtime._active_canned_message_read_generation == gen

        runtime._retire_canned_message_read(gen)
        assert runtime._active_canned_message_read_generation is None

    @pytest.mark.unit
    def test_is_canned_message_read_active_returns_correct_state(
        self, mock_node_for_read_gen: MagicMock
    ) -> None:
        """Test _is_canned_message_read_active returns correct state."""
        cache_store = _NodeContentCacheStore(mock_node_for_read_gen)
        runtime = _NodeAdminContentRuntime(
            mock_node_for_read_gen,
            cache_store=cache_store,
            response_runtime=MagicMock(),
        )

        gen = runtime._begin_canned_message_read()
        assert runtime._is_canned_message_read_active(gen) is True
        assert runtime._is_canned_message_read_active(gen + 1) is False

        runtime._retire_canned_message_read(gen)
        assert runtime._is_canned_message_read_active(gen) is False


class TestSelectWriteResponseHandler:
    """Tests for _select_write_response_handler method (lines 352-356)."""

    @pytest.mark.unit
    def test_select_write_response_handler_local_node_returns_none(
        self, mock_node_for_read_gen: MagicMock
    ) -> None:
        """Test _select_write_response_handler returns None for local node."""
        mock_node_for_read_gen.iface.localNode = mock_node_for_read_gen
        cache_store = _NodeContentCacheStore(mock_node_for_read_gen)
        runtime = _NodeAdminContentRuntime(
            mock_node_for_read_gen,
            cache_store=cache_store,
            response_runtime=MagicMock(),
        )

        result = runtime._select_write_response_handler()

        assert result is None

    @pytest.mark.unit
    def test_select_write_response_handler_remote_node_returns_onAckNak(
        self, mock_node_for_read_gen: MagicMock
    ) -> None:
        """Test _select_write_response_handler returns onAckNak for remote node."""
        local_node = MagicMock()
        mock_node_for_read_gen.iface.localNode = local_node
        cache_store = _NodeContentCacheStore(mock_node_for_read_gen)
        runtime = _NodeAdminContentRuntime(
            mock_node_for_read_gen,
            cache_store=cache_store,
            response_runtime=MagicMock(),
        )

        result = runtime._select_write_response_handler()

        assert result == mock_node_for_read_gen.onAckNak


class TestWriteCannedMessageChunking:
    """Tests for write_canned_message chunking (lines 498-532)."""

    @pytest.mark.unit
    def test_write_canned_message_single_chunk_short_message(
        self, mock_node_for_read_gen: MagicMock
    ) -> None:
        """Test short message is sent as single chunk."""
        cache_store = _NodeContentCacheStore(mock_node_for_read_gen)
        runtime = _NodeAdminContentRuntime(
            mock_node_for_read_gen,
            cache_store=cache_store,
            response_runtime=MagicMock(),
        )

        # Message shorter than chunk size
        short_message = "x" * (CANNED_MESSAGE_CHUNK_SIZE - 1)

        runtime.write_canned_message(short_message)

        # Should be sent as single message, not chunked
        assert mock_node_for_read_gen._send_admin.call_count == 1

    @pytest.mark.unit
    def test_write_canned_message_multi_chunk_long_message(
        self, mock_node_for_read_gen: MagicMock, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Test long message is split into multiple chunks."""
        # CANNED_MESSAGE_CHUNK_SIZE already imported at top

        cache_store = _NodeContentCacheStore(mock_node_for_read_gen)
        runtime = _NodeAdminContentRuntime(
            mock_node_for_read_gen,
            cache_store=cache_store,
            response_runtime=MagicMock(),
        )

        # Message longer than chunk size
        long_message = "x" * (CANNED_MESSAGE_CHUNK_SIZE * 3 + 10)

        with caplog.at_level(logging.DEBUG):
            runtime.write_canned_message(long_message)

        # Should be split into multiple sends
        assert (
            mock_node_for_read_gen._send_admin.call_count == 4
        )  # 3 full chunks + 1 partial
        assert "Setting canned message in 4 chunks" in caplog.text

    @pytest.mark.unit
    def test_write_canned_message_chunk_send_failure_invalidation(
        self, mock_node_for_read_gen: MagicMock
    ) -> None:
        """Test that cache is invalidated if chunk send fails after previous chunks sent."""
        # CANNED_MESSAGE_CHUNK_SIZE already imported at top

        cache_store = _NodeContentCacheStore(mock_node_for_read_gen)
        runtime = _NodeAdminContentRuntime(
            mock_node_for_read_gen,
            cache_store=cache_store,
            response_runtime=MagicMock(),
        )

        # Long message that will be chunked
        long_message = "x" * (CANNED_MESSAGE_CHUNK_SIZE * 2 + 10)

        # First chunk succeeds, second fails
        call_count = 0

        def send_admin_side_effect(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return MagicMock()
            return None

        mock_node_for_read_gen._send_admin.side_effect = send_admin_side_effect

        # Set up cache to verify invalidation
        mock_node_for_read_gen.cannedPluginMessage = "old_message"

        result = runtime.write_canned_message(long_message)

        # Result should be None because second chunk failed
        assert result is None
        # Cache should be invalidated because at least one chunk was written
        assert mock_node_for_read_gen.cannedPluginMessage is None

    @pytest.mark.unit
    def test_write_canned_message_first_chunk_failure_no_invalidation(
        self, mock_node_for_read_gen: MagicMock
    ) -> None:
        """Test that cache is NOT invalidated if first chunk send fails."""
        # CANNED_MESSAGE_CHUNK_SIZE already imported at top

        cache_store = _NodeContentCacheStore(mock_node_for_read_gen)
        runtime = _NodeAdminContentRuntime(
            mock_node_for_read_gen,
            cache_store=cache_store,
            response_runtime=MagicMock(),
        )

        # Long message that will be chunked
        long_message = "x" * (CANNED_MESSAGE_CHUNK_SIZE * 2 + 10)

        # First chunk fails
        mock_node_for_read_gen._send_admin.return_value = None

        # Set up cache to verify it's NOT invalidated
        mock_node_for_read_gen.cannedPluginMessage = "old_message"

        result = runtime.write_canned_message(long_message)

        # Result should be None because first chunk failed
        assert result is None
        # Cache should NOT be invalidated because no chunks were written
        assert mock_node_for_read_gen.cannedPluginMessage == "old_message"

    @pytest.mark.unit
    def test_write_canned_message_exception_during_chunks(
        self, mock_node_for_read_gen: MagicMock
    ) -> None:
        """Test that exception during chunking invalidates cache and re-raises."""
        # CANNED_MESSAGE_CHUNK_SIZE already imported at top

        cache_store = _NodeContentCacheStore(mock_node_for_read_gen)
        runtime = _NodeAdminContentRuntime(
            mock_node_for_read_gen,
            cache_store=cache_store,
            response_runtime=MagicMock(),
        )

        # Long message that will be chunked
        long_message = "x" * (CANNED_MESSAGE_CHUNK_SIZE * 2 + 10)

        # First chunk succeeds, second raises exception
        call_count = 0

        def send_admin_side_effect(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return MagicMock()
            raise RuntimeError("Send failed")

        mock_node_for_read_gen._send_admin.side_effect = send_admin_side_effect

        # Set up cache to verify invalidation
        mock_node_for_read_gen.cannedPluginMessage = "old_message"

        with pytest.raises(RuntimeError, match="Send failed"):
            runtime.write_canned_message(long_message)

        # Cache should be invalidated because at least one chunk was written
        assert mock_node_for_read_gen.cannedPluginMessage is None

    @pytest.mark.unit
    def test_write_canned_message_success_returns_first_result(
        self, mock_node_for_read_gen: MagicMock
    ) -> None:
        """Test that successful multi-chunk write returns first chunk's result."""
        # CANNED_MESSAGE_CHUNK_SIZE already imported at top

        cache_store = _NodeContentCacheStore(mock_node_for_read_gen)
        runtime = _NodeAdminContentRuntime(
            mock_node_for_read_gen,
            cache_store=cache_store,
            response_runtime=MagicMock(),
        )

        # Long message that will be chunked
        long_message = "x" * (CANNED_MESSAGE_CHUNK_SIZE * 2 + 10)

        # All chunks succeed
        first_result = MagicMock()
        call_count = 0

        def send_admin_side_effect(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return first_result
            return MagicMock()

        mock_node_for_read_gen._send_admin.side_effect = send_admin_side_effect

        result = runtime.write_canned_message(long_message)

        # Should return the first chunk's result
        assert result is first_result

    @pytest.mark.unit
    def test_write_canned_message_invalidation_on_success(
        self, mock_node_for_read_gen: MagicMock
    ) -> None:
        """Test that cache is invalidated after successful multi-chunk write."""
        # CANNED_MESSAGE_CHUNK_SIZE already imported at top

        cache_store = _NodeContentCacheStore(mock_node_for_read_gen)
        runtime = _NodeAdminContentRuntime(
            mock_node_for_read_gen,
            cache_store=cache_store,
            response_runtime=MagicMock(),
        )

        # Long message that will be chunked
        long_message = "x" * (CANNED_MESSAGE_CHUNK_SIZE * 2 + 10)

        # All chunks succeed
        mock_node_for_read_gen._send_admin.return_value = MagicMock()

        # Set up cache to verify invalidation
        mock_node_for_read_gen.cannedPluginMessage = "old_message"
        mock_node_for_read_gen.cannedPluginMessageMessages = "old_fragment"

        runtime.write_canned_message(long_message)

        # Cache should be invalidated
        assert mock_node_for_read_gen.cannedPluginMessage is None
        assert mock_node_for_read_gen.cannedPluginMessageMessages is None
