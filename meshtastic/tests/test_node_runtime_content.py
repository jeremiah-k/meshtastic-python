"""Meshtastic unit tests for node_runtime/content_runtime.py."""

# pylint: disable=redefined-outer-name

import logging
import threading
from collections.abc import Callable
from typing import Any
from unittest.mock import MagicMock

import pytest

from ..mesh_interface import MeshInterface
from ..node_runtime.content_runtime import (
    _NodeAdminContentRuntime,
    _NodeContentCacheStore,
    _NodeContentResponseRuntime,
)
from ..node_runtime.shared import MAX_CANNED_MESSAGE_LENGTH, MAX_RINGTONE_LENGTH
from ..protobuf import admin_pb2


@pytest.fixture
def mock_node_for_cache() -> MagicMock:
    """Create a minimal mock Node for cache store tests."""
    node = MagicMock(
        spec=[
            "ringtone",
            "ringtonePart",
            "cannedPluginMessage",
            "cannedPluginMessageMessages",
            "_ringtone_lock",
            "_canned_message_lock",
        ]
    )
    node.ringtone = None
    node.ringtonePart = None
    node.cannedPluginMessage = None
    node.cannedPluginMessageMessages = None

    # Create real locks for proper synchronization testing
    node._ringtone_lock = threading.Lock()
    node._canned_message_lock = threading.Lock()
    return node


@pytest.fixture
def mock_node_for_admin(mock_node_for_cache: MagicMock) -> MagicMock:
    """Create a mock Node for admin content runtime tests."""
    node = mock_node_for_cache
    node.module_available = MagicMock(return_value=True)
    node._send_admin = MagicMock(return_value=MagicMock())
    node._timeout = MagicMock()
    node._timeout.expireTimeout = 300
    node.ensureSessionKey = MagicMock()
    node._raise_interface_error = MagicMock(
        side_effect=MeshInterface.MeshInterfaceError("interface error raised")
    )
    node.onAckNak = MagicMock()
    node.iface = MagicMock()
    node.iface.localNode = node
    return node


