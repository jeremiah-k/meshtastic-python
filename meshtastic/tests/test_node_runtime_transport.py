"""Unit tests for transport_runtime module.

Tests for admin transport, channel write/delete, ACK/NAK, and position/time command runtimes.
"""

import logging
import threading
import time as time_module
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from meshtastic.node_runtime.shared import MAX_CHANNELS
from meshtastic.node_runtime.transport_runtime import (
    _DeleteChannelRewritePlan,
    _NodeAckNakRuntime,
    _NodeAdminSessionRuntime,
    _NodeAdminTransportRuntime,
    _NodeChannelWriteRuntime,
    _NodeDeleteChannelRuntime,
    _NodePositionTimeCommandRuntime,
)
from meshtastic.protobuf import admin_pb2, channel_pb2, mesh_pb2, portnums_pb2
from meshtastic.util import Acknowledgment

# ============================================================================
# Fixtures
# ============================================================================


@pytest.fixture
def mock_iface() -> MagicMock:
    """Create a minimal mock interface with acknowledgment support.

    Returns
    -------
    MagicMock
        A mock interface configured with _acknowledgment and sendData method.
    """
    iface = MagicMock(
        spec=[
            "_acknowledgment",
            "sendData",
            "_get_or_create_by_num",
            "localNode",
            "waitForAckNak",
        ]
    )
    iface._acknowledgment = Acknowledgment()
    iface.sendData = MagicMock(return_value=mesh_pb2.MeshPacket())
    iface._get_or_create_by_num = MagicMock(return_value={})
    iface.waitForAckNak = MagicMock()
    return iface


@pytest.fixture
def mock_local_node(mock_iface: MagicMock) -> MagicMock:
    """Create a mock local node for testing.

    Parameters
    ----------
    mock_iface : MagicMock
        The mock interface fixture.

    Returns
    -------
    MagicMock
        A mock node configured as a local node.
    """
    node = MagicMock(
        spec=[
            "nodeNum",
            "iface",
            "noProto",
            "requestConfig",
            "_send_admin",
            "ensureSessionKey",
            "_channels_lock",
            "channels",
            "partialChannels",
            "_raise_interface_error",
            "onAckNak",
            "_get_admin_channel_index",
            "_fixup_channels_locked",
        ]
    )
    node.nodeNum = 1234567890
    node.iface = mock_iface
    node.noProto = False
    node.requestConfig = MagicMock()
    node._send_admin = MagicMock(return_value=mesh_pb2.MeshPacket())
    node.ensureSessionKey = MagicMock()
    node._channels_lock = threading.RLock()
    node.channels = None
    node.partialChannels = []
    node._raise_interface_error = MagicMock(side_effect=Exception("interface error"))
    node.onAckNak = MagicMock()
    node._get_admin_channel_index = MagicMock(return_value=0)
    node._fixup_channels_locked = MagicMock()

    # Set up iface.localNode to return this node (making it a local node)
    mock_iface.localNode = node
    return node


@pytest.fixture
def mock_remote_node(mock_iface: MagicMock) -> MagicMock:
    """Create a mock remote node for testing.

    Parameters
    ----------
    mock_iface : MagicMock
        The mock interface fixture.

    Returns
    -------
    MagicMock
        A mock node configured as a remote node (different from localNode).
    """
    node = MagicMock(
        spec=[
            "nodeNum",
            "iface",
            "noProto",
            "requestConfig",
            "_send_admin",
            "ensureSessionKey",
            "_channels_lock",
            "channels",
            "partialChannels",
            "_raise_interface_error",
            "onAckNak",
            "_get_admin_channel_index",
        ]
    )
    node.nodeNum = 4000000000  # Different from local node, within 32-bit range
    node.iface = mock_iface
    node.noProto = False
    node.requestConfig = MagicMock()
    node._send_admin = MagicMock(return_value=mesh_pb2.MeshPacket())
    node.ensureSessionKey = MagicMock()
    node._channels_lock = threading.RLock()
    node.channels = None
    node.partialChannels = []
    node._raise_interface_error = MagicMock(side_effect=Exception("interface error"))
    node.onAckNak = MagicMock()
    node._get_admin_channel_index = MagicMock(return_value=0)
    return node


# ============================================================================
# Tests for _NodeAdminSessionRuntime
# ============================================================================


