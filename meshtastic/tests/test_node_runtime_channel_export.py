"""Unit tests for the _NodeChannelExportRuntime class."""

import threading
from unittest.mock import MagicMock, patch

import pytest

from meshtastic.node_runtime.channel_export_runtime import _NodeChannelExportRuntime
from meshtastic.protobuf import channel_pb2, localonly_pb2


@pytest.fixture
def mock_node() -> MagicMock:
    """Provide a minimal mock Node with channels list, localConfig, and lock.

    Returns
    -------
    MagicMock
        A mock Node with channels attribute, localConfig, _channels_lock,
        and _raise_interface_error method.
    """
    node = MagicMock(
        spec=[
            "channels",
            "_channels_lock",
            "localConfig",
            "_raise_interface_error",
            "requestConfig",
            "_write_channel_snapshot",
        ]
    )
    node._channels_lock = threading.RLock()
    node.channels = []
    node.localConfig = localonly_pb2.LocalConfig()
    node._raise_interface_error = MagicMock(side_effect=Exception)
    node.requestConfig = MagicMock()
    node._write_channel_snapshot = MagicMock()
    return node


@pytest.fixture
def export_runtime(mock_node: MagicMock) -> _NodeChannelExportRuntime:
    """Provide a _NodeChannelExportRuntime instance bound to the mock node.

    Parameters
    ----------
    mock_node : MagicMock
        The mock node fixture.

    Returns
    -------
    _NodeChannelExportRuntime
        The runtime instance under test.
    """
    return _NodeChannelExportRuntime(mock_node)


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


@pytest.mark.unit
def test_snapshot_channels_with_channels(
    export_runtime: _NodeChannelExportRuntime, mock_node: MagicMock
) -> None:
    """_snapshot_channels returns a list of detached channel copies."""
    primary = _make_channel(0, channel_pb2.Channel.Role.PRIMARY, name="primary")
    secondary = _make_channel(1, channel_pb2.Channel.Role.SECONDARY, name="secondary")
    mock_node.channels = [primary, secondary]

    snapshot = export_runtime._snapshot_channels()

    assert len(snapshot) == 2
    assert snapshot[0] is not primary
    assert snapshot[0].settings.name == "primary"
    assert snapshot[1] is not secondary
    assert snapshot[1].settings.name == "secondary"


@pytest.mark.unit
def test_snapshot_channels_with_none_channels(
    export_runtime: _NodeChannelExportRuntime, mock_node: MagicMock
) -> None:
    """_snapshot_channels returns empty list when channels is None."""
    mock_node.channels = None

    snapshot = export_runtime._snapshot_channels()

    assert snapshot == []


@pytest.mark.unit
def test_snapshot_channels_modification_isolated(
    export_runtime: _NodeChannelExportRuntime, mock_node: MagicMock
) -> None:
    """Modifying the snapshot does not affect the original channels."""
    primary = _make_channel(0, channel_pb2.Channel.Role.PRIMARY, name="original")
    mock_node.channels = [primary]

    snapshot = export_runtime._snapshot_channels()
    assert len(snapshot) == 1

    snapshot[0].settings.name = "modified"

    assert primary.settings.name == "original"


@pytest.mark.unit
def test_snapshot_local_config(
    export_runtime: _NodeChannelExportRuntime, mock_node: MagicMock
) -> None:
    """_snapshot_local_config returns a detached copy of localConfig."""
    mock_node.localConfig.lora.hop_limit = 5

    snapshot = export_runtime._snapshot_local_config()

    assert snapshot is not mock_node.localConfig
    assert snapshot.lora.hop_limit == 5


@pytest.mark.unit
def test_snapshot_local_config_modification_isolated(
    export_runtime: _NodeChannelExportRuntime, mock_node: MagicMock
) -> None:
    """Modifying the localConfig snapshot does not affect the original."""
    mock_node.localConfig.lora.hop_limit = 5

    snapshot = export_runtime._snapshot_local_config()
    snapshot.lora.hop_limit = 10

    assert mock_node.localConfig.lora.hop_limit == 5


@pytest.mark.unit
def test_get_url_with_channels(
    export_runtime: _NodeChannelExportRuntime, mock_node: MagicMock
) -> None:
    """get_url builds a valid channel URL export."""
    primary = _make_channel(
        0, channel_pb2.Channel.Role.PRIMARY, name="primary", psk=b"\x01"
    )
    secondary = _make_channel(
        1, channel_pb2.Channel.Role.SECONDARY, name="secondary", psk=b"\x02"
    )
    mock_node.channels = [primary, secondary]
    mock_node.localConfig.lora.hop_limit = 3

    url = export_runtime.get_url()

    assert url.startswith("https://meshtastic.org/e/#")


