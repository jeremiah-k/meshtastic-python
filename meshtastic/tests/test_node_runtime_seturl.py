"""Unit tests for the seturl_runtime module.

Tests for setURL transaction runtime classes including parser, planners,
execution engine, rollback engine, cache manager, and coordinator.
"""

import base64
import logging
import threading
from typing import NoReturn
from unittest.mock import MagicMock

import pytest
from pytest import LogCaptureFixture

from meshtastic.node_runtime.seturl_runtime import (
    _SetUrlAddOnlyExecutionState,
    _SetUrlAddOnlyPlan,
    _SetUrlAddOnlyPlanner,
    _SetUrlCacheManager,
    _SetUrlExecutionEngine,
    _SetUrlParsedInput,
    _SetUrlReplaceExecutionState,
    _SetUrlReplacePlan,
    _SetUrlReplacePlanner,
    _SetUrlRollbackEngine,
    _SetUrlParser,
    _SetUrlTransactionCoordinator,
)
from meshtastic.protobuf import (
    admin_pb2,
    apponly_pb2,
    channel_pb2,
    config_pb2,
    localonly_pb2,
)


# ============================================================================
# Helper Functions
# ============================================================================


def _make_channel(
    index: int,
    role: channel_pb2.Channel.Role.ValueType,
    name: str = "",
    psk: bytes = b"",
) -> channel_pb2.Channel:
    """Create a Channel protobuf with the given index, role, name, and psk.

    Parameters
    ----------
    index : int
        Channel index.
    role : channel_pb2.Channel.Role.ValueType
        Channel role (PRIMARY, SECONDARY, DISABLED).
    name : str
        Optional channel settings name.
    psk : bytes
        Optional channel settings psk.

    Returns
    -------
    channel_pb2.Channel
        A configured Channel instance.
    """
    channel = channel_pb2.Channel(index=index, role=role)
    if name:
        channel.settings.name = name
    if psk:
        channel.settings.psk = psk
    return channel


def _make_valid_channel_set_url(channel_name: str = "test") -> str:
    """Create a valid channel set URL for testing.

    Parameters
    ----------
    channel_name : str
        Name for the test channel.

    Returns
    -------
    str
        A valid meshtastic URL with encoded ChannelSet.
    """
    channel_set = apponly_pb2.ChannelSet()
    settings = channel_set.settings.add()
    settings.name = channel_name
    settings.psk = b"\x01"
    encoded = base64.urlsafe_b64encode(channel_set.SerializeToString()).decode("utf-8")
    return f"https://meshtastic.org/d/#{encoded}"


def _make_channel_set_with_lora(channel_name: str = "test") -> apponly_pb2.ChannelSet:
    """Create a ChannelSet with LoRa config for testing.

    Parameters
    ----------
    channel_name : str
        Name for the test channel.

    Returns
    -------
    apponly_pb2.ChannelSet
        A ChannelSet with channel settings and LoRa config.
    """
    channel_set = apponly_pb2.ChannelSet()
    settings = channel_set.settings.add()
    settings.name = channel_name
    settings.psk = b"\x01"
    channel_set.lora_config.hop_limit = 3
    return channel_set


# ============================================================================
# Fixtures
# ============================================================================


@pytest.fixture
def mock_iface() -> MagicMock:
    """Create a minimal mock interface.

    Returns
    -------
    MagicMock
        A mock interface with localNode attribute.
    """
    iface = MagicMock(spec=["localNode"])
    iface.localNode = None
    return iface


@pytest.fixture
def mock_local_node(mock_iface: MagicMock) -> MagicMock:
    """Create a mock local node for setURL testing.

    Parameters
    ----------
    mock_iface : MagicMock
        The mock interface fixture.

    Returns
    -------
    MagicMock
        A mock node configured as a local node with all required attributes.
    """
    node = MagicMock(
        spec=[
            "nodeNum",
            "iface",
            "noProto",
            "_channels_lock",
            "channels",
            "partialChannels",
            "localConfig",
            "_raise_interface_error",
            "_write_channel_snapshot",
            "_send_admin",
            "ensureSessionKey",
            "_get_admin_channel_index",
            "_get_named_admin_channel_index",
        ]
    )
    node.nodeNum = 1234567890
    node.iface = mock_iface
    node.noProto = False
    node._channels_lock = threading.RLock()
    node.channels = None
    node.partialChannels = []
    node.localConfig = localonly_pb2.LocalConfig()
    node._raise_interface_error = MagicMock(side_effect=Exception("interface error"))
    node._write_channel_snapshot = MagicMock()
    node._send_admin = MagicMock()
    node.ensureSessionKey = MagicMock()
    node._get_admin_channel_index = MagicMock(return_value=0)
    node._get_named_admin_channel_index = MagicMock(return_value=None)

    # Set up iface.localNode to return this node
    mock_iface.localNode = node
    return node


@pytest.fixture
def cache_manager(mock_local_node: MagicMock) -> _SetUrlCacheManager:
    """Provide a _SetUrlCacheManager instance bound to the mock node.

    Parameters
    ----------
    mock_local_node : MagicMock
        The mock node fixture.

    Returns
    -------
    _SetUrlCacheManager
        The cache manager instance under test.
    """
    return _SetUrlCacheManager(mock_local_node)


@pytest.fixture
def rollback_engine(
    mock_local_node: MagicMock, cache_manager: _SetUrlCacheManager
) -> _SetUrlRollbackEngine:
    """Provide a _SetUrlRollbackEngine instance.

    Parameters
    ----------
    mock_local_node : MagicMock
        The mock node fixture.
    cache_manager : _SetUrlCacheManager
        The cache manager fixture.

    Returns
    -------
    _SetUrlRollbackEngine
        The rollback engine instance under test.
    """
    return _SetUrlRollbackEngine(mock_local_node, cache_manager=cache_manager)


@pytest.fixture
def execution_engine(
    mock_local_node: MagicMock, cache_manager: _SetUrlCacheManager
) -> _SetUrlExecutionEngine:
    """Provide a _SetUrlExecutionEngine instance.

    Parameters
    ----------
    mock_local_node : MagicMock
        The mock node fixture.
    cache_manager : _SetUrlCacheManager
        The cache manager fixture.

    Returns
    -------
    _SetUrlExecutionEngine
        The execution engine instance under test.
    """
    return _SetUrlExecutionEngine(mock_local_node, cache_manager=cache_manager)