class TestNodeAdminSessionRuntime:
    """Tests for _NodeAdminSessionRuntime."""

    @pytest.mark.unit
    def test_ensure_session_key_when_noproto_logs_warning(
        self, mock_local_node: MagicMock, caplog: pytest.LogCaptureFixture
    ) -> None:
        """ensure_session_key should log warning and return early when noProto is True."""
        mock_local_node.noProto = True
        runtime = _NodeAdminSessionRuntime(mock_local_node)

        with caplog.at_level(logging.WARNING):
            runtime.ensure_session_key()

        assert "Not ensuring session key" in caplog.text
        mock_local_node.requestConfig.assert_not_called()

    @pytest.mark.unit
    def test_ensure_session_key_requests_key_when_missing(
        self, mock_local_node: MagicMock
    ) -> None:
        """ensure_session_key should request session key when adminSessionPassKey is None."""
        mock_local_node.iface._get_or_create_by_num.return_value = {
            "adminSessionPassKey": None
        }
        runtime = _NodeAdminSessionRuntime(mock_local_node)

        runtime.ensure_session_key()

        mock_local_node.requestConfig.assert_called_once_with(
            admin_pb2.AdminMessage.SESSIONKEY_CONFIG, adminIndex=None
        )

    @pytest.mark.unit
    def test_ensure_session_key_skips_when_key_present(
        self, mock_local_node: MagicMock
    ) -> None:
        """ensure_session_key should not request key when adminSessionPassKey is present."""
        mock_local_node.iface._get_or_create_by_num.return_value = {
            "adminSessionPassKey": b"test_key"
        }
        runtime = _NodeAdminSessionRuntime(mock_local_node)

        runtime.ensure_session_key()

        mock_local_node.requestConfig.assert_not_called()

    @pytest.mark.unit
    def test_ensure_session_key_passes_admin_index(
        self, mock_local_node: MagicMock
    ) -> None:
        """ensure_session_key should pass admin_index to requestConfig."""
        mock_local_node.iface._get_or_create_by_num.return_value = {
            "adminSessionPassKey": None
        }
        runtime = _NodeAdminSessionRuntime(mock_local_node)

        runtime.ensure_session_key(admin_index=5)

        mock_local_node.requestConfig.assert_called_once_with(
            admin_pb2.AdminMessage.SESSIONKEY_CONFIG, adminIndex=5
        )


# ============================================================================
# Tests for _NodeAdminTransportRuntime
# ============================================================================


class TestNodeAdminTransportRuntime:
    """Tests for _NodeAdminTransportRuntime."""

    @pytest.mark.unit
    def test_send_admin_when_noproto_logs_warning(
        self, mock_local_node: MagicMock, caplog: pytest.LogCaptureFixture
    ) -> None:
        """send_admin should log warning and return None when noProto is True."""
        mock_local_node.noProto = True
        runtime = _NodeAdminTransportRuntime(mock_local_node)

        with caplog.at_level(logging.WARNING):
            result = runtime.send_admin(admin_pb2.AdminMessage())

        assert "Not sending packet" in caplog.text
        assert result is None

    @pytest.mark.unit
    def test_send_admin_resolves_admin_index_to_local_node_index(
        self, mock_local_node: MagicMock
    ) -> None:
        """send_admin should resolve None admin_index to localNode's admin channel index."""
        mock_local_node.iface.localNode._get_admin_channel_index.return_value = 3
        runtime = _NodeAdminTransportRuntime(mock_local_node)

        runtime.send_admin(admin_pb2.AdminMessage())

        # Verify the resolved admin index was used
        call_kwargs = mock_local_node.iface.sendData.call_args[1]
        assert call_kwargs["channelIndex"] == 3

    @pytest.mark.unit
    def test_send_admin_uses_explicit_admin_index(
        self, mock_local_node: MagicMock
    ) -> None:
        """send_admin should use explicit admin_index when provided."""
        runtime = _NodeAdminTransportRuntime(mock_local_node)

        runtime.send_admin(admin_pb2.AdminMessage(), admin_index=7)

        call_kwargs = mock_local_node.iface.sendData.call_args[1]
        assert call_kwargs["channelIndex"] == 7

    @pytest.mark.unit
    def test_send_admin_includes_session_passkey(
        self, mock_local_node: MagicMock
    ) -> None:
        """send_admin should include session_passkey in message when available."""
        mock_local_node.iface._get_or_create_by_num.return_value = {
            "adminSessionPassKey": b"test_passkey"
        }
        runtime = _NodeAdminTransportRuntime(mock_local_node)

        message = admin_pb2.AdminMessage()
        runtime.send_admin(message)

        assert message.session_passkey == b"test_passkey"

    @pytest.mark.unit
    def test_send_admin_passes_correct_parameters_to_senddata(
        self, mock_local_node: MagicMock
    ) -> None:
        """send_admin should pass correct parameters to sendData."""
        runtime = _NodeAdminTransportRuntime(mock_local_node)
        message = admin_pb2.AdminMessage()

        runtime.send_admin(
            message,
            want_response=True,
            on_response=lambda x: None,
            admin_index=2,
        )

        call_kwargs = mock_local_node.iface.sendData.call_args[1]
        assert call_kwargs["portNum"] == portnums_pb2.PortNum.ADMIN_APP
        assert call_kwargs["wantAck"] is True
        assert call_kwargs["wantResponse"] is True
        assert call_kwargs["onResponse"] is not None
        assert call_kwargs["channelIndex"] == 2
        assert call_kwargs["pkiEncrypted"] is True


# ============================================================================
# Tests for _NodeChannelWriteRuntime
# ============================================================================


