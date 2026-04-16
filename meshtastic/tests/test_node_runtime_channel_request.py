"""Unit tests for channel_request_runtime module."""

# pylint: disable=redefined-outer-name

import logging
import threading
import warnings
from unittest.mock import MagicMock

import pytest

from meshtastic.node_runtime.channel_normalization_runtime import (
    _NodeChannelNormalizationRuntime,
)
from meshtastic.node_runtime.channel_request_runtime import _NodeChannelRequestRuntime
from meshtastic.node_runtime.shared import MAX_CHANNELS
from meshtastic.protobuf import admin_pb2, channel_pb2, localonly_pb2, mesh_pb2


@pytest.fixture
def mock_node() -> MagicMock:
    """Create a minimal mock Node with required attributes for channel request runtime tests.

    Returns
    -------
    MagicMock
        A mock configured with nodeNum, _channels_lock, channels, partialChannels,
        and _send_admin method.
    """
    node = MagicMock(
        spec=[
            "nodeNum",
            "_channels_lock",
            "channels",
            "partialChannels",
            "_send_admin",
            "_timeout",
            "iface",
            "onResponseRequestChannel",
            "localConfig",
        ]
    )
    node.nodeNum = 1234567890
    node._channels_lock = threading.RLock()  # noqa: SLF001
    node.channels = None
    node.partialChannels = []
    node._send_admin = MagicMock(return_value=mesh_pb2.MeshPacket())  # noqa: SLF001
    node.localConfig = localonly_pb2.LocalConfig()
    return node


@pytest.fixture
def mock_normalization_runtime(
    mock_node: MagicMock,
) -> _NodeChannelNormalizationRuntime:
    """Create a real normalization runtime instance for the mock node.

    Parameters
    ----------
    mock_node : MagicMock
        The mock node fixture.

    Returns
    -------
    _NodeChannelNormalizationRuntime
        A real normalization runtime instance attached to the mock node.
    """
    return _NodeChannelNormalizationRuntime(mock_node)


@pytest.fixture
def channel_request_runtime(
    mock_node: MagicMock, mock_normalization_runtime: _NodeChannelNormalizationRuntime
) -> _NodeChannelRequestRuntime:
    """Create a channel request runtime instance for testing.

    Parameters
    ----------
    mock_node : MagicMock
        The mock node fixture.
    mock_normalization_runtime : _NodeChannelNormalizationRuntime
        The normalization runtime fixture.

    Returns
    -------
    _NodeChannelRequestRuntime
        A channel request runtime instance attached to the mock node.
    """
    return _NodeChannelRequestRuntime(
        mock_node, normalization_runtime=mock_normalization_runtime
    )


@pytest.mark.unit
def test_setChannels_with_valid_channels_copies_and_normalizes(
    channel_request_runtime: _NodeChannelRequestRuntime,
    mock_node: MagicMock,
) -> None:
    """SetChannels should copy input channels and call fixup_channels_locked.

    Note: fixup_channels_locked calls fill_channels_locked which pads the channel
    list to MAX_CHANNELS (8) with DISABLED channels.
    """
    # Create source channels
    source_channel1 = channel_pb2.Channel()
    source_channel1.index = 0
    source_channel1.role = channel_pb2.Channel.Role.PRIMARY

    source_channel2 = channel_pb2.Channel()
    source_channel2.index = 1
    source_channel2.role = channel_pb2.Channel.Role.SECONDARY

    source_channels = [source_channel1, source_channel2]

    channel_request_runtime.setChannels(source_channels)

    # Verify channels were copied (not the same objects)
    assert mock_node.channels is not None
    assert len(mock_node.channels) == MAX_CHANNELS
    # Verify they are copies, not the same objects
    assert mock_node.channels[0] is not source_channel1
    assert mock_node.channels[1] is not source_channel2
    # Verify content was copied for first two channels
    assert mock_node.channels[0].index == 0
    assert mock_node.channels[0].role == channel_pb2.Channel.Role.PRIMARY
    assert mock_node.channels[1].index == 1
    assert mock_node.channels[1].role == channel_pb2.Channel.Role.SECONDARY
    # Verify remaining channels are DISABLED (filled by fill_channels_locked)
    for i in range(2, MAX_CHANNELS):
        assert mock_node.channels[i].role == channel_pb2.Channel.Role.DISABLED