# ============================================================================
# Tests for _SetUrlParser
# ============================================================================


class TestSetUrlParser:
    """Tests for _SetUrlParser."""

    @pytest.mark.unit
    def test_parse_with_valid_url_returns_parsed_input(self) -> None:
        """parse() with valid URL returns parsed config."""
        url = _make_valid_channel_set_url("testchannel")

        def raise_error(msg: str) -> NoReturn:
            raise AssertionError(f"Should not raise: {msg}")

        result = _SetUrlParser.parse(url, raise_interface_error=raise_error)

        assert isinstance(result, _SetUrlParsedInput)
        assert result.url == url
        assert len(result.channel_set.settings) == 1
        assert result.channel_set.settings[0].name == "testchannel"
        assert result.has_lora_update is False

    @pytest.mark.unit
    def test_parse_with_lora_config_sets_flag(self) -> None:
        """parse() with URL containing LoRa config sets has_lora_update."""
        channel_set = _make_channel_set_with_lora("test")
        encoded = base64.urlsafe_b64encode(channel_set.SerializeToString()).decode(
            "utf-8"
        )
        url = f"https://meshtastic.org/d/#{encoded}"

        def raise_error(msg: str) -> NoReturn:
            raise AssertionError(f"Should not raise: {msg}")

        result = _SetUrlParser.parse(url, raise_interface_error=raise_error)

        assert result.has_lora_update is True
        assert result.channel_set.lora_config.hop_limit == 3

    @pytest.mark.unit
    def test_parse_without_hash_raises_error(self) -> None:
        """parse() with URL missing hash fragment raises error."""
        url = "https://meshtastic.org/d/"

        captured_msg: list[str] = []

        def raise_error(msg: str) -> NoReturn:
            captured_msg.append(msg)
            raise Exception(msg)

        with pytest.raises(Exception):
            _SetUrlParser.parse(url, raise_interface_error=raise_error)

        assert len(captured_msg) == 1
        assert captured_msg[0] == "Invalid URL"

    @pytest.mark.unit
    def test_parse_with_empty_hash_raises_error(self) -> None:
        """parse() with empty hash fragment raises error."""
        url = "https://meshtastic.org/d/#"

        captured_msg: list[str] = []

        def raise_error(msg: str) -> NoReturn:
            captured_msg.append(msg)
            raise Exception(msg)

        with pytest.raises(Exception):
            _SetUrlParser.parse(url, raise_interface_error=raise_error)

        assert len(captured_msg) == 1
        assert "no channel data" in captured_msg[0]

    @pytest.mark.unit
    def test_parse_with_invalid_base64_raises_error(self) -> None:
        """parse() with invalid base64 raises error."""
        url = "https://meshtastic.org/d/#!!!invalid-base64!!!"

        captured_msg: list[str] = []

        def raise_error(msg: str) -> NoReturn:
            captured_msg.append(msg)
            raise Exception(msg)

        with pytest.raises(Exception):
            _SetUrlParser.parse(url, raise_interface_error=raise_error)

        assert len(captured_msg) == 1
        assert "Invalid URL" in captured_msg[0]

    @pytest.mark.unit
    def test_parse_with_no_settings_raises_error(self) -> None:
        """parse() with empty ChannelSet raises error."""
        channel_set = apponly_pb2.ChannelSet()  # Empty - no settings
        encoded = base64.urlsafe_b64encode(channel_set.SerializeToString()).decode(
            "utf-8"
        )
        url = f"https://meshtastic.org/d/#{encoded}"

        captured_msg: list[str] = []

        def raise_error(msg: str) -> NoReturn:
            captured_msg.append(msg)
            raise Exception(msg)

        with pytest.raises(Exception):
            _SetUrlParser.parse(url, raise_interface_error=raise_error)

        # The empty ChannelSet is encoded as empty bytes, which results in
        # "no channel data found" since the base64 is empty after stripping
        assert len(captured_msg) == 1
        assert (
            "no channel data" in captured_msg[0].lower()
            or "no settings" in captured_msg[0].lower()
        )

    @pytest.mark.unit
    def test_parse_adds_padding_to_base64(self) -> None:
        """parse() correctly adds padding to unpadded base64."""
        channel_set = apponly_pb2.ChannelSet()
        settings = channel_set.settings.add()
        settings.name = "test"
        settings.psk = b"\x01"
        encoded = base64.urlsafe_b64encode(channel_set.SerializeToString()).decode(
            "utf-8"
        )
        # Remove padding
        encoded_unpadded = encoded.rstrip("=")
        url = f"https://meshtastic.org/d/#{encoded_unpadded}"

        def raise_error(msg: str) -> NoReturn:
            raise AssertionError(f"Should not raise: {msg}")

        result = _SetUrlParser.parse(url, raise_interface_error=raise_error)

        assert result.channel_set.settings[0].name == "test"


# ============================================================================
# Tests for _SetUrlCacheManager
# ============================================================================