class TestNodeChannelWriteRuntime:
    """Tests for _NodeChannelWriteRuntime."""

    @pytest.fixture
    def channel_write_runtime(
        self, mock_local_node: MagicMock
    ) -> _NodeChannelWriteRuntime:
        """Create a channel write runtime instance for testing.

        Parameters
        ----------
        mock_local_node : MagicMock
            The mock local node fixture.

        Returns
        -------
        _NodeChannelWriteRuntime
            A channel write runtime instance.
        """
        session_runtime = _NodeAdminSessionRuntime(mock_local_node)
        transport_runtime = _NodeAdminTransportRuntime(mock_local_node)
        return _NodeChannelWriteRuntime(
            mock_local_node,
            admin_session_runtime=session_runtime,
            admin_transport_runtime=transport_runtime,
        )

    @pytest.mark.unit
    def test_write_channel_snapshot_calls_ensure_session_key(
        self,
        channel_write_runtime: _NodeChannelWriteRuntime,
        mock_local_node: MagicMock,
    ) -> None:
        """write_channel_snapshot should call ensureSessionKey."""
        channel = channel_pb2.Channel()
        channel.index = 0

        channel_write_runtime.write_channel_snapshot(channel)

        mock_local_node.ensureSessionKey.assert_called_once()

    @pytest.mark.unit
    def test_write_channel_snapshot_sends_set_channel(
        self,
        channel_write_runtime: _NodeChannelWriteRuntime,
        mock_local_node: MagicMock,
    ) -> None:
        """write_channel_snapshot should send set_channel admin message."""
        channel = channel_pb2.Channel()
        channel.index = 0
        channel.role = channel_pb2.Channel.Role.PRIMARY

        channel_write_runtime.write_channel_snapshot(channel)

        mock_local_node._send_admin.assert_called_once()
        call_args = mock_local_node._send_admin.call_args
        sent_message = call_args[0][0]
        assert isinstance(sent_message, admin_pb2.AdminMessage)
        assert sent_message.set_channel.index == 0

    @pytest.mark.unit
    def test_write_channel_snapshot_raises_when_send_not_started(
        self,
        channel_write_runtime: _NodeChannelWriteRuntime,
        mock_local_node: MagicMock,
    ) -> None:
        """write_channel_snapshot should raise when _send_admin skips sending."""
        channel = channel_pb2.Channel()
        channel.index = 4
        mock_local_node._send_admin.return_value = None

        def _raise_error(msg: str) -> None:
            raise ValueError(msg)

        mock_local_node._raise_interface_error = MagicMock(side_effect=_raise_error)

        with pytest.raises(
            ValueError,
            match="Channel write for index 4 was not started",
        ):
            channel_write_runtime.write_channel_snapshot(channel)

    @pytest.mark.unit
    def test_write_channel_validates_channel_index(
        self,
        channel_write_runtime: _NodeChannelWriteRuntime,
        mock_local_node: MagicMock,
    ) -> None:
        """write_channel should raise error for out-of-range channel index."""
        # Set up channels
        mock_channel = channel_pb2.Channel()
        mock_channel.index = 0
        mock_local_node.channels = [mock_channel]

        with pytest.raises(Exception, match="interface error"):
            channel_write_runtime.write_channel(5)  # Out of range

    @pytest.mark.unit
    def test_write_channel_validates_channels_exist(
        self,
        channel_write_runtime: _NodeChannelWriteRuntime,
        mock_local_node: MagicMock,
    ) -> None:
        """write_channel should raise error when no channels have been read."""
        mock_local_node.channels = None

        with pytest.raises(Exception, match="interface error"):
            channel_write_runtime.write_channel(0)


# ============================================================================
# Tests for _NodeDeleteChannelRuntime
# ============================================================================