@pytest.mark.unit
def test_requestChannels_with_starting_index_zero_resets_channels(
    channel_request_runtime: _NodeChannelRequestRuntime,
    mock_node: MagicMock,
) -> None:
    """RequestChannels with starting_index=0 should reset channels and partialChannels."""
    # Set up pre-existing channels
    mock_node.channels = [channel_pb2.Channel()]
    mock_node.partialChannels = [channel_pb2.Channel()]

    channel_request_runtime.requestChannels(starting_index=0)

    # Verify channels were reset
    assert mock_node.channels is None
    assert mock_node.partialChannels == []
    # Verify request_channel was called
    mock_node._send_admin.assert_called_once()  # noqa: SLF001


@pytest.mark.unit
def test_requestChannels_with_starting_index_nonzero_preserves_channels(
    channel_request_runtime: _NodeChannelRequestRuntime,
    mock_node: MagicMock,
) -> None:
    """RequestChannels with starting_index>0 should not reset channels or partialChannels."""
    # Set up pre-existing channels
    existing_channel = channel_pb2.Channel()
    mock_node.channels = [existing_channel]
    mock_node.partialChannels = [channel_pb2.Channel()]

    channel_request_runtime.requestChannels(starting_index=2)

    # Verify channels were NOT reset
    assert mock_node.channels == [existing_channel]
    assert len(mock_node.partialChannels) == 1
    # Verify request_channel was still called
    mock_node._send_admin.assert_called_once()  # noqa: SLF001


@pytest.mark.unit
def test_requestChannels_calls_canonical_request_channel_method(
    channel_request_runtime: _NodeChannelRequestRuntime,
) -> None:
    """RequestChannels should call requestChannel directly, not deprecated alias."""
    channel_request_runtime.requestChannel = MagicMock()  # type: ignore[method-assign]
    channel_request_runtime.request_channel = MagicMock()  # type: ignore[method-assign]

    channel_request_runtime.requestChannels(starting_index=3)

    channel_request_runtime.requestChannel.assert_called_once_with(3)
    channel_request_runtime.request_channel.assert_not_called()


@pytest.mark.unit
def test_waitForConfig_delegates_to_timeout_wait_for_set(
    channel_request_runtime: _NodeChannelRequestRuntime,
    mock_node: MagicMock,
) -> None:
    """WaitForConfig should delegate to _timeout.waitForSet with correct attributes."""
    mock_timeout = MagicMock()
    mock_timeout.waitForSet.return_value = True
    mock_node._timeout = mock_timeout  # noqa: SLF001

    result = channel_request_runtime.waitForConfig(attribute="channels")

    assert result is True
    mock_timeout.waitForSet.assert_called_once_with(mock_node, attrs=("channels",))


@pytest.mark.unit
def test_waitForConfig_with_non_channels_attribute(
    channel_request_runtime: _NodeChannelRequestRuntime,
    mock_node: MagicMock,
) -> None:
    """WaitForConfig should poll localConfig field presence for nested attributes."""
    mock_timeout = MagicMock()
    mock_timeout.waitForSet.return_value = True
    mock_node._timeout = mock_timeout  # noqa: SLF001

    result = channel_request_runtime.waitForConfig(attribute="lora")

    assert result is True
    call_args = mock_timeout.waitForSet.call_args
    assert call_args is not None
    target = call_args[0][0]
    assert hasattr(target, "is_set")
    mock_timeout.waitForSet.assert_called_once_with(target, attrs=("is_set",))


@pytest.mark.unit
def test_request_channel_sends_admin_message_with_correct_channel_num(
    channel_request_runtime: _NodeChannelRequestRuntime,
    mock_node: MagicMock,
) -> None:
    """RequestChannel should send admin message with channel_num+1 as get_channel_request."""
    # Set up node to be the same as localNode (local request)
    mock_iface = MagicMock()
    mock_iface.localNode = mock_node
    mock_node.iface = mock_iface

    mock_node._send_admin.return_value = mesh_pb2.MeshPacket()  # noqa: SLF001

    result = channel_request_runtime.requestChannel(channel_num=3)

    # Verify _send_admin was called with correct message
    mock_node._send_admin.assert_called_once()  # noqa: SLF001
    call_args = mock_node._send_admin.call_args  # noqa: SLF001
    sent_message = call_args[0][0]

    assert isinstance(sent_message, admin_pb2.AdminMessage)
    assert sent_message.get_channel_request == 4  # channel_num + 1

    # Verify wantResponse and onResponse were passed
    assert call_args[1]["wantResponse"] is True
    assert call_args[1]["onResponse"] == mock_node.onResponseRequestChannel

    # Verify return value
    assert isinstance(result, mesh_pb2.MeshPacket)