class TestSetUrlCacheManager:
    """Tests for _SetUrlCacheManager."""

    @pytest.mark.unit
    def test_apply_add_only_success_with_none_channels_logs_warning(
        self,
        cache_manager: _SetUrlCacheManager,
        mock_local_node: MagicMock,
        caplog: LogCaptureFixture,
    ) -> None:
        """apply_add_only_success with None channels logs warning (line 381)."""
        mock_local_node.channels = None
        channels_to_write: list[tuple[channel_pb2.Channel, str]] = [
            (_make_channel(0, channel_pb2.Channel.Role.SECONDARY, "test"), "test")
        ]

        with caplog.at_level(logging.WARNING):
            cache_manager.apply_add_only_success(channels_to_write)

        assert "Channel cache unavailable after successful addOnly apply" in caplog.text

    @pytest.mark.unit
    def test_apply_add_only_success_with_out_of_range_index_invalidates_cache(
        self,
        cache_manager: _SetUrlCacheManager,
        mock_local_node: MagicMock,
        caplog: LogCaptureFixture,
    ) -> None:
        """apply_add_only_success with out-of-range index invalidates cache (lines 388-393)."""
        mock_local_node.channels = [
            _make_channel(0, channel_pb2.Channel.Role.PRIMARY, "primary"),
        ]
        # Channel index 5 is out of range for a 1-element list
        channels_to_write: list[tuple[channel_pb2.Channel, str]] = [
            (_make_channel(5, channel_pb2.Channel.Role.SECONDARY, "test"), "test")
        ]

        with caplog.at_level(logging.WARNING):
            cache_manager.apply_add_only_success(channels_to_write)

        assert "out of range during addOnly cache update" in caplog.text
        assert mock_local_node.channels is None
        assert mock_local_node.partialChannels == []

    @pytest.mark.unit
    def test_apply_add_only_success_updates_valid_channels(
        self, cache_manager: _SetUrlCacheManager, mock_local_node: MagicMock
    ) -> None:
        """apply_add_only_success updates valid channel indices."""
        primary = _make_channel(0, channel_pb2.Channel.Role.PRIMARY, "primary")
        disabled = _make_channel(1, channel_pb2.Channel.Role.DISABLED)
        mock_local_node.channels = [primary, disabled]

        new_channel = _make_channel(1, channel_pb2.Channel.Role.SECONDARY, "new")
        channels_to_write: list[tuple[channel_pb2.Channel, str]] = [
            (new_channel, "new")
        ]

        cache_manager.apply_add_only_success(channels_to_write)

        # Channel at index 1 should be updated
        assert mock_local_node.channels[1].role == channel_pb2.Channel.Role.SECONDARY

    @pytest.mark.unit
    def test_apply_replace_channel_write_with_none_channels_raises_error(
        self, cache_manager: _SetUrlCacheManager, mock_local_node: MagicMock
    ) -> None:
        """apply_replace_channel_write with None channels raises error (line 401)."""
        mock_local_node.channels = None
        staged_channel = _make_channel(0, channel_pb2.Channel.Role.PRIMARY, "test")

        # Mock _raise_interface_error to raise an exception with the actual message
        def raise_error(msg: str) -> NoReturn:
            raise Exception(msg)

        mock_local_node._raise_interface_error = MagicMock(side_effect=raise_error)

        with pytest.raises(Exception, match="Config or channels not loaded"):
            cache_manager.apply_replace_channel_write(staged_channel)

    @pytest.mark.unit
    def test_apply_replace_channel_write_with_out_of_range_index_raises_error(
        self, cache_manager: _SetUrlCacheManager, mock_local_node: MagicMock
    ) -> None:
        """apply_replace_channel_write with out-of-range index raises error (line 405)."""
        mock_local_node.channels = [
            _make_channel(0, channel_pb2.Channel.Role.PRIMARY, "primary")
        ]
        staged_channel = _make_channel(5, channel_pb2.Channel.Role.SECONDARY, "test")

        # Mock _raise_interface_error to raise an exception with the actual message
        def raise_error(msg: str) -> NoReturn:
            raise Exception(msg)

        mock_local_node._raise_interface_error = MagicMock(side_effect=raise_error)

        with pytest.raises(Exception, match="out of range"):
            cache_manager.apply_replace_channel_write(staged_channel)

    @pytest.mark.unit
    def test_apply_replace_channel_write_with_negative_index_raises_error(
        self, cache_manager: _SetUrlCacheManager, mock_local_node: MagicMock
    ) -> None:
        """apply_replace_channel_write with negative index raises error."""
        mock_local_node.channels = [
            _make_channel(0, channel_pb2.Channel.Role.PRIMARY, "primary")
        ]
        staged_channel = _make_channel(-1, channel_pb2.Channel.Role.SECONDARY, "test")

        # Mock _raise_interface_error to raise an exception with the actual message
        def raise_error(msg: str) -> NoReturn:
            raise Exception(msg)

        mock_local_node._raise_interface_error = MagicMock(side_effect=raise_error)

        with pytest.raises(Exception, match="out of range"):
            cache_manager.apply_replace_channel_write(staged_channel)

    @pytest.mark.unit
    def test_apply_replace_channel_write_updates_valid_channel(
        self, cache_manager: _SetUrlCacheManager, mock_local_node: MagicMock
    ) -> None:
        """apply_replace_channel_write updates a valid channel."""
        primary = _make_channel(0, channel_pb2.Channel.Role.PRIMARY, "old")
        mock_local_node.channels = [primary]

        staged_channel = _make_channel(0, channel_pb2.Channel.Role.PRIMARY, "new")

        cache_manager.apply_replace_channel_write(staged_channel)

        assert mock_local_node.channels[0].settings.name == "new"

    @pytest.mark.unit
    def test_clear_lora_cache_with_warning_clears_and_logs(
        self,
        cache_manager: _SetUrlCacheManager,
        mock_local_node: MagicMock,
        caplog: LogCaptureFixture,
    ) -> None:
        """clear_lora_cache_with_warning clears LoRa cache and logs (lines 445-446)."""
        mock_local_node.localConfig.lora.hop_limit = 5

        with caplog.at_level(logging.WARNING):
            cache_manager.clear_lora_cache_with_warning("Test warning message")

        assert not mock_local_node.localConfig.HasField("lora")
        assert "Test warning message" in caplog.text

    @pytest.mark.unit
    def test_restore_lora_snapshot_restores_config(
        self, cache_manager: _SetUrlCacheManager, mock_local_node: MagicMock
    ) -> None:
        """restore_lora_snapshot restores LoRa config."""
        original_config = config_pb2.Config.LoRaConfig()
        original_config.hop_limit = 7
        original_config.region = config_pb2.Config.LoRaConfig.RegionCode.US

        cache_manager.restore_lora_snapshot(original_config)

        assert mock_local_node.localConfig.lora.hop_limit == 7
        assert (
            mock_local_node.localConfig.lora.region
            == config_pb2.Config.LoRaConfig.RegionCode.US
        )

    @pytest.mark.unit
    def test_apply_lora_success_updates_config(
        self, cache_manager: _SetUrlCacheManager, mock_local_node: MagicMock
    ) -> None:
        """apply_lora_success updates LoRa config in localConfig."""
        lora_config = config_pb2.Config.LoRaConfig()
        lora_config.hop_limit = 4

        cache_manager.apply_lora_success(lora_config)

        assert mock_local_node.localConfig.lora.hop_limit == 4

    @pytest.mark.unit
    def test_invalidate_channel_cache_clears_channels(
        self,
        cache_manager: _SetUrlCacheManager,
        mock_local_node: MagicMock,
        caplog: LogCaptureFixture,
    ) -> None:
        """invalidate_channel_cache clears channels and partialChannels."""
        mock_local_node.channels = [
            _make_channel(0, channel_pb2.Channel.Role.PRIMARY, "test")
        ]
        mock_local_node.partialChannels = ["partial"]

        with caplog.at_level(logging.WARNING):
            cache_manager.invalidate_channel_cache("Cache invalidated for testing")

        assert mock_local_node.channels is None
        assert mock_local_node.partialChannels == []
        assert "Cache invalidated for testing" in caplog.text

    @pytest.mark.unit
    def test_restore_replace_channels_snapshot_restores_channels(
        self, cache_manager: _SetUrlCacheManager, mock_local_node: MagicMock
    ) -> None:
        """restore_replace_channels_snapshot restores channel list from snapshot."""
        original_channels = [
            _make_channel(0, channel_pb2.Channel.Role.PRIMARY, "primary"),
            _make_channel(1, channel_pb2.Channel.Role.SECONDARY, "secondary"),
        ]

        cache_manager.restore_replace_channels_snapshot(original_channels)

        assert len(mock_local_node.channels) == 2
        assert mock_local_node.channels[0].settings.name == "primary"
        assert mock_local_node.channels[1].settings.name == "secondary"
        assert mock_local_node.partialChannels == []