class TestNodeContentCacheStore:
    """Tests for _NodeContentCacheStore."""

    @pytest.mark.unit
    def test_get_cached_ringtone_with_cache_returns_ringtone(
        self, mock_node_for_cache: MagicMock, caplog: pytest.LogCaptureFixture
    ) -> None:
        """get_cached_ringtone with cached value should return the ringtone."""
        mock_node_for_cache.ringtone = "RTTTL: melody:d=4,o=5,b=100:c"
        cache_store = _NodeContentCacheStore(mock_node_for_cache)

        with caplog.at_level(logging.DEBUG):
            result = cache_store.get_cached_ringtone()

        assert result == "RTTTL: melody:d=4,o=5,b=100:c"
        assert "ringtone cached" in caplog.text
        assert "29 chars" in caplog.text

    @pytest.mark.unit
    def test_get_cached_ringtone_no_cache_returns_none(
        self, mock_node_for_cache: MagicMock
    ) -> None:
        """get_cached_ringtone with no cached value should return None."""
        mock_node_for_cache.ringtone = None
        cache_store = _NodeContentCacheStore(mock_node_for_cache)

        result = cache_store.get_cached_ringtone()

        assert result is None

    @pytest.mark.unit
    def test_clear_ringtone_fragment_clears_fragment(
        self, mock_node_for_cache: MagicMock, caplog: pytest.LogCaptureFixture
    ) -> None:
        """clear_ringtone_fragment should clear the fragment cache."""
        mock_node_for_cache.ringtonePart = "some_fragment"
        cache_store = _NodeContentCacheStore(mock_node_for_cache)

        with caplog.at_level(logging.DEBUG):
            cache_store.clear_ringtone_fragment()

        assert mock_node_for_cache.ringtonePart is None
        assert "ringtone fragment cache cleared" in caplog.text

    @pytest.mark.unit
    def test_store_ringtone_fragment_stores_fragment(
        self, mock_node_for_cache: MagicMock, caplog: pytest.LogCaptureFixture
    ) -> None:
        """store_ringtone_fragment should store the ringtone fragment."""
        cache_store = _NodeContentCacheStore(mock_node_for_cache)
        fragment = "RTTTL: partial:d=4,o=5,b=100:c,d,e"

        with caplog.at_level(logging.DEBUG):
            cache_store.store_ringtone_fragment(fragment)

        assert mock_node_for_cache.ringtonePart == fragment
        assert "ringtone fragment stored" in caplog.text
        assert "34 chars" in caplog.text

    @pytest.mark.unit
    def test_resolve_ringtone_after_read_from_full_cache(
        self, mock_node_for_cache: MagicMock, caplog: pytest.LogCaptureFixture
    ) -> None:
        """resolve_ringtone_after_read should return cached ringtone when available."""
        mock_node_for_cache.ringtone = "RTTTL: cached:d=4,o=5,b=100:c"
        cache_store = _NodeContentCacheStore(mock_node_for_cache)

        with caplog.at_level(logging.DEBUG):
            result = cache_store.resolve_ringtone_after_read()

        assert result == "RTTTL: cached:d=4,o=5,b=100:c"
        assert "ringtone resolved from full cache" in caplog.text

    @pytest.mark.unit
    def test_resolve_ringtone_after_read_from_fragment(
        self, mock_node_for_cache: MagicMock, caplog: pytest.LogCaptureFixture
    ) -> None:
        """resolve_ringtone_after_read should use fragment when full cache is None."""
        mock_node_for_cache.ringtone = None
        mock_node_for_cache.ringtonePart = "RTTTL: fragment:d=4,o=5,b=100:c"
        cache_store = _NodeContentCacheStore(mock_node_for_cache)

        with caplog.at_level(logging.DEBUG):
            result = cache_store.resolve_ringtone_after_read()

        assert result == "RTTTL: fragment:d=4,o=5,b=100:c"
        assert mock_node_for_cache.ringtone == "RTTTL: fragment:d=4,o=5,b=100:c"
        assert "ringtone resolved from fragment cache" in caplog.text

    @pytest.mark.unit
    def test_resolve_ringtone_after_read_no_cache_returns_none(
        self, mock_node_for_cache: MagicMock
    ) -> None:
        """resolve_ringtone_after_read should return None when no cache available."""
        mock_node_for_cache.ringtone = None
        mock_node_for_cache.ringtonePart = None
        cache_store = _NodeContentCacheStore(mock_node_for_cache)

        result = cache_store.resolve_ringtone_after_read()

        assert result is None

    @pytest.mark.unit
    def test_invalidate_ringtone_cache_clears_both(
        self, mock_node_for_cache: MagicMock, caplog: pytest.LogCaptureFixture
    ) -> None:
        """invalidate_ringtone_cache should clear both full and fragment cache."""
        mock_node_for_cache.ringtone = "full_ringtone"
        mock_node_for_cache.ringtonePart = "fragment_ringtone"
        cache_store = _NodeContentCacheStore(mock_node_for_cache)

        with caplog.at_level(logging.DEBUG):
            cache_store.invalidate_ringtone_cache()

        assert mock_node_for_cache.ringtone is None
        assert mock_node_for_cache.ringtonePart is None
        assert "ringtone cache invalidated" in caplog.text

    @pytest.mark.unit
    def test_get_cached_canned_message_with_cache_returns_message(
        self, mock_node_for_cache: MagicMock, caplog: pytest.LogCaptureFixture
    ) -> None:
        """get_cached_canned_message with cached value should return the message."""
        mock_node_for_cache.cannedPluginMessage = "Hello\nWorld\nTest"
        cache_store = _NodeContentCacheStore(mock_node_for_cache)

        with caplog.at_level(logging.DEBUG):
            result = cache_store.get_cached_canned_message()

        assert result == "Hello\nWorld\nTest"
        assert "canned message cached" in caplog.text
        assert "16 chars" in caplog.text

    @pytest.mark.unit
    def test_get_cached_canned_message_no_cache_returns_none(
        self, mock_node_for_cache: MagicMock
    ) -> None:
        """get_cached_canned_message with no cached value should return None."""
        mock_node_for_cache.cannedPluginMessage = None
        cache_store = _NodeContentCacheStore(mock_node_for_cache)

        result = cache_store.get_cached_canned_message()

        assert result is None

    @pytest.mark.unit
    def test_clear_canned_message_fragment_clears_fragment(
        self, mock_node_for_cache: MagicMock, caplog: pytest.LogCaptureFixture
    ) -> None:
        """clear_canned_message_fragment should clear the fragment cache."""
        mock_node_for_cache.cannedPluginMessageMessages = "some_fragment"
        cache_store = _NodeContentCacheStore(mock_node_for_cache)

        with caplog.at_level(logging.DEBUG):
            cache_store.clear_canned_message_fragment()

        assert mock_node_for_cache.cannedPluginMessageMessages is None
        assert "canned message fragment cache cleared" in caplog.text

    @pytest.mark.unit
    def test_store_canned_message_fragment_stores_message(
        self, mock_node_for_cache: MagicMock, caplog: pytest.LogCaptureFixture
    ) -> None:
        """store_canned_message_fragment should store the canned message fragment."""
        cache_store = _NodeContentCacheStore(mock_node_for_cache)
        fragment = "Hello\nWorld"

        with caplog.at_level(logging.DEBUG):
            cache_store.store_canned_message_fragment(fragment)

        assert mock_node_for_cache.cannedPluginMessageMessages == fragment
        assert "canned message fragment stored" in caplog.text
        assert "11 chars" in caplog.text

    @pytest.mark.unit
    def test_resolve_canned_message_after_read_from_full_cache(
        self, mock_node_for_cache: MagicMock, caplog: pytest.LogCaptureFixture
    ) -> None:
        """resolve_canned_message_after_read should return cached message when available."""
        mock_node_for_cache.cannedPluginMessage = "Cached\nMessage"
        cache_store = _NodeContentCacheStore(mock_node_for_cache)

        with caplog.at_level(logging.DEBUG):
            result = cache_store.resolve_canned_message_after_read()

        assert result == "Cached\nMessage"
        assert "canned message resolved from full cache" in caplog.text

    @pytest.mark.unit
    def test_resolve_canned_message_after_read_from_fragment(
        self, mock_node_for_cache: MagicMock, caplog: pytest.LogCaptureFixture
    ) -> None:
        """resolve_canned_message_after_read should use fragment when full cache is None."""
        mock_node_for_cache.cannedPluginMessage = None
        mock_node_for_cache.cannedPluginMessageMessages = "Fragment\nMessage"
        cache_store = _NodeContentCacheStore(mock_node_for_cache)

        with caplog.at_level(logging.DEBUG):
            result = cache_store.resolve_canned_message_after_read()

        assert result == "Fragment\nMessage"
        assert mock_node_for_cache.cannedPluginMessage == "Fragment\nMessage"
        assert "canned message resolved from fragment cache" in caplog.text

    @pytest.mark.unit
    def test_resolve_canned_message_after_read_no_cache_returns_none(
        self, mock_node_for_cache: MagicMock
    ) -> None:
        """resolve_canned_message_after_read should return None when no cache available."""
        mock_node_for_cache.cannedPluginMessage = None
        mock_node_for_cache.cannedPluginMessageMessages = None
        cache_store = _NodeContentCacheStore(mock_node_for_cache)

        result = cache_store.resolve_canned_message_after_read()

        assert result is None

    @pytest.mark.unit
    def test_invalidate_canned_message_cache_clears_both(
        self, mock_node_for_cache: MagicMock, caplog: pytest.LogCaptureFixture
    ) -> None:
        """invalidate_canned_message_cache should clear both full and fragment cache."""
        mock_node_for_cache.cannedPluginMessage = "full_message"
        mock_node_for_cache.cannedPluginMessageMessages = "fragment_message"
        cache_store = _NodeContentCacheStore(mock_node_for_cache)

        with caplog.at_level(logging.DEBUG):
            cache_store.invalidate_canned_message_cache()

        assert mock_node_for_cache.cannedPluginMessage is None
        assert mock_node_for_cache.cannedPluginMessageMessages is None
        assert "canned message cache invalidated" in caplog.text