class TestNodeDeleteChannelRuntime:
    """Tests for _NodeDeleteChannelRuntime."""

    @pytest.fixture
    def delete_channel_runtime(
        self, mock_local_node: MagicMock
    ) -> _NodeDeleteChannelRuntime:
        """Create a delete channel runtime instance for testing.

        Parameters
        ----------
        mock_local_node : MagicMock
            The mock local node fixture.

        Returns
        -------
        _NodeDeleteChannelRuntime
            A delete channel runtime instance.
        """
        session_runtime = _NodeAdminSessionRuntime(mock_local_node)
        transport_runtime = _NodeAdminTransportRuntime(mock_local_node)
        channel_write_runtime = _NodeChannelWriteRuntime(
            mock_local_node,
            admin_session_runtime=session_runtime,
            admin_transport_runtime=transport_runtime,
        )
        return _NodeDeleteChannelRuntime(
            mock_local_node, channel_write_runtime=channel_write_runtime
        )

    @pytest.mark.unit
    def test_named_admin_index_from_channels_finds_admin_channel(self) -> None:
        """_named_admin_index_from_channels should find the named admin channel."""
        channels = []
        for i in range(MAX_CHANNELS):
            ch = channel_pb2.Channel()
            ch.index = i
            ch.role = channel_pb2.Channel.Role.DISABLED
            channels.append(ch)

        # Set channel 2 as admin channel
        channels[2].role = channel_pb2.Channel.Role.SECONDARY
        channels[2].settings.name = "admin"

        result = _NodeDeleteChannelRuntime._named_admin_index_from_channels(channels)
        assert result == 2

    @pytest.mark.unit
    def test_named_admin_index_from_channels_returns_zero_when_not_found(self) -> None:
        """_named_admin_index_from_channels should return 0 when no admin channel found."""
        channels = []
        for i in range(MAX_CHANNELS):
            ch = channel_pb2.Channel()
            ch.index = i
            ch.role = channel_pb2.Channel.Role.DISABLED
            channels.append(ch)

        result = _NodeDeleteChannelRuntime._named_admin_index_from_channels(channels)
        assert result == 0

    @pytest.mark.unit
    def test_normalize_staged_channels_truncates_to_max_channels(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """_normalize_staged_channels should truncate channels exceeding MAX_CHANNELS (line 191-196)."""
        # Create more channels than MAX_CHANNELS
        channels = []
        for i in range(MAX_CHANNELS + 3):  # 11 channels
            ch = channel_pb2.Channel()
            ch.index = i
            ch.role = channel_pb2.Channel.Role.SECONDARY
            channels.append(ch)

        with caplog.at_level(logging.WARNING):
            _NodeDeleteChannelRuntime._normalize_staged_channels(channels)

        # Should be truncated to MAX_CHANNELS
        assert len(channels) == MAX_CHANNELS
        # Should log warning about truncation (line 191)
        assert "Truncating channel list" in caplog.text
        assert str(MAX_CHANNELS + 3) in caplog.text

    @pytest.mark.unit
    def test_normalize_staged_channels_reindexes_channels(self) -> None:
        """_normalize_staged_channels should reindex all channels."""
        channels = []
        for i in range(3):
            ch = channel_pb2.Channel()
            ch.index = i + 10  # Wrong index
            ch.role = channel_pb2.Channel.Role.SECONDARY
            channels.append(ch)

        _NodeDeleteChannelRuntime._normalize_staged_channels(channels)

        # Should have MAX_CHANNELS entries
        assert len(channels) == MAX_CHANNELS
        # First 3 should be reindexed
        for i in range(3):
            assert channels[i].index == i
        # Remaining should be DISABLED
        for i in range(3, MAX_CHANNELS):
            assert channels[i].role == channel_pb2.Channel.Role.DISABLED

    @pytest.mark.unit
    def test_build_rewrite_plan_local_node_uses_named_admin_index(
        self,
        delete_channel_runtime: _NodeDeleteChannelRuntime,
        mock_local_node: MagicMock,
    ) -> None:
        """_build_rewrite_plan for local node should use _named_admin_index_from_channels (line 231)."""
        # Set up channels with PRIMARY at 0, SECONDARY at 1 (to delete), admin at 2
        channels = []
        for i in range(MAX_CHANNELS):
            ch = channel_pb2.Channel()
            ch.index = i
            if i == 0:
                ch.role = channel_pb2.Channel.Role.PRIMARY
            elif i == 1:
                ch.role = channel_pb2.Channel.Role.SECONDARY  # To be deleted
            elif i == 2:
                ch.role = channel_pb2.Channel.Role.SECONDARY
                ch.settings.name = "admin"  # Named admin channel
            else:
                ch.role = channel_pb2.Channel.Role.DISABLED
            channels.append(ch)

        mock_local_node.channels = channels

        plan = delete_channel_runtime._build_rewrite_plan(1)  # Delete channel 1

        # Pre-delete admin index should be 2 (the named admin channel)
        assert plan.pre_delete_admin_index == 2

    @pytest.mark.unit
    def test_build_rewrite_plan_remote_node_uses_localnode_getadminchannelindex(
        self, mock_remote_node: MagicMock, mock_iface: MagicMock
    ) -> None:
        """_build_rewrite_plan for remote node should use localNode.getAdminChannelIndex() (line 233, 256)."""
        # Set up local node with admin channel index
        mock_local = MagicMock()
        mock_local.getAdminChannelIndex = MagicMock(return_value=4)
        mock_iface.localNode = mock_local

        # Set up remote node channels
        channels = []
        for i in range(MAX_CHANNELS):
            ch = channel_pb2.Channel()
            ch.index = i
            if i == 0:
                ch.role = channel_pb2.Channel.Role.PRIMARY
            elif i == 1:
                ch.role = channel_pb2.Channel.Role.SECONDARY  # To be deleted
            else:
                ch.role = channel_pb2.Channel.Role.DISABLED
            channels.append(ch)

        mock_remote_node.channels = channels

        # Create runtime for remote node
        session_runtime = _NodeAdminSessionRuntime(mock_remote_node)
        transport_runtime = _NodeAdminTransportRuntime(mock_remote_node)
        channel_write_runtime = _NodeChannelWriteRuntime(
            mock_remote_node,
            admin_session_runtime=session_runtime,
            admin_transport_runtime=transport_runtime,
        )
        delete_runtime = _NodeDeleteChannelRuntime(
            mock_remote_node, channel_write_runtime=channel_write_runtime
        )

        plan = delete_runtime._build_rewrite_plan(1)  # Delete channel 1

        # Pre-delete and post-delete admin index should come from localNode.getAdminChannelIndex()
        mock_local.getAdminChannelIndex.assert_called()
        assert plan.pre_delete_admin_index == 4
        assert plan.post_delete_admin_index == 4

    @pytest.mark.unit
    def test_build_rewrite_plan_validates_channel_role(
        self,
        delete_channel_runtime: _NodeDeleteChannelRuntime,
        mock_local_node: MagicMock,
    ) -> None:
        """_build_rewrite_plan should raise error for PRIMARY channel deletion."""
        channels = []
        for i in range(MAX_CHANNELS):
            ch = channel_pb2.Channel()
            ch.index = i
            if i == 0:
                ch.role = channel_pb2.Channel.Role.PRIMARY
            else:
                ch.role = channel_pb2.Channel.Role.DISABLED
            channels.append(ch)

        mock_local_node.channels = channels

        with pytest.raises(Exception, match="interface error"):
            delete_channel_runtime._build_rewrite_plan(0)  # Try to delete PRIMARY

    @pytest.mark.unit
    def test_delete_channel_executes_rewrite_plan(
        self,
        delete_channel_runtime: _NodeDeleteChannelRuntime,
        mock_local_node: MagicMock,
    ) -> None:
        """delete_channel should execute rewrite plan and update channels."""
        # Set up channels
        channels = []
        for i in range(MAX_CHANNELS):
            ch = channel_pb2.Channel()
            ch.index = i
            if i == 0:
                ch.role = channel_pb2.Channel.Role.PRIMARY
            elif i == 1:
                ch.role = channel_pb2.Channel.Role.SECONDARY
            else:
                ch.role = channel_pb2.Channel.Role.DISABLED
            channels.append(ch)

        mock_local_node.channels = channels

        # Patch write_channel_snapshot to avoid actual I/O
        with patch.object(
            delete_channel_runtime._channel_write_runtime, "write_channel_snapshot"
        ):
            delete_channel_runtime.delete_channel(1)

        # Verify channels were updated
        assert mock_local_node.channels is not None
        # Channel 1 should now be DISABLED (shifted from index 2)
        assert mock_local_node.channels[1].role == channel_pb2.Channel.Role.DISABLED

    @pytest.mark.unit
    def test_delete_channel_switches_admin_index_after_slot_rewrite(
        self,
        delete_channel_runtime: _NodeDeleteChannelRuntime,
        mock_local_node: MagicMock,
    ) -> None:
        """delete_channel should switch admin_index after admin slot rewrite (line 281)."""
        # Set up channels where admin channel is at index 2, and we delete index 1
        # This means pre_delete_admin_index >= channel_index, so switch_after_admin_slot_rewrite is True
        channels = []
        for i in range(MAX_CHANNELS):
            ch = channel_pb2.Channel()
            ch.index = i
            if i == 0:
                ch.role = channel_pb2.Channel.Role.PRIMARY
            elif i == 1:
                ch.role = channel_pb2.Channel.Role.SECONDARY  # To delete
            elif i == 2:
                ch.role = channel_pb2.Channel.Role.SECONDARY
                ch.settings.name = "admin"  # Named admin channel
            else:
                ch.role = channel_pb2.Channel.Role.DISABLED
            channels.append(ch)

        mock_local_node.channels = channels

        write_calls: list[tuple[int, int | None]] = []
        with patch.object(
            delete_channel_runtime._channel_write_runtime,
            "write_channel_snapshot",
            side_effect=lambda ch, **kw: write_calls.append(
                (ch.index, kw.get("admin_index"))
            ),
        ):
            delete_channel_runtime.delete_channel(1)

        # Verify admin_index switches after writing the admin slot (index 2)
        # After delete, admin channel moves to index 1, so we should see switch after index 2 write
        pre_delete_admin = 2
        post_delete_admin = 1  # Admin channel moves down after delete

        # Check that we have writes with different admin indices
        admin_indices = [admin_idx for _, admin_idx in write_calls]
        assert pre_delete_admin in admin_indices
        assert post_delete_admin in admin_indices

    @pytest.mark.unit
    def test_delete_channel_handles_cache_unavailable(
        self,
        delete_channel_runtime: _NodeDeleteChannelRuntime,
        mock_local_node: MagicMock,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """delete_channel should handle channel cache becoming unavailable (lines 290-295)."""
        # Set up channels
        channels = []
        for i in range(MAX_CHANNELS):
            ch = channel_pb2.Channel()
            ch.index = i
            if i == 0:
                ch.role = channel_pb2.Channel.Role.PRIMARY
            elif i == 1:
                ch.role = channel_pb2.Channel.Role.SECONDARY
            else:
                ch.role = channel_pb2.Channel.Role.DISABLED
            channels.append(ch)

        mock_local_node.channels = channels

        def make_cache_none(*_args: Any, **_kwargs: Any) -> None:
            mock_local_node.channels = None

        with (
            caplog.at_level(logging.WARNING),
            patch.object(
                delete_channel_runtime._channel_write_runtime,
                "write_channel_snapshot",
                side_effect=make_cache_none,
            ),
        ):
            delete_channel_runtime.delete_channel(1)

        assert "Channel cache became unavailable" in caplog.text
        assert mock_local_node.channels is None
        assert mock_local_node.partialChannels == []

    @pytest.mark.unit
    def test_delete_channel_handles_cache_changed(
        self,
        delete_channel_runtime: _NodeDeleteChannelRuntime,
        mock_local_node: MagicMock,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """delete_channel should handle channel cache changing during delete (lines 297-302)."""
        # Set up channels
        channels = []
        for i in range(MAX_CHANNELS):
            ch = channel_pb2.Channel()
            ch.index = i
            if i == 0:
                ch.role = channel_pb2.Channel.Role.PRIMARY
            elif i == 1:
                ch.role = channel_pb2.Channel.Role.SECONDARY
            else:
                ch.role = channel_pb2.Channel.Role.DISABLED
            channels.append(ch)

        mock_local_node.channels = channels

        def change_cache(*_args: Any, **_kwargs: Any) -> None:
            # Replace with a different list
            mock_local_node.channels = [channel_pb2.Channel()]

        with (
            caplog.at_level(logging.WARNING),
            patch.object(
                delete_channel_runtime._channel_write_runtime,
                "write_channel_snapshot",
                side_effect=change_cache,
            ),
        ):
            delete_channel_runtime.delete_channel(1)

        assert "Channel cache changed during delete rewrite" in caplog.text
        assert mock_local_node.channels is None
        assert mock_local_node.partialChannels == []

    @pytest.mark.unit
    def test_delete_channel_invalidates_cache_when_rewrite_raises(
        self,
        delete_channel_runtime: _NodeDeleteChannelRuntime,
        mock_local_node: MagicMock,
    ) -> None:
        """delete_channel should invalidate cache if rewrite fails mid-transaction."""
        channels = []
        for i in range(MAX_CHANNELS):
            ch = channel_pb2.Channel()
            ch.index = i
            if i == 0:
                ch.role = channel_pb2.Channel.Role.PRIMARY
            elif i == 1:
                ch.role = channel_pb2.Channel.Role.SECONDARY
            else:
                ch.role = channel_pb2.Channel.Role.DISABLED
            channels.append(ch)
        mock_local_node.channels = channels

        with patch.object(
            delete_channel_runtime,
            "_execute_rewrite_plan",
            side_effect=RuntimeError("rewrite failed"),
        ):
            with pytest.raises(RuntimeError, match="rewrite failed"):
                delete_channel_runtime.delete_channel(1)

        assert mock_local_node.channels is None
        assert mock_local_node.partialChannels == []


# ============================================================================
# Tests for _NodeAckNakRuntime
# ============================================================================


class TestNodeAckNakRuntime:
    """Tests for _NodeAckNakRuntime."""

    @pytest.mark.unit
    def test_handle_ack_nak_with_error_reason_sets_nak(
        self, mock_local_node: MagicMock
    ) -> None:
        """handle_ack_nak with error reason should set receivedNak."""
        runtime = _NodeAckNakRuntime(mock_local_node)
        packet: dict[str, Any] = {
            "decoded": {
                "routing": {"errorReason": "NO_RESPONSE"},
            },
            "from": 12345,
        }

        runtime.handle_ack_nak(packet)

        assert mock_local_node.iface._acknowledgment.receivedNak is True

    @pytest.mark.unit
    def test_handle_ack_nak_without_error_reason_sets_ack(
        self, mock_local_node: MagicMock
    ) -> None:
        """handle_ack_nak without error reason should set receivedAck."""
        runtime = _NodeAckNakRuntime(mock_local_node)
        packet: dict[str, Any] = {
            "decoded": {
                "routing": {"errorReason": "NONE"},
            },
            "from": 99999999,  # Different from local node
        }

        runtime.handle_ack_nak(packet)

        assert mock_local_node.iface._acknowledgment.receivedAck is True

    @pytest.mark.unit
    def test_handle_ack_nak_from_local_node_sets_impl_ack(
        self, mock_local_node: MagicMock, caplog: pytest.LogCaptureFixture
    ) -> None:
        """handle_ack_nak from local node should set receivedImplAck."""
        runtime = _NodeAckNakRuntime(mock_local_node)
        packet: dict[str, Any] = {
            "decoded": {
                "routing": {"errorReason": "NONE"},
            },
            "from": mock_local_node.nodeNum,  # Same as local node
        }

        with caplog.at_level(logging.INFO):
            runtime.handle_ack_nak(packet)

        assert mock_local_node.iface._acknowledgment.receivedImplAck is True
        assert "implicit ACK" in caplog.text

    @pytest.mark.unit
    def test_handle_ack_nak_missing_routing_logs_warning(
        self, mock_local_node: MagicMock, caplog: pytest.LogCaptureFixture
    ) -> None:
        """handle_ack_nak with missing routing should log warning."""
        runtime = _NodeAckNakRuntime(mock_local_node)
        packet: dict[str, Any] = {"decoded": {}}

        with caplog.at_level(logging.WARNING):
            runtime.handle_ack_nak(packet)

        assert "without routing details" in caplog.text

    @pytest.mark.unit
    def test_handle_ack_nak_missing_from_logs_warning(
        self, mock_local_node: MagicMock, caplog: pytest.LogCaptureFixture
    ) -> None:
        """handle_ack_nak with missing from should log warning."""
        runtime = _NodeAckNakRuntime(mock_local_node)
        packet: dict[str, Any] = {
            "decoded": {
                "routing": {"errorReason": "NONE"},
            }
        }

        with caplog.at_level(logging.WARNING):
            runtime.handle_ack_nak(packet)

        assert "without sender" in caplog.text

    @pytest.mark.unit
    def test_handle_ack_nak_invalid_from_logs_warning(
        self, mock_local_node: MagicMock, caplog: pytest.LogCaptureFixture
    ) -> None:
        """handle_ack_nak with invalid from should log warning."""
        runtime = _NodeAckNakRuntime(mock_local_node)
        packet: dict[str, Any] = {
            "decoded": {
                "routing": {"errorReason": "NONE"},
            },
            "from": "invalid",
        }

        with caplog.at_level(logging.WARNING):
            runtime.handle_ack_nak(packet)

        assert "invalid sender" in caplog.text


# ============================================================================
# Tests for _NodePositionTimeCommandRuntime (Lines 353-424)
# ============================================================================


class TestNodePositionTimeCommandRuntime:
    """Tests for _NodePositionTimeCommandRuntime (lines 353-424)."""

    @pytest.fixture
    def position_time_runtime(
        self, mock_local_node: MagicMock
    ) -> _NodePositionTimeCommandRuntime:
        """Create a position/time command runtime instance for testing.

        Parameters
        ----------
        mock_local_node : MagicMock
            The mock local node fixture.

        Returns
        -------
        _NodePositionTimeCommandRuntime
            A position/time command runtime instance.
        """
        return _NodePositionTimeCommandRuntime(mock_local_node)

    # -------------------------------------------------------------------------
    # set_fixed_position tests (lines 380-422)
    # -------------------------------------------------------------------------

    @pytest.mark.unit
    def test_set_fixed_position_with_float_lat_lon_scales_by_1e7(
        self,
        position_time_runtime: _NodePositionTimeCommandRuntime,
        mock_local_node: MagicMock,
    ) -> None:
        """set_fixed_position with float lat/lon should scale by 1e7 (lines 406-415)."""
        position_time_runtime.set_fixed_position(lat=37.7749, lon=-122.4194, alt=10)

        mock_local_node.ensureSessionKey.assert_called_once()
        mock_local_node._send_admin.assert_called_once()

        # Verify the position message was scaled correctly
        call_args = mock_local_node._send_admin.call_args
        admin_message = call_args[0][0]
        assert isinstance(admin_message, admin_pb2.AdminMessage)

        position = admin_message.set_fixed_position
        assert position.latitude_i == int(37.7749 * 1e7)
        assert position.longitude_i == int(-122.4194 * 1e7)
        assert position.altitude == 10

    @pytest.mark.unit
    def test_set_fixed_position_with_int_lat_lon_uses_directly(
        self,
        position_time_runtime: _NodePositionTimeCommandRuntime,
        mock_local_node: MagicMock,
    ) -> None:
        """set_fixed_position with int lat/lon should use values directly (lines 408-409, 414-415)."""
        lat_int = 377749000  # Pre-scaled latitude
        lon_int = -1224194000  # Pre-scaled longitude

        position_time_runtime.set_fixed_position(lat=lat_int, lon=lon_int, alt=50)

        mock_local_node.ensureSessionKey.assert_called_once()
        call_args = mock_local_node._send_admin.call_args
        admin_message = call_args[0][0]

        position = admin_message.set_fixed_position
        assert position.latitude_i == lat_int
        assert position.longitude_i == lon_int
        assert position.altitude == 50

    @pytest.mark.unit
    def test_set_fixed_position_with_none_lat_omits_latitude(
        self,
        position_time_runtime: _NodePositionTimeCommandRuntime,
        mock_local_node: MagicMock,
    ) -> None:
        """set_fixed_position with None lat should omit latitude_i field."""
        position_time_runtime.set_fixed_position(lat=None, lon=-122.4194, alt=10)

        call_args = mock_local_node._send_admin.call_args
        admin_message = call_args[0][0]

        position = admin_message.set_fixed_position
        # latitude_i should be 0 (default) when not set
        assert position.latitude_i == 0
        assert position.longitude_i == int(-122.4194 * 1e7)

    @pytest.mark.unit
    def test_set_fixed_position_with_none_lon_omits_longitude(
        self,
        position_time_runtime: _NodePositionTimeCommandRuntime,
        mock_local_node: MagicMock,
    ) -> None:
        """set_fixed_position with None lon should omit longitude_i field."""
        position_time_runtime.set_fixed_position(lat=37.7749, lon=None, alt=10)

        call_args = mock_local_node._send_admin.call_args
        admin_message = call_args[0][0]

        position = admin_message.set_fixed_position
        assert position.latitude_i == int(37.7749 * 1e7)
        # longitude_i should be 0 (default) when not set
        assert position.longitude_i == 0

    @pytest.mark.unit
    def test_set_fixed_position_with_altitude_sets_altitude_field(
        self,
        position_time_runtime: _NodePositionTimeCommandRuntime,
        mock_local_node: MagicMock,
    ) -> None:
        """set_fixed_position with altitude should set altitude field (lines 417-418)."""
        position_time_runtime.set_fixed_position(lat=37.7749, lon=-122.4194, alt=100)

        call_args = mock_local_node._send_admin.call_args
        admin_message = call_args[0][0]

        position = admin_message.set_fixed_position
        assert position.altitude == 100

    @pytest.mark.unit
    def test_set_fixed_position_with_none_alt_omits_altitude(
        self,
        position_time_runtime: _NodePositionTimeCommandRuntime,
        mock_local_node: MagicMock,
    ) -> None:
        """set_fixed_position with None alt should omit altitude field."""
        position_time_runtime.set_fixed_position(lat=37.7749, lon=-122.4194, alt=None)

        call_args = mock_local_node._send_admin.call_args
        admin_message = call_args[0][0]

        position = admin_message.set_fixed_position
        # altitude should be 0 (default) when not set
        assert position.altitude == 0

    @pytest.mark.unit
    def test_set_fixed_position_calls_ensure_session_key(
        self,
        position_time_runtime: _NodePositionTimeCommandRuntime,
        mock_local_node: MagicMock,
    ) -> None:
        """set_fixed_position should call ensureSessionKey (line 402)."""
        position_time_runtime.set_fixed_position(lat=37.7749, lon=-122.4194, alt=10)

        mock_local_node.ensureSessionKey.assert_called_once()

    @pytest.mark.unit
    def test_set_fixed_position_calls_send_admin(
        self,
        position_time_runtime: _NodePositionTimeCommandRuntime,
        mock_local_node: MagicMock,
    ) -> None:
        """set_fixed_position should call _send_admin with correct message."""
        position_time_runtime.set_fixed_position(lat=37.7749, lon=-122.4194, alt=10)

        mock_local_node._send_admin.assert_called_once()
        call_args = mock_local_node._send_admin.call_args

        # Verify onResponse was passed (None for local node)
        assert "onResponse" in call_args[1]

    @pytest.mark.unit
    def test_set_fixed_position_for_remote_node_waits_for_ack(
        self, mock_remote_node: MagicMock, mock_iface: MagicMock
    ) -> None:
        """set_fixed_position for remote node should wait for ACK/NAK."""
        # Set up local node (different from remote)
        mock_local = MagicMock()
        mock_local.nodeNum = 1111111111
        mock_iface.localNode = mock_local

        runtime = _NodePositionTimeCommandRuntime(mock_remote_node)

        runtime.set_fixed_position(lat=37.7749, lon=-122.4194, alt=10)

        # Should call waitForAckNak for remote node
        mock_iface.waitForAckNak.assert_called_once()

    # -------------------------------------------------------------------------
    # remove_fixed_position tests (lines 424-430)
    # -------------------------------------------------------------------------

    @pytest.mark.unit
    def test_remove_fixed_position_sets_remove_flag(
        self,
        position_time_runtime: _NodePositionTimeCommandRuntime,
        mock_local_node: MagicMock,
    ) -> None:
        """remove_fixed_position should set remove_fixed_position flag."""
        position_time_runtime.remove_fixed_position()

        mock_local_node.ensureSessionKey.assert_called_once()
        call_args = mock_local_node._send_admin.call_args
        admin_message = call_args[0][0]

        assert admin_message.remove_fixed_position is True

    @pytest.mark.unit
    @pytest.mark.usefixtures("mock_local_node")
    def test_remove_fixed_position_logs_info(
        self,
        position_time_runtime: _NodePositionTimeCommandRuntime,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """remove_fixed_position should log info message."""
        with caplog.at_level(logging.INFO):
            position_time_runtime.remove_fixed_position()

        assert "remove fixed position" in caplog.text

    # -------------------------------------------------------------------------
    # set_time tests (lines 432-440)
    # -------------------------------------------------------------------------

    @pytest.mark.unit
    def test_set_time_with_explicit_timestamp(
        self,
        position_time_runtime: _NodePositionTimeCommandRuntime,
        mock_local_node: MagicMock,
    ) -> None:
        """set_time with explicit timestamp should use provided value."""
        explicit_time = 1700000000

        position_time_runtime.set_time(time_sec=explicit_time)

        mock_local_node.ensureSessionKey.assert_called_once()
        call_args = mock_local_node._send_admin.call_args
        admin_message = call_args[0][0]

        assert admin_message.set_time_only == explicit_time

    @pytest.mark.unit
    def test_set_time_with_zero_uses_current_time(
        self,
        position_time_runtime: _NodePositionTimeCommandRuntime,
        mock_local_node: MagicMock,
    ) -> None:
        """set_time with time_sec=0 should use current time."""
        current_time = 1700123456

        with patch.object(time_module, "time", return_value=current_time):
            position_time_runtime.set_time(time_sec=0)

        call_args = mock_local_node._send_admin.call_args
        admin_message = call_args[0][0]

        assert admin_message.set_time_only == current_time

    @pytest.mark.unit
    def test_set_time_default_uses_current_time(
        self,
        position_time_runtime: _NodePositionTimeCommandRuntime,
        mock_local_node: MagicMock,
    ) -> None:
        """set_time with default argument should use current time."""
        current_time = 1700999999

        with patch.object(time_module, "time", return_value=current_time):
            position_time_runtime.set_time()

        call_args = mock_local_node._send_admin.call_args
        admin_message = call_args[0][0]

        assert admin_message.set_time_only == current_time

    @pytest.mark.unit
    @pytest.mark.usefixtures("mock_local_node")
    def test_set_time_logs_info(
        self,
        position_time_runtime: _NodePositionTimeCommandRuntime,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """set_time should log info message with timestamp."""
        with caplog.at_level(logging.INFO):
            position_time_runtime.set_time(time_sec=1700000000)

        assert "Setting node time" in caplog.text
        assert "1700000000" in caplog.text


# ============================================================================
# Tests for _DeleteChannelRewritePlan dataclass
# ============================================================================


class TestDeleteChannelRewritePlan:
    """Tests for _DeleteChannelRewritePlan dataclass."""

    @pytest.mark.unit
    def test_rewrite_plan_is_frozen(self) -> None:
        """_DeleteChannelRewritePlan should be immutable (frozen)."""
        channels = [channel_pb2.Channel()]
        plan = _DeleteChannelRewritePlan(
            original_channels_ref=channels,
            pre_delete_admin_index=0,
            post_delete_admin_index=1,
            switch_after_admin_slot_rewrite=False,
            channels_to_rewrite=[],
            staged_channels=[],
        )

        with pytest.raises(AttributeError):
            plan.pre_delete_admin_index = 5  # type: ignore[misc]

    @pytest.mark.unit
    def test_rewrite_plan_stores_all_fields(self) -> None:
        """_DeleteChannelRewritePlan should store all provided fields."""
        channels = [channel_pb2.Channel()]
        rewrite_channels = [channel_pb2.Channel(), channel_pb2.Channel()]
        staged = [channel_pb2.Channel()]

        plan = _DeleteChannelRewritePlan(
            original_channels_ref=channels,
            pre_delete_admin_index=2,
            post_delete_admin_index=3,
            switch_after_admin_slot_rewrite=True,
            channels_to_rewrite=rewrite_channels,
            staged_channels=staged,
        )

        assert plan.original_channels_ref is channels
        assert plan.pre_delete_admin_index == 2
        assert plan.post_delete_admin_index == 3
        assert plan.switch_after_admin_slot_rewrite is True
        assert plan.channels_to_rewrite is rewrite_channels
        assert plan.staged_channels is staged