# ============================================================================
# Tests for _SetUrlRollbackEngine
# ============================================================================


class TestSetUrlRollbackEngine:
    """Tests for _SetUrlRollbackEngine."""

    @pytest.mark.unit
    def test_rollback_add_only_with_channel_failure_attempts_next_admin_index(
        self,
        rollback_engine: _SetUrlRollbackEngine,
        mock_local_node: MagicMock,
        caplog: LogCaptureFixture,
    ) -> None:
        """rollback_add_only with channel failure attempts next admin index (line 658)."""
        # Set up channels with a disabled slot for addOnly
        original_channel = _make_channel(1, channel_pb2.Channel.Role.DISABLED)
        mock_local_node.channels = [
            _make_channel(0, channel_pb2.Channel.Role.PRIMARY, "primary"),
            original_channel,
        ]

        # Create plan with original channel for rollback
        plan = _SetUrlAddOnlyPlan(
            ignored_channel_names=[],
            channels_to_write=[],
            deferred_add_only_admin_channel=None,
            deferred_add_only_admin_index=None,
            original_channels_ref=[],
            original_channels_by_index={1: original_channel},
            original_lora_config=None,
        )

        # State tracking - we wrote to index 1
        state = _SetUrlAddOnlyExecutionState(
            written_indices=[1], lora_write_started=False
        )

        # Mock admin context
        admin_context = MagicMock()
        admin_context.admin_index_for_write = 0

        # First write attempt fails, second succeeds
        call_count = [0]

        def write_side_effect(channel: channel_pb2.Channel, adminIndex: int) -> None:
            call_count[0] += 1
            if call_count[0] == 1:
                raise Exception("Write failed")

        mock_local_node._write_channel_snapshot.side_effect = write_side_effect

        with caplog.at_level(logging.WARNING):
            rollback_engine.rollback_add_only(
                admin_context=admin_context,
                plan=plan,
                state=state,
            )

        # Should have attempted rollback
        assert mock_local_node._write_channel_snapshot.call_count >= 1

    @pytest.mark.unit
    def test_rollback_add_only_with_lora_failure_clears_cache_with_warning(
        self,
        rollback_engine: _SetUrlRollbackEngine,
        mock_local_node: MagicMock,
        caplog: LogCaptureFixture,
    ) -> None:
        """rollback_add_only with LoRa failure clears cache with warning (lines 712-727)."""
        original_channel = _make_channel(1, channel_pb2.Channel.Role.DISABLED)
        mock_local_node.channels = [
            _make_channel(0, channel_pb2.Channel.Role.PRIMARY, "primary"),
            original_channel,
        ]

        # Create plan with LoRa config for rollback
        original_lora = config_pb2.Config.LoRaConfig()
        original_lora.hop_limit = 3

        plan = _SetUrlAddOnlyPlan(
            ignored_channel_names=[],
            channels_to_write=[],
            deferred_add_only_admin_channel=None,
            deferred_add_only_admin_index=None,
            original_channels_ref=[],
            original_channels_by_index={1: original_channel},
            original_lora_config=original_lora,
        )

        # State tracking - LoRa write was started
        state = _SetUrlAddOnlyExecutionState(
            written_indices=[1], lora_write_started=True
        )

        admin_context = MagicMock()
        admin_context.admin_index_for_write = 0

        # Channel write succeeds, LoRa write fails
        write_call_count = [0]

        def write_side_effect(channel: channel_pb2.Channel, adminIndex: int) -> None:
            write_call_count[0] += 1
            # First call is channel write, succeeds

        def send_admin_side_effect(
            msg: admin_pb2.AdminMessage, adminIndex: int
        ) -> None:
            raise Exception("LoRa rollback failed")

        mock_local_node._write_channel_snapshot.side_effect = write_side_effect
        mock_local_node._send_admin.side_effect = send_admin_side_effect

        with caplog.at_level(logging.WARNING):
            rollback_engine.rollback_add_only(
                admin_context=admin_context,
                plan=plan,
                state=state,
            )

        assert "Rollback of LoRa config failed" in caplog.text
        assert "LoRa config cache cleared" in caplog.text

    @pytest.mark.unit
    def test_rollback_replace_all_with_channel_failure_logs_warning(
        self,
        rollback_engine: _SetUrlRollbackEngine,
        mock_local_node: MagicMock,
        caplog: LogCaptureFixture,
    ) -> None:
        """rollback_replace_all with channel failure logs warning (lines 773-790)."""
        original_channel = _make_channel(
            0, channel_pb2.Channel.Role.PRIMARY, "original"
        )
        mock_local_node.channels = [original_channel]

        plan = _SetUrlReplacePlan(
            max_channels=1,
            replace_original_channels_ref=mock_local_node.channels,
            replace_original_channels_snapshot=[original_channel],
            replace_original_channels_by_index={0: original_channel},
            staged_channels=[],
            staged_channels_by_index={},
            deferred_new_named_admin_channel=None,
            deferred_new_named_admin_index=None,
            deferred_previous_admin_slot_channel=None,
            replace_original_lora_config=None,
        )

        # State tracking - we wrote to index 0
        state = _SetUrlReplaceExecutionState(
            written_channel_indices=[0],
            lora_write_started=False,
            rollback_admin_indexes_for_write=[0],
        )

        admin_context = MagicMock()
        admin_context.admin_index_for_write = 0

        # All rollback attempts fail
        mock_local_node._write_channel_snapshot.side_effect = Exception(
            "Rollback failed"
        )

        with caplog.at_level(logging.WARNING):
            rollback_engine.rollback_replace_all(
                admin_context=admin_context,
                plan=plan,
                state=state,
            )

        assert "Rollback of channel index" in caplog.text
        assert "failed after replace-all" in caplog.text

    @pytest.mark.unit
    def test_rollback_replace_all_with_lora_failure_clears_cache_with_warning(
        self,
        rollback_engine: _SetUrlRollbackEngine,
        mock_local_node: MagicMock,
        caplog: LogCaptureFixture,
    ) -> None:
        """rollback_replace_all with LoRa failure clears cache with warning (lines 825-851)."""
        original_channel = _make_channel(
            0, channel_pb2.Channel.Role.PRIMARY, "original"
        )
        mock_local_node.channels = [original_channel]

        original_lora = config_pb2.Config.LoRaConfig()
        original_lora.hop_limit = 3

        plan = _SetUrlReplacePlan(
            max_channels=1,
            replace_original_channels_ref=mock_local_node.channels,
            replace_original_channels_snapshot=[original_channel],
            replace_original_channels_by_index={0: original_channel},
            staged_channels=[],
            staged_channels_by_index={},
            deferred_new_named_admin_channel=None,
            deferred_new_named_admin_index=None,
            deferred_previous_admin_slot_channel=None,
            replace_original_lora_config=original_lora,
        )

        # State tracking - LoRa write was started
        state = _SetUrlReplaceExecutionState(
            written_channel_indices=[],
            lora_write_started=True,
            rollback_admin_indexes_for_write=[0],
        )

        admin_context = MagicMock()
        admin_context.admin_index_for_write = 0

        # LoRa rollback fails
        mock_local_node._send_admin.side_effect = Exception("LoRa rollback failed")

        with caplog.at_level(logging.WARNING):
            rollback_engine.rollback_replace_all(
                admin_context=admin_context,
                plan=plan,
                state=state,
            )

        assert "Rollback of LoRa config failed" in caplog.text
        assert "LoRa config cache cleared after rollback failure" in caplog.text

    @pytest.mark.unit
    def test_rollback_replace_all_without_snapshot_clears_cache(
        self,
        rollback_engine: _SetUrlRollbackEngine,
        mock_local_node: MagicMock,
        caplog: LogCaptureFixture,
    ) -> None:
        """rollback_replace_all without snapshot clears cache (lines 844-847)."""
        original_channel = _make_channel(
            0, channel_pb2.Channel.Role.PRIMARY, "original"
        )
        mock_local_node.channels = [original_channel]

        plan = _SetUrlReplacePlan(
            max_channels=1,
            replace_original_channels_ref=mock_local_node.channels,
            replace_original_channels_snapshot=[original_channel],
            replace_original_channels_by_index={0: original_channel},
            staged_channels=[],
            staged_channels_by_index={},
            deferred_new_named_admin_channel=None,
            deferred_new_named_admin_index=None,
            deferred_previous_admin_slot_channel=None,
            replace_original_lora_config=None,  # No snapshot
        )

        # State tracking - LoRa write was started but no snapshot available
        state = _SetUrlReplaceExecutionState(
            written_channel_indices=[],
            lora_write_started=True,
            rollback_admin_indexes_for_write=[0],
        )

        admin_context = MagicMock()
        admin_context.admin_index_for_write = 0

        with caplog.at_level(logging.WARNING):
            rollback_engine.rollback_replace_all(
                admin_context=admin_context,
                plan=plan,
                state=state,
            )

        assert (
            "LoRa config cache cleared after replace-all failure without rollback snapshot"
            in caplog.text
        )

    @pytest.mark.unit
    def test_rollback_replace_all_successful_restores_snapshot(
        self,
        rollback_engine: _SetUrlRollbackEngine,
        mock_local_node: MagicMock,
        caplog: LogCaptureFixture,
    ) -> None:
        """rollback_replace_all with successful rollback restores snapshot."""
        original_channel = _make_channel(
            0, channel_pb2.Channel.Role.PRIMARY, "original"
        )
        mock_local_node.channels = [original_channel]

        plan = _SetUrlReplacePlan(
            max_channels=1,
            replace_original_channels_ref=mock_local_node.channels,
            replace_original_channels_snapshot=[original_channel],
            replace_original_channels_by_index={0: original_channel},
            staged_channels=[],
            staged_channels_by_index={},
            deferred_new_named_admin_channel=None,
            deferred_new_named_admin_index=None,
            deferred_previous_admin_slot_channel=None,
            replace_original_lora_config=None,
        )

        state = _SetUrlReplaceExecutionState(
            written_channel_indices=[0],
            lora_write_started=False,
            rollback_admin_indexes_for_write=[0],
        )

        admin_context = MagicMock()
        admin_context.admin_index_for_write = 0

        with caplog.at_level(logging.WARNING):
            rollback_engine.rollback_replace_all(
                admin_context=admin_context,
                plan=plan,
                state=state,
            )

        # Channels should be restored
        assert len(mock_local_node.channels) == 1
        assert mock_local_node.channels[0].settings.name == "original"