class TestNodeContentResponseRuntime:
    """Tests for _NodeContentResponseRuntime."""

    @pytest.fixture
    def cache_store(self, mock_node_for_cache: MagicMock) -> _NodeContentCacheStore:
        """Create a cache store for response runtime tests."""
        return _NodeContentCacheStore(mock_node_for_cache)

    @pytest.mark.unit
    def test_has_routing_error_with_error_returns_true(
        self,
        mock_node_for_cache: MagicMock,
        cache_store: _NodeContentCacheStore,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """_has_routing_error with non-NONE error should return True and log error."""
        runtime = _NodeContentResponseRuntime(
            mock_node_for_cache, cache_store=cache_store
        )
        decoded: dict[str, Any] = {"routing": {"errorReason": "NO_RESPONSE"}}

        with caplog.at_level(logging.ERROR):
            result = runtime._has_routing_error(decoded)

        assert result is True
        assert "Error on response: NO_RESPONSE" in caplog.text

    @pytest.mark.unit
    def test_has_routing_error_with_none_returns_false(
        self, mock_node_for_cache: MagicMock, cache_store: _NodeContentCacheStore
    ) -> None:
        """_has_routing_error with NONE error should return False."""
        runtime = _NodeContentResponseRuntime(
            mock_node_for_cache, cache_store=cache_store
        )
        decoded: dict[str, Any] = {"routing": {"errorReason": "NONE"}}

        result = runtime._has_routing_error(decoded)

        assert result is False

    @pytest.mark.unit
    def test_has_routing_error_no_routing_returns_false(
        self, mock_node_for_cache: MagicMock, cache_store: _NodeContentCacheStore
    ) -> None:
        """_has_routing_error with no routing dict should return False."""
        runtime = _NodeContentResponseRuntime(
            mock_node_for_cache, cache_store=cache_store
        )
        decoded: dict[str, Any] = {}

        result = runtime._has_routing_error(decoded)

        assert result is False

    @pytest.mark.unit
    def test_has_routing_error_invalid_error_reason_type_returns_false(
        self, mock_node_for_cache: MagicMock, cache_store: _NodeContentCacheStore
    ) -> None:
        """_has_routing_error with non-string errorReason should return False."""
        runtime = _NodeContentResponseRuntime(
            mock_node_for_cache, cache_store=cache_store
        )
        decoded: dict[str, Any] = {"routing": {"errorReason": 123}}

        result = runtime._has_routing_error(decoded)

        assert result is False

    @pytest.mark.unit
    def test_handle_ringtone_response_with_valid_response_stores_fragment(
        self,
        mock_node_for_cache: MagicMock,
        cache_store: _NodeContentCacheStore,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """handle_ringtone_response with valid response should store ringtone fragment."""
        runtime = _NodeContentResponseRuntime(
            mock_node_for_cache, cache_store=cache_store
        )

        raw = admin_pb2.AdminMessage()
        raw.get_ringtone_response = "RTTTL: test:d=4,o=5,b=100:c"

        packet: dict[str, Any] = {
            "decoded": {
                "admin": {"raw": raw},
            }
        }

        with caplog.at_level(logging.DEBUG):
            terminal, payload = runtime.handle_ringtone_response(packet)

        assert terminal is True
        assert payload == "RTTTL: test:d=4,o=5,b=100:c"
        assert "onResponseRequestRingtone" in caplog.text

    @pytest.mark.unit
    def test_handle_ringtone_response_with_routing_error_returns_true(
        self,
        mock_node_for_cache: MagicMock,
        cache_store: _NodeContentCacheStore,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """handle_ringtone_response with routing error should return True."""
        runtime = _NodeContentResponseRuntime(
            mock_node_for_cache, cache_store=cache_store
        )

        packet: dict[str, Any] = {
            "decoded": {
                "routing": {"errorReason": "NO_RESPONSE"},
            }
        }

        with caplog.at_level(logging.ERROR):
            terminal, payload = runtime.handle_ringtone_response(packet)

        assert terminal is True
        assert payload is None
        assert "Error on response: NO_RESPONSE" in caplog.text

    @pytest.mark.unit
    def test_handle_ringtone_response_missing_decoded_returns_true(
        self,
        mock_node_for_cache: MagicMock,
        cache_store: _NodeContentCacheStore,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """handle_ringtone_response with missing decoded should return True."""
        runtime = _NodeContentResponseRuntime(
            mock_node_for_cache, cache_store=cache_store
        )

        packet: dict[str, Any] = {"decoded": None}

        with caplog.at_level(logging.WARNING):
            terminal, payload = runtime.handle_ringtone_response(packet)

        assert terminal is True
        assert payload is None
        assert "Unexpected ringtone response without decoded payload" in caplog.text

    @pytest.mark.unit
    def test_handle_ringtone_response_missing_admin_returns_false(
        self,
        mock_node_for_cache: MagicMock,
        cache_store: _NodeContentCacheStore,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """handle_ringtone_response with missing admin should return terminal True."""
        runtime = _NodeContentResponseRuntime(
            mock_node_for_cache, cache_store=cache_store
        )

        packet: dict[str, Any] = {"decoded": {}}

        with caplog.at_level(logging.WARNING):
            terminal, payload = runtime.handle_ringtone_response(packet)

        assert terminal is True
        assert payload is None
        assert "Unexpected ringtone response without admin payload" in caplog.text

    @pytest.mark.unit
    def test_handle_ringtone_response_missing_raw_returns_false(
        self,
        mock_node_for_cache: MagicMock,
        cache_store: _NodeContentCacheStore,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """handle_ringtone_response with missing raw should return terminal True."""
        runtime = _NodeContentResponseRuntime(
            mock_node_for_cache, cache_store=cache_store
        )

        packet: dict[str, Any] = {"decoded": {"admin": {}}}

        with caplog.at_level(logging.WARNING):
            terminal, payload = runtime.handle_ringtone_response(packet)

        assert terminal is True
        assert payload is None
        assert "Unexpected ringtone response without raw ringtone data" in caplog.text

    @pytest.mark.unit
    def test_handle_canned_message_response_with_valid_response_stores_fragment(
        self,
        mock_node_for_cache: MagicMock,
        cache_store: _NodeContentCacheStore,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """handle_canned_message_response with valid response should store message fragment."""
        runtime = _NodeContentResponseRuntime(
            mock_node_for_cache, cache_store=cache_store
        )

        raw = admin_pb2.AdminMessage()
        raw.get_canned_message_module_messages_response = "Hello\nWorld"

        packet: dict[str, Any] = {
            "decoded": {
                "admin": {"raw": raw},
            }
        }

        with caplog.at_level(logging.DEBUG):
            terminal, payload = runtime.handle_canned_message_response(packet)

        assert terminal is True
        assert payload == "Hello\nWorld"
        assert "onResponseRequestCannedMessagePluginMessageMessages" in caplog.text

    @pytest.mark.unit
    def test_handle_canned_message_response_with_routing_error_returns_true(
        self,
        mock_node_for_cache: MagicMock,
        cache_store: _NodeContentCacheStore,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """handle_canned_message_response with routing error should return True."""
        runtime = _NodeContentResponseRuntime(
            mock_node_for_cache, cache_store=cache_store
        )

        packet: dict[str, Any] = {
            "decoded": {
                "routing": {"errorReason": "NO_RESPONSE"},
            }
        }

        with caplog.at_level(logging.ERROR):
            terminal, payload = runtime.handle_canned_message_response(packet)

        assert terminal is True
        assert payload is None
        assert "Error on response: NO_RESPONSE" in caplog.text

    @pytest.mark.unit
    def test_handle_canned_message_response_missing_decoded_returns_true(
        self,
        mock_node_for_cache: MagicMock,
        cache_store: _NodeContentCacheStore,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """handle_canned_message_response with missing decoded should return True."""
        runtime = _NodeContentResponseRuntime(
            mock_node_for_cache, cache_store=cache_store
        )

        packet: dict[str, Any] = {"decoded": None}

        with caplog.at_level(logging.WARNING):
            terminal, payload = runtime.handle_canned_message_response(packet)

        assert terminal is True
        assert payload is None
        assert (
            "Unexpected canned-message response without decoded payload" in caplog.text
        )

    @pytest.mark.unit
    def test_handle_canned_message_response_missing_admin_returns_false(
        self,
        mock_node_for_cache: MagicMock,
        cache_store: _NodeContentCacheStore,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """handle_canned_message_response with missing admin should return terminal True."""
        runtime = _NodeContentResponseRuntime(
            mock_node_for_cache, cache_store=cache_store
        )

        packet: dict[str, Any] = {"decoded": {}}

        with caplog.at_level(logging.WARNING):
            terminal, payload = runtime.handle_canned_message_response(packet)

        assert terminal is True
        assert payload is None
        assert "Unexpected canned-message response without admin payload" in caplog.text

    @pytest.mark.unit
    def test_handle_canned_message_response_missing_raw_returns_false(
        self,
        mock_node_for_cache: MagicMock,
        cache_store: _NodeContentCacheStore,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """handle_canned_message_response with missing raw should return terminal True."""
        runtime = _NodeContentResponseRuntime(
            mock_node_for_cache, cache_store=cache_store
        )

        packet: dict[str, Any] = {"decoded": {"admin": {}}}

        with caplog.at_level(logging.WARNING):
            terminal, payload = runtime.handle_canned_message_response(packet)

        assert terminal is True
        assert payload is None
        assert (
            "Unexpected canned-message response without raw message data" in caplog.text
        )


class TestNodeAdminContentRuntime:
    """Tests for _NodeAdminContentRuntime."""

    @pytest.fixture
    def cache_store(self, mock_node_for_admin: MagicMock) -> _NodeContentCacheStore:
        """Create a cache store for admin runtime tests."""
        return _NodeContentCacheStore(mock_node_for_admin)

    @pytest.fixture
    def response_runtime(
        self, mock_node_for_admin: MagicMock, cache_store: _NodeContentCacheStore
    ) -> _NodeContentResponseRuntime:
        """Create a response runtime for admin runtime tests."""
        return _NodeContentResponseRuntime(mock_node_for_admin, cache_store=cache_store)

    @pytest.mark.unit
    def test_module_available_or_warn_returns_true_when_available(
        self,
        mock_node_for_admin: MagicMock,
        cache_store: _NodeContentCacheStore,
        response_runtime: _NodeContentResponseRuntime,
    ) -> None:
        """_module_available_or_warn should return True when module is available."""
        mock_node_for_admin.module_available = MagicMock(return_value=True)
        runtime = _NodeAdminContentRuntime(
            mock_node_for_admin,
            cache_store=cache_store,
            response_runtime=response_runtime,
        )

        result = runtime._module_available_or_warn(0x01, "Module not present")

        assert result is True

    @pytest.mark.unit
    def test_module_available_or_warn_returns_false_and_logs_when_unavailable(
        self,
        mock_node_for_admin: MagicMock,
        cache_store: _NodeContentCacheStore,
        response_runtime: _NodeContentResponseRuntime,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """_module_available_or_warn should return False and log when module is unavailable."""
        mock_node_for_admin.module_available = MagicMock(return_value=False)
        runtime = _NodeAdminContentRuntime(
            mock_node_for_admin,
            cache_store=cache_store,
            response_runtime=response_runtime,
        )

        with caplog.at_level(logging.WARNING):
            result = runtime._module_available_or_warn(0x01, "Module not present")

        assert result is False
        assert "Module not present" in caplog.text

    @pytest.mark.unit
    def test_read_ringtone_returns_cached_value(
        self,
        mock_node_for_admin: MagicMock,
        cache_store: _NodeContentCacheStore,
        response_runtime: _NodeContentResponseRuntime,
    ) -> None:
        """read_ringtone should return cached value when available."""
        mock_node_for_admin.ringtone = "RTTTL: cached:d=4,o=5,b=100:c"
        runtime = _NodeAdminContentRuntime(
            mock_node_for_admin,
            cache_store=cache_store,
            response_runtime=response_runtime,
        )

        result = runtime.read_ringtone()

        assert result == "RTTTL: cached:d=4,o=5,b=100:c"
        # Should not call _send_admin since cached
        mock_node_for_admin._send_admin.assert_not_called()

    @pytest.mark.unit
    def test_read_ringtone_module_unavailable_returns_none(
        self,
        mock_node_for_admin: MagicMock,
        cache_store: _NodeContentCacheStore,
        response_runtime: _NodeContentResponseRuntime,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """read_ringtone should return None when module is unavailable."""
        mock_node_for_admin.module_available = MagicMock(return_value=False)
        runtime = _NodeAdminContentRuntime(
            mock_node_for_admin,
            cache_store=cache_store,
            response_runtime=response_runtime,
        )

        with caplog.at_level(logging.WARNING):
            result = runtime.read_ringtone()

        assert result is None
        assert "External Notification module not present" in caplog.text

    @pytest.mark.unit
    def test_read_ringtone_ignores_late_callback_after_timeout(
        self,
        mock_node_for_admin: MagicMock,
        cache_store: _NodeContentCacheStore,
        response_runtime: _NodeContentResponseRuntime,
    ) -> None:
        """Late callbacks from timed-out reads should not contaminate subsequent reads."""
        callback_by_call: dict[int, Any] = {}
        send_call_count = {"count": 0}

        def send_admin_side_effect(
            _message: admin_pb2.AdminMessage,
            wantResponse: bool = False,
            onResponse: Callable[[dict[str, Any]], Any] | None = None,
            adminIndex: int | None = None,
        ) -> object:
            _ = (wantResponse, adminIndex)
            send_call_count["count"] += 1
            assert onResponse is not None
            callback = onResponse
            callback_by_call[send_call_count["count"]] = callback
            if send_call_count["count"] == 2:
                callback(
                    {
                        "decoded": {
                            "routing": {"errorReason": "NO_RESPONSE"},
                        }
                    }
                )
            return MagicMock()

        mock_node_for_admin._timeout.expireTimeout = 0.0
        mock_node_for_admin._send_admin.side_effect = send_admin_side_effect
        runtime = _NodeAdminContentRuntime(
            mock_node_for_admin,
            cache_store=cache_store,
            response_runtime=response_runtime,
        )

        first_result = runtime.read_ringtone()
        assert first_result is None

        late_raw = admin_pb2.AdminMessage()
        late_raw.get_ringtone_response = "RTTTL: stale:d=4,o=5,b=100:c"
        callback_by_call[1]({"decoded": {"admin": {"raw": late_raw}}})
        assert mock_node_for_admin.ringtonePart is None

        second_result = runtime.read_ringtone()
        assert second_result is None
        assert mock_node_for_admin.ringtonePart is None

    @pytest.mark.unit
    def test_write_ringtone_valid_ringtone_calls_send_admin(
        self,
        mock_node_for_admin: MagicMock,
        cache_store: _NodeContentCacheStore,
        response_runtime: _NodeContentResponseRuntime,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """write_ringtone with valid ringtone should call _send_admin."""
        runtime = _NodeAdminContentRuntime(
            mock_node_for_admin,
            cache_store=cache_store,
            response_runtime=response_runtime,
        )
        ringtone = "RTTTL: test:d=4,o=5,b=100:c"

        with caplog.at_level(logging.DEBUG):
            result = runtime.write_ringtone(ringtone)

        assert result is not None
        mock_node_for_admin._send_admin.assert_called_once()
        mock_node_for_admin.ensureSessionKey.assert_called_once()
        assert "Setting ringtone" in caplog.text

    @pytest.mark.unit
    def test_write_ringtone_too_long_raises_error(
        self,
        mock_node_for_admin: MagicMock,
        cache_store: _NodeContentCacheStore,
        response_runtime: _NodeContentResponseRuntime,
    ) -> None:
        """write_ringtone with too-long ringtone should raise error."""
        runtime = _NodeAdminContentRuntime(
            mock_node_for_admin,
            cache_store=cache_store,
            response_runtime=response_runtime,
        )
        long_ringtone = "x" * (MAX_RINGTONE_LENGTH + 1)

        with pytest.raises(
            MeshInterface.MeshInterfaceError, match="interface error raised"
        ):
            runtime.write_ringtone(long_ringtone)

        mock_node_for_admin._raise_interface_error.assert_called_once()
        error_msg = mock_node_for_admin._raise_interface_error.call_args[0][0]
        assert f"{MAX_RINGTONE_LENGTH} characters" in error_msg

    @pytest.mark.unit
    def test_write_ringtone_module_unavailable_returns_none(
        self,
        mock_node_for_admin: MagicMock,
        cache_store: _NodeContentCacheStore,
        response_runtime: _NodeContentResponseRuntime,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """write_ringtone should return None when module is unavailable."""
        mock_node_for_admin.module_available = MagicMock(return_value=False)
        runtime = _NodeAdminContentRuntime(
            mock_node_for_admin,
            cache_store=cache_store,
            response_runtime=response_runtime,
        )

        with caplog.at_level(logging.WARNING):
            result = runtime.write_ringtone("RTTTL: test:d=4,o=5,b=100:c")

        assert result is None
        assert "External Notification module not present" in caplog.text

    @pytest.mark.unit
    def test_write_ringtone_invalidates_cache(
        self,
        mock_node_for_admin: MagicMock,
        cache_store: _NodeContentCacheStore,
        response_runtime: _NodeContentResponseRuntime,
    ) -> None:
        """write_ringtone should invalidate ringtone cache on success."""
        mock_node_for_admin.ringtone = "old_ringtone"
        mock_node_for_admin.ringtonePart = "old_fragment"
        runtime = _NodeAdminContentRuntime(
            mock_node_for_admin,
            cache_store=cache_store,
            response_runtime=response_runtime,
        )

        runtime.write_ringtone("RTTTL: new:d=4,o=5,b=100:c")

        assert mock_node_for_admin.ringtone is None
        assert mock_node_for_admin.ringtonePart is None

    @pytest.mark.unit
    def test_write_ringtone_send_returns_none_skips_invalidation(
        self,
        mock_node_for_admin: MagicMock,
        cache_store: _NodeContentCacheStore,
        response_runtime: _NodeContentResponseRuntime,
    ) -> None:
        """write_ringtone should not invalidate cache when _send_admin returns None."""
        mock_node_for_admin._send_admin = MagicMock(return_value=None)
        mock_node_for_admin.ringtone = "old_ringtone"
        runtime = _NodeAdminContentRuntime(
            mock_node_for_admin,
            cache_store=cache_store,
            response_runtime=response_runtime,
        )

        runtime.write_ringtone("RTTTL: new:d=4,o=5,b=100:c")

        # Cache should not be invalidated
        assert mock_node_for_admin.ringtone == "old_ringtone"

    @pytest.mark.unit
    def test_read_canned_message_returns_cached_value(
        self,
        mock_node_for_admin: MagicMock,
        cache_store: _NodeContentCacheStore,
        response_runtime: _NodeContentResponseRuntime,
    ) -> None:
        """read_canned_message should return cached value when available."""
        mock_node_for_admin.cannedPluginMessage = "Hello\nWorld"
        runtime = _NodeAdminContentRuntime(
            mock_node_for_admin,
            cache_store=cache_store,
            response_runtime=response_runtime,
        )

        result = runtime.read_canned_message()

        assert result == "Hello\nWorld"
        # Should not call _send_admin since cached
        mock_node_for_admin._send_admin.assert_not_called()

    @pytest.mark.unit
    def test_read_canned_message_module_unavailable_returns_none(
        self,
        mock_node_for_admin: MagicMock,
        cache_store: _NodeContentCacheStore,
        response_runtime: _NodeContentResponseRuntime,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """read_canned_message should return None when module is unavailable."""
        mock_node_for_admin.module_available = MagicMock(return_value=False)
        runtime = _NodeAdminContentRuntime(
            mock_node_for_admin,
            cache_store=cache_store,
            response_runtime=response_runtime,
        )

        with caplog.at_level(logging.WARNING):
            result = runtime.read_canned_message()

        assert result is None
        assert "Canned Message module not present" in caplog.text

    @pytest.mark.unit
    def test_write_canned_message_valid_message_calls_send_admin(
        self,
        mock_node_for_admin: MagicMock,
        cache_store: _NodeContentCacheStore,
        response_runtime: _NodeContentResponseRuntime,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """write_canned_message with valid message should call _send_admin."""
        runtime = _NodeAdminContentRuntime(
            mock_node_for_admin,
            cache_store=cache_store,
            response_runtime=response_runtime,
        )
        message = "Hello\nWorld"

        with caplog.at_level(logging.DEBUG):
            result = runtime.write_canned_message(message)

        assert result is not None
        mock_node_for_admin._send_admin.assert_called_once()
        mock_node_for_admin.ensureSessionKey.assert_called_once()
        assert "Setting canned message" in caplog.text

    @pytest.mark.unit
    def test_write_canned_message_too_long_raises_error(
        self,
        mock_node_for_admin: MagicMock,
        cache_store: _NodeContentCacheStore,
        response_runtime: _NodeContentResponseRuntime,
    ) -> None:
        """write_canned_message with too-long message should raise error."""
        runtime = _NodeAdminContentRuntime(
            mock_node_for_admin,
            cache_store=cache_store,
            response_runtime=response_runtime,
        )
        long_message = "x" * (MAX_CANNED_MESSAGE_LENGTH + 1)

        with pytest.raises(
            MeshInterface.MeshInterfaceError, match="interface error raised"
        ):
            runtime.write_canned_message(long_message)

        mock_node_for_admin._raise_interface_error.assert_called_once()
        error_msg = mock_node_for_admin._raise_interface_error.call_args[0][0]
        assert f"{MAX_CANNED_MESSAGE_LENGTH} characters" in error_msg

    @pytest.mark.unit
    def test_write_canned_message_module_unavailable_returns_none(
        self,
        mock_node_for_admin: MagicMock,
        cache_store: _NodeContentCacheStore,
        response_runtime: _NodeContentResponseRuntime,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """write_canned_message should return None when module is unavailable."""
        mock_node_for_admin.module_available = MagicMock(return_value=False)
        runtime = _NodeAdminContentRuntime(
            mock_node_for_admin,
            cache_store=cache_store,
            response_runtime=response_runtime,
        )

        with caplog.at_level(logging.WARNING):
            result = runtime.write_canned_message("Hello\nWorld")

        assert result is None
        assert "Canned Message module not present" in caplog.text

    @pytest.mark.unit
    def test_write_canned_message_invalidates_cache(
        self,
        mock_node_for_admin: MagicMock,
        cache_store: _NodeContentCacheStore,
        response_runtime: _NodeContentResponseRuntime,
    ) -> None:
        """write_canned_message should invalidate canned message cache on success."""
        mock_node_for_admin.cannedPluginMessage = "old_message"
        mock_node_for_admin.cannedPluginMessageMessages = "old_fragment"
        runtime = _NodeAdminContentRuntime(
            mock_node_for_admin,
            cache_store=cache_store,
            response_runtime=response_runtime,
        )

        runtime.write_canned_message("New message")

        assert mock_node_for_admin.cannedPluginMessage is None
        assert mock_node_for_admin.cannedPluginMessageMessages is None

    @pytest.mark.unit
    def test_write_canned_message_send_returns_none_skips_invalidation(
        self,
        mock_node_for_admin: MagicMock,
        cache_store: _NodeContentCacheStore,
        response_runtime: _NodeContentResponseRuntime,
    ) -> None:
        """write_canned_message should not invalidate cache when _send_admin returns None."""
        mock_node_for_admin._send_admin = MagicMock(return_value=None)
        mock_node_for_admin.cannedPluginMessage = "old_message"
        runtime = _NodeAdminContentRuntime(
            mock_node_for_admin,
            cache_store=cache_store,
            response_runtime=response_runtime,
        )

        runtime.write_canned_message("New message")

        # Cache should not be invalidated
        assert mock_node_for_admin.cannedPluginMessage == "old_message"

    @pytest.mark.unit
    def test_select_write_response_handler_local_node_returns_none(
        self,
        mock_node_for_admin: MagicMock,
        cache_store: _NodeContentCacheStore,
        response_runtime: _NodeContentResponseRuntime,
    ) -> None:
        """_select_write_response_handler should return None for local node."""
        mock_node_for_admin.iface.localNode = mock_node_for_admin
        runtime = _NodeAdminContentRuntime(
            mock_node_for_admin,
            cache_store=cache_store,
            response_runtime=response_runtime,
        )

        result = runtime._select_write_response_handler()

        assert result is None

    @pytest.mark.unit
    def test_select_write_response_handler_remote_node_returns_onAckNak(
        self,
        mock_node_for_admin: MagicMock,
        cache_store: _NodeContentCacheStore,
        response_runtime: _NodeContentResponseRuntime,
    ) -> None:
        """_select_write_response_handler should return onAckNak for remote node."""
        local_node = MagicMock()
        mock_node_for_admin.iface.localNode = local_node
        runtime = _NodeAdminContentRuntime(
            mock_node_for_admin,
            cache_store=cache_store,
            response_runtime=response_runtime,
        )

        result = runtime._select_write_response_handler()

        assert result == mock_node_for_admin.onAckNak

    @pytest.mark.unit
    def test_send_content_read_request_returns_none_when_send_fails(
        self,
        mock_node_for_admin: MagicMock,
        cache_store: _NodeContentCacheStore,
        response_runtime: _NodeContentResponseRuntime,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """_send_content_read_request should return None when _send_admin returns None."""
        mock_node_for_admin._send_admin = MagicMock(return_value=None)
        runtime = _NodeAdminContentRuntime(
            mock_node_for_admin,
            cache_store=cache_store,
            response_runtime=response_runtime,
        )

        with caplog.at_level(logging.DEBUG):
            result = runtime._send_content_read_request(
                begin_read_generation=lambda: 1,
                is_read_generation_active=lambda _generation: True,
                retire_read_generation=lambda _generation: None,
                build_request=lambda _msg: None,
                handle_response=lambda _pkt: (True, None),
                commit_payload=lambda _payload: None,
                skipped_send_debug_message="Send was not started",
                timeout_warning_message="Timed out",
                resolve_result=lambda: "result",
            )

        assert result is None
        assert "Send was not started" in caplog.text

    @pytest.mark.unit
    def test_send_content_read_request_times_out(
        self,
        mock_node_for_admin: MagicMock,
        cache_store: _NodeContentCacheStore,
        response_runtime: _NodeContentResponseRuntime,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """_send_content_read_request should return None when waiting times out."""
        mock_node_for_admin._timeout.expireTimeout = 0
        runtime = _NodeAdminContentRuntime(
            mock_node_for_admin,
            cache_store=cache_store,
            response_runtime=response_runtime,
        )

        with caplog.at_level(logging.WARNING):
            result = runtime._send_content_read_request(
                begin_read_generation=lambda: 1,
                is_read_generation_active=lambda _generation: True,
                retire_read_generation=lambda _generation: None,
                build_request=lambda _msg: None,
                handle_response=lambda _pkt: (True, None),
                commit_payload=lambda _payload: None,
                skipped_send_debug_message="Send was not started",
                timeout_warning_message="Timed out waiting for response",
                resolve_result=lambda: "result",
            )

        assert result is None
        assert "Timed out waiting for response" in caplog.text