@pytest.mark.unit
def test_get_url_include_all_false(
    export_runtime: _NodeChannelExportRuntime, mock_node: MagicMock
) -> None:
    """get_url with include_all=False only includes PRIMARY channel."""
    primary = _make_channel(
        0, channel_pb2.Channel.Role.PRIMARY, name="primary", psk=b"\x01"
    )
    secondary = _make_channel(
        1, channel_pb2.Channel.Role.SECONDARY, name="secondary", psk=b"\x02"
    )
    mock_node.channels = [primary, secondary]
    mock_node.localConfig.lora.hop_limit = 3

    url_primary_only = export_runtime.get_url(include_all=False)
    url_all = export_runtime.get_url(include_all=True)

    assert url_primary_only.startswith("https://meshtastic.org/e/#")
    assert len(url_primary_only) < len(url_all)


@pytest.mark.unit
def test_get_url_include_all_true(
    export_runtime: _NodeChannelExportRuntime, mock_node: MagicMock
) -> None:
    """get_url with include_all=True includes PRIMARY and SECONDARY channels."""
    primary = _make_channel(
        0, channel_pb2.Channel.Role.PRIMARY, name="primary", psk=b"\x01"
    )
    secondary = _make_channel(
        1, channel_pb2.Channel.Role.SECONDARY, name="secondary", psk=b"\x02"
    )
    mock_node.channels = [primary, secondary]
    mock_node.localConfig.lora.hop_limit = 3

    url = export_runtime.get_url(include_all=True)

    assert url.startswith("https://meshtastic.org/e/#")


@pytest.mark.unit
def test_get_url_requests_config_when_lora_missing(
    export_runtime: _NodeChannelExportRuntime, mock_node: MagicMock
) -> None:
    """get_url requests config when lora field is missing from localConfig."""
    primary = _make_channel(
        0, channel_pb2.Channel.Role.PRIMARY, name="primary", psk=b"\x01"
    )
    mock_node.channels = [primary]
    # Simulate HasField("lora") returning False initially
    # After requestConfig, localConfig will have lora populated
    mock_node.localConfig = localonly_pb2.LocalConfig()

    call_count = {"lora": 0}

    def _has_field_side_effect(field_name: str) -> bool:
        if field_name == "lora":
            call_count["lora"] += 1
            return call_count["lora"] > 1
        return True

    with patch.object(
        localonly_pb2.LocalConfig,
        "HasField",
        side_effect=_has_field_side_effect,
    ):
        # The method will call requestConfig, then snapshot again
        # For the test to complete, we need to make the second snapshot work
        url = export_runtime.get_url()

        assert mock_node.requestConfig.called
        assert url.startswith("https://meshtastic.org/e/#")


@pytest.mark.unit
def test_get_channels_with_hash_named_channel_with_psk(
    export_runtime: _NodeChannelExportRuntime, mock_node: MagicMock
) -> None:
    """get_channels_with_hash returns hash for named channel with PSK."""
    primary = _make_channel(
        0, channel_pb2.Channel.Role.PRIMARY, name="primary", psk=b"\x01"
    )
    mock_node.channels = [primary]

    result = export_runtime.get_channels_with_hash()

    assert len(result) == 1
    assert result[0]["index"] == 0
    assert result[0]["role"] == "PRIMARY"
    assert result[0]["name"] == "primary"
    assert result[0]["hash"] is not None
    assert isinstance(result[0]["hash"], int)


@pytest.mark.unit
def test_get_channels_with_hash_channel_without_psk(
    export_runtime: _NodeChannelExportRuntime, mock_node: MagicMock
) -> None:
    """get_channels_with_hash returns None hash for channel without PSK."""
    primary = _make_channel(
        0, channel_pb2.Channel.Role.PRIMARY, name="primary", psk=b""
    )
    mock_node.channels = [primary]

    result = export_runtime.get_channels_with_hash()

    assert len(result) == 1
    assert result[0]["index"] == 0
    assert result[0]["role"] == "PRIMARY"
    assert result[0]["name"] == "primary"
    assert result[0]["hash"] is None


@pytest.mark.unit
def test_get_channels_with_hash_channel_without_name(
    export_runtime: _NodeChannelExportRuntime, mock_node: MagicMock
) -> None:
    """get_channels_with_hash returns None hash for channel without name."""
    primary = _make_channel(0, channel_pb2.Channel.Role.PRIMARY, name="", psk=b"\x01")
    mock_node.channels = [primary]

    result = export_runtime.get_channels_with_hash()

    assert len(result) == 1
    assert result[0]["index"] == 0
    assert result[0]["name"] == ""
    assert result[0]["hash"] is None


@pytest.mark.unit
def test_get_channels_with_hash_none_channels(
    export_runtime: _NodeChannelExportRuntime, mock_node: MagicMock
) -> None:
    """get_channels_with_hash returns empty list when channels is None."""
    mock_node.channels = None

    result = export_runtime.get_channels_with_hash()

    assert result == []


@pytest.mark.unit
def test_get_channels_with_hash_multiple_channels(
    export_runtime: _NodeChannelExportRuntime, mock_node: MagicMock
) -> None:
    """get_channels_with_hash returns descriptors for multiple channels."""
    primary = _make_channel(
        0, channel_pb2.Channel.Role.PRIMARY, name="primary", psk=b"\x01"
    )
    secondary = _make_channel(
        1, channel_pb2.Channel.Role.SECONDARY, name="secondary", psk=b"\x02"
    )
    disabled = _make_channel(2, channel_pb2.Channel.Role.DISABLED)
    mock_node.channels = [primary, secondary, disabled]

    result = export_runtime.get_channels_with_hash()

    assert len(result) == 3
    assert result[0]["role"] == "PRIMARY"
    assert result[1]["role"] == "SECONDARY"
    assert result[2]["role"] == "DISABLED"