# ============================================================================
# Tests for _SetUrlExecutionEngine
# ============================================================================


class TestSetUrlExecutionEngine:
    """Tests for _SetUrlExecutionEngine."""

    @pytest.mark.unit
    def test_execute_add_only_writes_channels(
        self, execution_engine: _SetUrlExecutionEngine, mock_local_node: MagicMock
    ) -> None:
        """execute_add_only writes channels to device."""
        channel_set = apponly_pb2.ChannelSet()
        settings = channel_set.settings.add()
        settings.name = "newchannel"
        settings.psk = b"\x01"

        parsed_input = _SetUrlParsedInput(
            url="https://meshtastic.org/d/#test",
            channel_set=channel_set,
            has_lora_update=False,
        )

        admin_context = MagicMock()
        admin_context.admin_index_for_write = 0
        admin_context.has_admin_write_node_named_admin = False

        new_channel = _make_channel(1, channel_pb2.Channel.Role.SECONDARY, "newchannel")
        plan = _SetUrlAddOnlyPlan(
            ignored_channel_names=[],
            channels_to_write=[(new_channel, "newchannel")],
            deferred_add_only_admin_channel=None,
            deferred_add_only_admin_index=None,
            original_channels_ref=[],
            original_channels_by_index={},
            original_lora_config=None,
        )

        state = _SetUrlAddOnlyExecutionState()

        execution_engine.execute_add_only(
            parsed_input=parsed_input,
            admin_context=admin_context,
            plan=plan,
            state=state,
        )

        mock_local_node._write_channel_snapshot.assert_called_once()
        assert 1 in state.written_indices

    @pytest.mark.unit
    def test_execute_add_only_with_lora_update_sends_admin(
        self, execution_engine: _SetUrlExecutionEngine, mock_local_node: MagicMock
    ) -> None:
        """execute_add_only with LoRa update sends admin message."""
        channel_set = _make_channel_set_with_lora("test")

        parsed_input = _SetUrlParsedInput(
            url="https://meshtastic.org/d/#test",
            channel_set=channel_set,
            has_lora_update=True,
        )

        admin_context = MagicMock()
        admin_context.admin_index_for_write = 0
        admin_context.has_admin_write_node_named_admin = False

        new_channel = _make_channel(1, channel_pb2.Channel.Role.SECONDARY, "test")
        plan = _SetUrlAddOnlyPlan(
            ignored_channel_names=[],
            channels_to_write=[(new_channel, "test")],
            deferred_add_only_admin_channel=None,
            deferred_add_only_admin_index=None,
            original_channels_ref=[],
            original_channels_by_index={},
            original_lora_config=config_pb2.Config.LoRaConfig(),
        )

        state = _SetUrlAddOnlyExecutionState()

        execution_engine.execute_add_only(
            parsed_input=parsed_input,
            admin_context=admin_context,
            plan=plan,
            state=state,
        )

        mock_local_node.ensureSessionKey.assert_called()
        mock_local_node._send_admin.assert_called()
        assert state.lora_write_started is True

    @pytest.mark.unit
    def test_execute_replace_all_writes_channels(
        self,
        execution_engine: _SetUrlExecutionEngine,
        mock_local_node: MagicMock,
        cache_manager: _SetUrlCacheManager,
    ) -> None:
        """execute_replace_all writes staged channels."""
        mock_local_node.channels = [
            _make_channel(0, channel_pb2.Channel.Role.PRIMARY, "old"),
            _make_channel(1, channel_pb2.Channel.Role.DISABLED),
        ]

        channel_set = apponly_pb2.ChannelSet()
        settings = channel_set.settings.add()
        settings.name = "newprimary"
        settings.psk = b"\x01"

        parsed_input = _SetUrlParsedInput(
            url="https://meshtastic.org/d/#test",
            channel_set=channel_set,
            has_lora_update=False,
        )

        admin_context = MagicMock()
        admin_context.admin_index_for_write = 0
        admin_context.has_admin_write_node_named_admin = False

        staged = _make_channel(0, channel_pb2.Channel.Role.PRIMARY, "newprimary")
        plan = _SetUrlReplacePlan(
            max_channels=2,
            replace_original_channels_ref=mock_local_node.channels,
            replace_original_channels_snapshot=[],
            replace_original_channels_by_index={},
            staged_channels=[staged],
            staged_channels_by_index={0: staged},
            deferred_new_named_admin_channel=None,
            deferred_new_named_admin_index=None,
            deferred_previous_admin_slot_channel=None,
            replace_original_lora_config=None,
        )

        state = _SetUrlReplaceExecutionState(rollback_admin_indexes_for_write=[0])

        execution_engine.execute_replace_all(
            parsed_input=parsed_input,
            admin_context=admin_context,
            plan=plan,
            state=state,
        )

        mock_local_node._write_channel_snapshot.assert_called()
        assert 0 in state.written_channel_indices

    @pytest.mark.unit
    def test_post_write_fallback_admin_index_returns_named_admin(self) -> None:
        """_post_write_fallback_admin_index returns named admin channel index."""
        staged_admin = _make_channel(1, channel_pb2.Channel.Role.SECONDARY, "admin")
        staged_primary = _make_channel(0, channel_pb2.Channel.Role.PRIMARY, "primary")

        plan = _SetUrlReplacePlan(
            max_channels=2,
            replace_original_channels_ref=[],
            replace_original_channels_snapshot=[],
            replace_original_channels_by_index={},
            staged_channels=[staged_primary, staged_admin],
            staged_channels_by_index={0: staged_primary, 1: staged_admin},
            deferred_new_named_admin_channel=None,
            deferred_new_named_admin_index=None,
            deferred_previous_admin_slot_channel=None,
            replace_original_lora_config=None,
        )

        result = _SetUrlExecutionEngine._post_write_fallback_admin_index(plan)

        assert result == 1

    @pytest.mark.unit
    def test_post_write_fallback_admin_index_defaults_to_zero(self) -> None:
        """_post_write_fallback_admin_index returns 0 when no named admin."""
        staged_primary = _make_channel(0, channel_pb2.Channel.Role.PRIMARY, "primary")

        plan = _SetUrlReplacePlan(
            max_channels=1,
            replace_original_channels_ref=[],
            replace_original_channels_snapshot=[],
            replace_original_channels_by_index={},
            staged_channels=[staged_primary],
            staged_channels_by_index={0: staged_primary},
            deferred_new_named_admin_channel=None,
            deferred_new_named_admin_index=None,
            deferred_previous_admin_slot_channel=None,
            replace_original_lora_config=None,
        )

        result = _SetUrlExecutionEngine._post_write_fallback_admin_index(plan)

        assert result == 0