@pytest.mark.unit
def test_request_channel_for_remote_node_logs_info(
    channel_request_runtime: _NodeChannelRequestRuntime,
    mock_node: MagicMock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """RequestChannel for remote node should log info message."""
    # Set up node to be different from localNode (remote request)
    mock_iface = MagicMock()
    mock_local_node = MagicMock()
    mock_local_node.nodeNum = 987654321  # Different from mock_node.nodeNum
    mock_iface.localNode = mock_local_node
    mock_node.iface = mock_iface

    with caplog.at_level(logging.INFO):
        channel_request_runtime.requestChannel(channel_num=5)

    # Verify info log was emitted for remote node
    assert "Requesting channel 5 info from remote node" in caplog.text

    # Verify _send_admin was still called
    mock_node._send_admin.assert_called_once()  # noqa: SLF001


@pytest.mark.unit
def test_request_channel_for_local_node_logs_debug(
    channel_request_runtime: _NodeChannelRequestRuntime,
    mock_node: MagicMock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """RequestChannel for local node should log debug message."""
    # Set up node to be the same as localNode (local request)
    mock_iface = MagicMock()
    mock_iface.localNode = mock_node
    mock_node.iface = mock_iface

    with caplog.at_level(logging.DEBUG):
        channel_request_runtime.requestChannel(channel_num=2)

    # Verify debug log was emitted for local node
    assert "Requesting channel 2" in caplog.text
    # Verify remote message is NOT logged
    assert "remote node" not in caplog.text.lower()


@pytest.mark.unit
def test_request_channel_send_exception_marks_request_failed(
    channel_request_runtime: _NodeChannelRequestRuntime,
    mock_node: MagicMock,
) -> None:
    """RequestChannel should clear in-flight state when _send_admin raises."""
    mock_iface = MagicMock()
    mock_iface.localNode = mock_node
    mock_node.iface = mock_iface

    channel_response_runtime = MagicMock()
    mock_node._channel_response_runtime = channel_response_runtime  # type: ignore[attr-defined]
    mock_node._send_admin.side_effect = RuntimeError("send failed")  # noqa: SLF001

    with pytest.raises(RuntimeError, match="send failed"):
        channel_request_runtime.requestChannel(channel_num=1)

    channel_response_runtime.markChannelRequestSent.assert_called_once_with(1)
    channel_response_runtime.markChannelRequestSendFailed.assert_called_once_with(1)


@pytest.mark.unit
def test_request_channel_is_silent_compatibility_shim(
    channel_request_runtime: _NodeChannelRequestRuntime,
    mock_node: MagicMock,
) -> None:
    """request_channel should be a silent compatibility shim without warnings."""
    mock_iface = MagicMock()
    mock_iface.localNode = mock_node
    mock_node.iface = mock_iface

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        channel_request_runtime.request_channel(channel_num=0)
        channel_request_runtime.request_channel(channel_num=1)

    deprecation_warnings = [
        warning
        for warning in caught
        if issubclass(warning.category, DeprecationWarning)
    ]
    assert len(deprecation_warnings) == 0


@pytest.mark.unit
def test_timeout_for_field_skips_unknown_field(
    channel_request_runtime: _NodeChannelRequestRuntime,
    mock_node: MagicMock,
) -> None:
    """_timeout_for_field returns True for fields not in protobuf descriptor."""
    mock_local_config = MagicMock()
    mock_local_config.DESCRIPTOR.fields_by_name = {}
    mock_node.localConfig = mock_local_config
    result = channel_request_runtime._timeout_for_field("nonexistent_field", 0.1)
    assert result is True


@pytest.mark.unit
def test_timeout_for_field_returns_true_for_populated_field(
    channel_request_runtime: _NodeChannelRequestRuntime,
    mock_node: MagicMock,
) -> None:
    """_timeout_for_field returns True when field is already populated."""
    mock_local_config = MagicMock()
    mock_local_config.DESCRIPTOR.fields_by_name = {"lora": MagicMock()}
    mock_local_config.HasField.return_value = True
    mock_node.localConfig = mock_local_config
    result = channel_request_runtime._timeout_for_field("lora", 0.1)
    assert result is True


@pytest.mark.unit
def test_timeout_for_field_returns_false_on_timeout(
    channel_request_runtime: _NodeChannelRequestRuntime,
    mock_node: MagicMock,
) -> None:
    """_timeout_for_field returns False when field is not populated within timeout."""
    mock_local_config = MagicMock()
    mock_local_config.DESCRIPTOR.fields_by_name = {"lora": MagicMock()}
    mock_local_config.HasField.return_value = False
    mock_node.localConfig = mock_local_config
    result = channel_request_runtime._timeout_for_field("lora", 0.1)
    assert result is False