@pytest.mark.unit
def test_turn_off_encryption_on_primary_channel_no_channels(
    export_runtime: _NodeChannelExportRuntime, mock_node: MagicMock
) -> None:
    """turn_off_encryption_on_primary_channel raises error when no channels available."""
    mock_node.channels = []
    mock_node._raise_interface_error.side_effect = Exception(
        "Error: No channels have been read"
    )

    with pytest.raises(Exception, match="Error: No channels have been read"):
        export_runtime.turn_off_encryption_on_primary_channel()

    mock_node._raise_interface_error.assert_called_once_with(
        "Error: No channels have been read"
    )


@pytest.mark.unit
def test_turn_off_encryption_on_primary_channel_no_primary(
    export_runtime: _NodeChannelExportRuntime, mock_node: MagicMock
) -> None:
    """turn_off_encryption_on_primary_channel raises error when no primary channel found."""
    secondary = _make_channel(1, channel_pb2.Channel.Role.SECONDARY, name="secondary")
    disabled = _make_channel(2, channel_pb2.Channel.Role.DISABLED)
    mock_node.channels = [secondary, disabled]
    mock_node._raise_interface_error.side_effect = Exception(
        "Error: No primary channel found"
    )

    with pytest.raises(Exception, match="Error: No primary channel found"):
        export_runtime.turn_off_encryption_on_primary_channel()

    mock_node._raise_interface_error.assert_called_once_with(
        "Error: No primary channel found"
    )


@pytest.mark.unit
def test_turn_off_encryption_on_primary_channel_with_primary(
    export_runtime: _NodeChannelExportRuntime, mock_node: MagicMock
) -> None:
    """turn_off_encryption_on_primary_channel updates primary channel PSK to 'none'."""
    primary = _make_channel(
        0, channel_pb2.Channel.Role.PRIMARY, name="primary", psk=b"\x01\x02\x03"
    )
    mock_node.channels = [primary]

    export_runtime.turn_off_encryption_on_primary_channel()

    mock_node._write_channel_snapshot.assert_called_once()
    # Verify the channel passed to _write_channel_snapshot has empty PSK
    written_channel = mock_node._write_channel_snapshot.call_args[0][0]
    assert written_channel.settings.psk == b"\x00"  # fromPSK("none") returns b'\x00'


@pytest.mark.unit
def test_turn_off_encryption_on_primary_channel_updates_local_cache(
    export_runtime: _NodeChannelExportRuntime, mock_node: MagicMock
) -> None:
    """turn_off_encryption_on_primary_channel updates local channel cache after write."""
    primary = _make_channel(
        0, channel_pb2.Channel.Role.PRIMARY, name="primary", psk=b"\x01\x02\x03"
    )
    mock_node.channels = [primary]

    export_runtime.turn_off_encryption_on_primary_channel()

    # After successful write, local cache should be updated
    assert primary.settings.psk == b"\x00"  # Updated to "none" PSK


@pytest.mark.unit
def test_turn_off_encryption_on_primary_channel_no_cache_after_write(
    export_runtime: _NodeChannelExportRuntime,
    mock_node: MagicMock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """turn_off_encryption_on_primary_channel logs warning when cache unavailable after write."""
    primary = _make_channel(
        0, channel_pb2.Channel.Role.PRIMARY, name="primary", psk=b"\x01"
    )
    mock_node.channels = [primary]

    # After write, set channels to None to simulate unavailable cache
    def write_side_effect(channel):
        mock_node.channels = None

    mock_node._write_channel_snapshot.side_effect = write_side_effect

    with caplog.at_level("WARNING"):
        export_runtime.turn_off_encryption_on_primary_channel()

    assert "local channel cache is unavailable" in caplog.text


@pytest.mark.unit
def test_turn_off_encryption_on_primary_channel_index_not_found(
    export_runtime: _NodeChannelExportRuntime,
    mock_node: MagicMock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """turn_off_encryption_on_primary_channel logs warning when index not in cache."""
    primary = _make_channel(
        0, channel_pb2.Channel.Role.PRIMARY, name="primary", psk=b"\x01"
    )
    mock_node.channels = [primary]

    # After write, change the channels list to not include the primary index
    def write_side_effect(channel):
        # Replace with a different channel at different index
        other = _make_channel(5, channel_pb2.Channel.Role.SECONDARY, name="other")
        mock_node.channels = [other]

    mock_node._write_channel_snapshot.side_effect = write_side_effect

    with caplog.at_level("WARNING"):
        export_runtime.turn_off_encryption_on_primary_channel()

    assert "invalidating local channel cache" in caplog.text
    assert mock_node.channels is None