# ============================================================================
# Tests for _SetUrlTransactionCoordinator
# ============================================================================


class TestSetUrlTransactionCoordinator:
    """Tests for _SetUrlTransactionCoordinator."""

    @pytest.mark.unit
    def test_init_with_none_localnode_raises_error(
        self, mock_local_node: MagicMock, mock_iface: MagicMock
    ) -> None:
        """__init__ with None localNode raises error (line 881)."""
        mock_iface.localNode = None  # No localNode available

        # Mock _raise_interface_error to raise an exception with the actual message
        def raise_error(msg: str) -> NoReturn:
            raise Exception(msg)

        mock_local_node._raise_interface_error = MagicMock(side_effect=raise_error)

        channel_set = apponly_pb2.ChannelSet()
        settings = channel_set.settings.add()
        settings.name = "test"

        parsed_input = _SetUrlParsedInput(
            url="https://meshtastic.org/d/#test",
            channel_set=channel_set,
            has_lora_update=False,
        )

        with pytest.raises(Exception, match="Interface localNode not initialized"):
            _SetUrlTransactionCoordinator(mock_local_node, parsed_input=parsed_input)

    @pytest.mark.unit
    def test_init_creates_components(self, mock_local_node: MagicMock) -> None:
        """__init__ creates cache manager, execution engine, and rollback engine."""
        channel_set = apponly_pb2.ChannelSet()
        settings = channel_set.settings.add()
        settings.name = "test"

        parsed_input = _SetUrlParsedInput(
            url="https://meshtastic.org/d/#test",
            channel_set=channel_set,
            has_lora_update=False,
        )

        coordinator = _SetUrlTransactionCoordinator(
            mock_local_node, parsed_input=parsed_input
        )

        assert coordinator._cache_manager is not None
        assert coordinator._execution_engine is not None
        assert coordinator._rollback_engine is not None

    @pytest.mark.unit
    def test_resolve_admin_context_returns_context(
        self, mock_local_node: MagicMock
    ) -> None:
        """_resolve_admin_context returns admin context with correct values."""
        mock_local_node._get_admin_channel_index.return_value = 1
        mock_local_node._get_named_admin_channel_index.return_value = 2

        channel_set = apponly_pb2.ChannelSet()
        settings = channel_set.settings.add()
        settings.name = "test"

        parsed_input = _SetUrlParsedInput(
            url="https://meshtastic.org/d/#test",
            channel_set=channel_set,
            has_lora_update=False,
        )

        coordinator = _SetUrlTransactionCoordinator(
            mock_local_node, parsed_input=parsed_input
        )

        assert coordinator._admin_context.admin_index_for_write == 1
        assert coordinator._admin_context.named_admin_index_for_write == 2
        assert coordinator._admin_context.has_admin_write_node_named_admin is True


# ============================================================================
# Tests for _SetUrlAddOnlyPlanner
# ============================================================================


class TestSetUrlAddOnlyPlanner:
    """Tests for _SetUrlAddOnlyPlanner."""

    @pytest.mark.unit
    def test_capture_original_lora_snapshot_without_lora_returns_none(
        self, mock_local_node: MagicMock
    ) -> None:
        """capture_original_lora_snapshot returns None when no LoRa update."""
        channel_set = apponly_pb2.ChannelSet()
        settings = channel_set.settings.add()
        settings.name = "test"

        parsed_input = _SetUrlParsedInput(
            url="https://meshtastic.org/d/#test",
            channel_set=channel_set,
            has_lora_update=False,
        )

        admin_context = MagicMock()
        planner = _SetUrlAddOnlyPlanner(
            mock_local_node,
            parsed_input=parsed_input,
            admin_context=admin_context,
        )

        result = planner.capture_original_lora_snapshot()

        assert result is None

    @pytest.mark.unit
    def test_capture_original_lora_snapshot_with_lora_returns_snapshot(
        self, mock_local_node: MagicMock
    ) -> None:
        """capture_original_lora_snapshot returns snapshot when LoRa update."""
        mock_local_node.localConfig.lora.hop_limit = 5

        channel_set = _make_channel_set_with_lora("test")

        parsed_input = _SetUrlParsedInput(
            url="https://meshtastic.org/d/#test",
            channel_set=channel_set,
            has_lora_update=True,
        )

        admin_context = MagicMock()
        planner = _SetUrlAddOnlyPlanner(
            mock_local_node,
            parsed_input=parsed_input,
            admin_context=admin_context,
        )

        result = planner.capture_original_lora_snapshot()

        assert result is not None
        assert result.hop_limit == 5

    @pytest.mark.unit
    def test_capture_original_lora_snapshot_without_loaded_lora_raises_error(
        self, mock_local_node: MagicMock
    ) -> None:
        """capture_original_lora_snapshot raises error when LoRa not loaded."""
        # localConfig exists but has no lora field set
        mock_local_node.localConfig = localonly_pb2.LocalConfig()

        # Mock _raise_interface_error to raise an exception with the actual message
        def raise_error(msg: str) -> NoReturn:
            raise Exception(msg)

        mock_local_node._raise_interface_error = MagicMock(side_effect=raise_error)

        channel_set = _make_channel_set_with_lora("test")

        parsed_input = _SetUrlParsedInput(
            url="https://meshtastic.org/d/#test",
            channel_set=channel_set,
            has_lora_update=True,
        )

        admin_context = MagicMock()
        planner = _SetUrlAddOnlyPlanner(
            mock_local_node,
            parsed_input=parsed_input,
            admin_context=admin_context,
        )

        with pytest.raises(Exception, match="LoRa config must be loaded"):
            planner.capture_original_lora_snapshot()


# ============================================================================
# Tests for _SetUrlReplacePlanner
# ============================================================================


class TestSetUrlReplacePlanner:
    """Tests for _SetUrlReplacePlanner."""

    @pytest.mark.unit
    def test_build_plan_with_valid_channels(self, mock_local_node: MagicMock) -> None:
        """build_plan creates staged channels from URL settings."""
        mock_local_node.channels = [
            _make_channel(0, channel_pb2.Channel.Role.PRIMARY, "old"),
            _make_channel(1, channel_pb2.Channel.Role.DISABLED),
        ]

        channel_set = apponly_pb2.ChannelSet()
        settings = channel_set.settings.add()
        settings.name = "newprimary"
        settings.psk = b"\x01"

        parsed_input = _SetUrlParsedInput(
            url="https://meshtastic.org/d/#test",
            channel_set=channel_set,
            has_lora_update=False,
        )

        admin_context = MagicMock()
        admin_context.named_admin_index_for_write = None

        planner = _SetUrlReplacePlanner(
            mock_local_node,
            parsed_input=parsed_input,
            admin_context=admin_context,
        )

        plan = planner.build_plan()

        assert plan.max_channels == 2
        assert len(plan.staged_channels) == 2
        assert plan.staged_channels[0].role == channel_pb2.Channel.Role.PRIMARY
        assert plan.staged_channels[1].role == channel_pb2.Channel.Role.DISABLED

    @pytest.mark.unit
    def test_build_plan_with_lora_requires_loaded_config(
        self, mock_local_node: MagicMock
    ) -> None:
        """build_plan with LoRa update requires loaded LoRa config (line 287)."""
        mock_local_node.channels = [
            _make_channel(0, channel_pb2.Channel.Role.PRIMARY, "old")
        ]
        # localConfig exists but has no lora field
        mock_local_node.localConfig = localonly_pb2.LocalConfig()

        # Mock _raise_interface_error to raise an exception with the actual message
        def raise_error(msg: str) -> NoReturn:
            raise Exception(msg)

        mock_local_node._raise_interface_error = MagicMock(side_effect=raise_error)

        channel_set = _make_channel_set_with_lora("test")

        parsed_input = _SetUrlParsedInput(
            url="https://meshtastic.org/d/#test",
            channel_set=channel_set,
            has_lora_update=True,
        )

        admin_context = MagicMock()
        admin_context.named_admin_index_for_write = None

        planner = _SetUrlReplacePlanner(
            mock_local_node,
            parsed_input=parsed_input,
            admin_context=admin_context,
        )

        with pytest.raises(Exception, match="LoRa config must be loaded before setURL"):
            planner.build_plan()

    @pytest.mark.unit
    def test_build_plan_with_too_many_channels_logs_warning(
        self, mock_local_node: MagicMock, caplog: LogCaptureFixture
    ) -> None:
        """build_plan with more URL channels than device slots logs warning."""
        mock_local_node.channels = [
            _make_channel(0, channel_pb2.Channel.Role.PRIMARY, "old"),
        ]

        # Create channel set with 3 channels (more than the 1 slot)
        channel_set = apponly_pb2.ChannelSet()
        for i in range(3):
            settings = channel_set.settings.add()
            settings.name = f"channel{i}"
            settings.psk = b"\x01"

        parsed_input = _SetUrlParsedInput(
            url="https://meshtastic.org/d/#test",
            channel_set=channel_set,
            has_lora_update=False,
        )

        admin_context = MagicMock()
        admin_context.named_admin_index_for_write = None

        planner = _SetUrlReplacePlanner(
            mock_local_node,
            parsed_input=parsed_input,
            admin_context=admin_context,
        )

        with caplog.at_level(logging.WARNING):
            plan = planner.build_plan()

        assert "more than 1 channels" in caplog.text
        # Only first channel should be staged
        assert len(plan.staged_channels) == 1

    @pytest.mark.unit
    def test_build_plan_with_multiple_admin_channels_raises_error(
        self, mock_local_node: MagicMock
    ) -> None:
        """build_plan with multiple admin channels raises error."""
        mock_local_node.channels = [
            _make_channel(0, channel_pb2.Channel.Role.PRIMARY, "old"),
            _make_channel(1, channel_pb2.Channel.Role.DISABLED),
        ]

        # Mock _raise_interface_error to raise an exception with the actual message
        def raise_error(msg: str) -> NoReturn:
            raise Exception(msg)

        mock_local_node._raise_interface_error = MagicMock(side_effect=raise_error)

        channel_set = apponly_pb2.ChannelSet()
        for i in range(2):
            settings = channel_set.settings.add()
            settings.name = "admin"  # Both named admin
            settings.psk = b"\x01"

        parsed_input = _SetUrlParsedInput(
            url="https://meshtastic.org/d/#test",
            channel_set=channel_set,
            has_lora_update=False,
        )

        admin_context = MagicMock()
        admin_context.named_admin_index_for_write = None

        planner = _SetUrlReplacePlanner(
            mock_local_node,
            parsed_input=parsed_input,
            admin_context=admin_context,
        )

        with pytest.raises(Exception, match="multiple channels named 'admin'"):
            planner.build_plan()


# ============================================================================
# Tests for Execution State Classes
# ============================================================================


class TestSetUrlAddOnlyExecutionState:
    """Tests for _SetUrlAddOnlyExecutionState."""

    @pytest.mark.unit
    def test_default_state(self) -> None:
        """Default state has empty written_indices and lora_write_started=False."""
        state = _SetUrlAddOnlyExecutionState()

        assert state.written_indices == []
        assert state.lora_write_started is False

    @pytest.mark.unit
    def test_state_tracks_writes(self) -> None:
        """State tracks written indices."""
        state = _SetUrlAddOnlyExecutionState()
        state.written_indices.append(0)
        state.written_indices.append(1)

        assert state.written_indices == [0, 1]


class TestSetUrlReplaceExecutionState:
    """Tests for _SetUrlReplaceExecutionState."""

    @pytest.mark.unit
    def test_default_state(self) -> None:
        """Default state has empty tracking lists."""
        state = _SetUrlReplaceExecutionState()

        assert state.written_channel_indices == []
        assert state.lora_write_started is False
        assert state.rollback_admin_indexes_for_write == []

    @pytest.mark.unit
    def test_state_with_initial_rollback_indexes(self) -> None:
        """State can be initialized with rollback admin indexes."""
        state = _SetUrlReplaceExecutionState(rollback_admin_indexes_for_write=[0, 1])

        assert state.rollback_admin_indexes_for_write == [0, 1]
