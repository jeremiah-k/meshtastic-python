"""Meshtastic unit tests for node.py."""

import logging
import re
import warnings
from collections.abc import Callable
from typing import Any, cast
from unittest.mock import MagicMock, create_autospec, patch

import pytest
from pytest import CaptureFixture, LogCaptureFixture

from ..mesh_interface import MeshInterface
from ..node import MAX_CHANNELS, Node
from ..protobuf import (
    admin_pb2,
    localonly_pb2,
    mesh_pb2,
)
from ..protobuf.channel_pb2 import Channel  # pylint: disable=E0611
from ..serial_interface import SerialInterface
from ..util import fromPSK

from ._node_legacy_support import (
    _decode_channel_set_from_url,
    _get_mock_call_arg,
    _make_fake_send_admin,
)

CHANNEL_LIMIT = MAX_CHANNELS


@pytest.mark.unit
def test_getChannelByChannelIndex(
    autospec_local_node_iface: Callable[[type[Any]], MagicMock],
) -> None:
    """Test getChannelByChannelIndex()."""
    anode = Node(autospec_local_node_iface(MeshInterface), "!12345678", noProto=True)

    channel1 = Channel(index=0, role=Channel.Role.PRIMARY)  # primary channel
    channel2 = Channel(index=1, role=Channel.Role.SECONDARY)  # secondary channel
    channel3 = Channel(index=2, role=Channel.Role.DISABLED)
    channel4 = Channel(index=3, role=Channel.Role.DISABLED)
    channel5 = Channel(index=4, role=Channel.Role.DISABLED)
    channel6 = Channel(index=5, role=Channel.Role.DISABLED)
    channel7 = Channel(index=6, role=Channel.Role.DISABLED)
    channel8 = Channel(index=7, role=Channel.Role.DISABLED)

    channels = [
        channel1,
        channel2,
        channel3,
        channel4,
        channel5,
        channel6,
        channel7,
        channel8,
    ]

    anode.channels = channels

    # test primary
    selected_primary = anode.getChannelByChannelIndex(0)
    assert selected_primary is not None
    assert selected_primary is channel1
    # test secondary
    assert anode.getChannelByChannelIndex(1) is not None
    # test disabled
    assert anode.getChannelByChannelIndex(2) is not None
    # test invalid values
    assert anode.getChannelByChannelIndex(-1) is None
    assert anode.getChannelByChannelIndex(CHANNEL_LIMIT) is None

    copied_primary = anode.getChannelCopyByChannelIndex(0)
    assert copied_primary is not None
    assert copied_primary is not channel1
    copied_primary.role = Channel.Role.DISABLED
    assert channel1.role == Channel.Role.PRIMARY


@pytest.mark.unit
def test_writeConfig_with_no_radioConfig(
    autospec_local_node_iface: Callable[[type[Any]], MagicMock],
) -> None:
    """Test writeConfig raises MeshInterfaceError for invalid config name."""
    anode = Node(autospec_local_node_iface(MeshInterface), "!12345678", noProto=True)

    with pytest.raises(
        MeshInterface.MeshInterfaceError,
        match="Error: No valid config with name foo",
    ):
        anode.writeConfig("foo")


@pytest.mark.unit
def test_writeChannel_with_no_channels_raises_mesh_error(
    autospec_local_node_iface: Callable[[type[Any]], MagicMock],
) -> None:
    """Test writeChannel raises when channels have not been loaded."""
    anode = Node(autospec_local_node_iface(MeshInterface), "!12345678", noProto=True)
    anode.channels = None

    with pytest.raises(
        MeshInterface.MeshInterfaceError, match="Error: No channels have been read"
    ):
        anode.writeChannel(0)


@pytest.mark.unit
def test_writeChannel_forwards_admin_index_to_session_key_bootstrap(
    autospec_local_node_iface: Callable[[type[Any]], MagicMock],
) -> None:
    """WriteChannel should use the same admin index for session bootstrap and channel write."""
    anode = Node(autospec_local_node_iface(MeshInterface), "!12345678", noProto=True)
    primary = Channel(index=0, role=Channel.Role.PRIMARY)
    primary.settings.name = "primary"
    primary.settings.psk = b"\x01"
    anode.channels = [primary]
    anode.ensureSessionKey = MagicMock()  # type: ignore[method-assign]
    anode._send_admin = MagicMock(return_value=mesh_pb2.MeshPacket())  # type: ignore[method-assign]

    anode.writeChannel(0, adminIndex=4)

    assert (
        _get_mock_call_arg(
            anode.ensureSessionKey.call_args,
            name="adminIndex",
            positional_index=0,
        )
        == 4
    )
    assert (
        _get_mock_call_arg(
            anode._send_admin.call_args,
            name="adminIndex",
            positional_index=3,
        )
        == 4
    )


@pytest.mark.unit
def test_writeConfig_traffic_management(
    autospec_local_node_iface: Callable[[type[Any]], MagicMock],
) -> None:
    """Test writeConfig writes traffic_management module config through set_module_config.

    The bool toggles (``enabled``, ``rate_limit_enabled``, ...) were removed
    from the protobuf in favour of the "non-zero implies enabled" convention
    on their companion uint32 fields, so we exercise that convention here.
    """
    iface = autospec_local_node_iface(MeshInterface)
    anode = Node(iface, "!12345678", noProto=True)
    tm = anode.moduleConfig.traffic_management
    tm.position_min_interval_secs = 30
    tm.rate_limit_window_secs = 60
    tm.rate_limit_max_packets = 100

    sent_messages: list[admin_pb2.AdminMessage] = []
    anode._send_admin = _make_fake_send_admin(  # type: ignore[method-assign,assignment]
        sent_messages=sent_messages
    )

    anode.writeConfig("traffic_management")

    assert len(sent_messages) == 1
    sent_message = sent_messages[0]
    assert sent_message.HasField("set_module_config")
    assert sent_message.set_module_config.HasField("traffic_management")
    result = sent_message.set_module_config.traffic_management
    assert result.position_min_interval_secs == 30
    assert result.rate_limit_window_secs == 60
    assert result.rate_limit_max_packets == 100


@pytest.mark.unit
def test_requestChannel_not_localNode(
    caplog: LogCaptureFixture, mock_serial_interface: MagicMock
) -> None:
    """Verify that requesting channel 0 on a non-local node logs a remote channel info request.

    Sets up a mocked SerialInterface and a Node that is not the local node, configures max channels,
    calls _request_channel(0), and asserts that an INFO log contains "Requesting channel 0 info".

    """
    iface = mock_serial_interface
    anode = Node(iface, "!12345678", noProto=True)
    with caplog.at_level(logging.INFO):
        anode._request_channel(0)
        assert re.search(
            r"Requesting channel 0 info from remote node", caplog.text, re.MULTILINE
        )


@pytest.mark.unit
def test_requestChannel_localNode(
    caplog: LogCaptureFixture, mock_serial_interface: MagicMock
) -> None:
    """Verify that a local node logs a local channel request when _request_channel is called.

    Checks that the log contains "Requesting channel 0" and does not include "from remote node".

    """
    iface = mock_serial_interface
    anode = Node(iface, "!12345678", noProto=True)
    iface.localNode = anode

    with caplog.at_level(logging.DEBUG):
        anode._request_channel(0)
        assert re.search(r"Requesting channel 0", caplog.text, re.MULTILINE)
        assert not re.search(r"from remote node", caplog.text, re.MULTILINE)


@pytest.mark.unit
def test_requestChannels_non_localNode(
    caplog: LogCaptureFixture, mock_serial_interface: MagicMock
) -> None:
    """Test requestChannels() with a starting index of 0."""
    iface = mock_serial_interface
    anode = Node(iface, "!12345678", noProto=True)
    # Set a sentinel value to verify it gets reset
    anode.partialChannels = [Channel()]
    with caplog.at_level(logging.DEBUG):
        anode.requestChannels(0)
        assert re.search(
            "Requesting channel 0 info from remote node", caplog.text, re.MULTILINE
        )
        assert not anode.partialChannels


@pytest.mark.unit
def test_requestChannels_non_localNode_starting_index(
    caplog: LogCaptureFixture, mock_serial_interface: MagicMock
) -> None:
    """Test requestChannels() with a starting index of non-0."""
    iface = mock_serial_interface
    anode = Node(iface, "!12345678", noProto=True)
    sentinel_channel = Channel()
    anode.partialChannels = [sentinel_channel]
    with caplog.at_level(logging.DEBUG):
        anode.requestChannels(3)
        assert re.search(
            "Requesting channel 3 info from remote node", caplog.text, re.MULTILINE
        )
        # make sure it hasn't been initialized (identity check ensures list wasn't replaced)
        assert (
            len(anode.partialChannels) == 1
            and anode.partialChannels[0] is sentinel_channel
        )


@pytest.mark.unit
@pytest.mark.parametrize("node_id", ["!1dec0ded", 502009325])
@pytest.mark.parametrize(
    ("method_name", "field_name"),
    [
        ("setFavorite", "set_favorite_node"),
        ("removeFavorite", "remove_favorite_node"),
        ("setIgnored", "set_ignored_node"),
        ("removeIgnored", "remove_ignored_node"),
    ],
)
def test_favorite_and_ignored_admin_messages(
    node_id: str | int,
    method_name: str,
    field_name: str,
    autospec_local_node_iface: Callable[[type[Any]], MagicMock],
) -> None:
    """Favorite/ignored helpers should resolve IDs and populate the matching admin field."""
    iface = autospec_local_node_iface(SerialInterface)
    node = Node(iface, 12345678)
    admin_message = admin_pb2.AdminMessage()
    with patch("meshtastic.node.admin_pb2.AdminMessage", return_value=admin_message):
        getattr(node, method_name)(node_id)

    assert getattr(admin_message, field_name) == 502009325
    iface.sendData.assert_called_once()


@pytest.mark.unit
@pytest.mark.parametrize(
    ("param_name", "value", "expected_error"),
    [
        (
            "long_name",
            "   ",
            "Long Name cannot be empty or contain only whitespace characters",
        ),
        (
            "long_name",
            "",
            "Long Name cannot be empty or contain only whitespace characters",
        ),
        (
            "short_name",
            "   ",
            "Short Name cannot be empty or contain only whitespace characters",
        ),
        (
            "short_name",
            "",
            "Short Name cannot be empty or contain only whitespace characters",
        ),
    ],
)
def test_setOwner_rejects_empty_or_whitespace_names(
    autospec_local_node_iface: Callable[[type[Any]], MagicMock],
    param_name: str,
    value: str,
    expected_error: str,
) -> None:
    """Test setOwner rejects empty or whitespace-only names."""
    iface = autospec_local_node_iface(MeshInterface)
    anode = Node(iface, 123, noProto=True)

    with pytest.raises(MeshInterface.MeshInterfaceError, match=expected_error):
        anode.setOwner(**{param_name: value})  # type: ignore[arg-type]


@pytest.mark.unit
@pytest.mark.parametrize(
    ("owner_kwargs", "expected_patterns", "unexpected_patterns", "expected_short_name"),
    [
        pytest.param(
            {"long_name": "ValidName", "short_name": "VN"},
            (
                r"p\.set_owner\.long_name_set:True",
                r"p\.set_owner\.short_name_set:True",
            ),
            (
                r"p\.set_owner\.is_licensed:True",
                r"p\.set_owner\.is_unmessagable:True",
            ),
            "VN",
            id="long-and-short",
        ),
        pytest.param(
            {"short_name": "TST"},
            (r"p\.set_owner\.short_name_set:True",),
            (
                r"p\.set_owner\.long_name_set:True",
                r"p\.set_owner\.is_licensed:True",
                r"p\.set_owner\.is_unmessagable:True",
            ),
            "TST",
            id="short-only",
        ),
        pytest.param(
            {"long_name": "TestUser", "short_name": "TOOLONG"},
            (
                r"p\.set_owner\.long_name_set:True",
                r"p\.set_owner\.short_name_set:True",
            ),
            (
                r"p\.set_owner\.is_licensed:True",
                r"p\.set_owner\.is_unmessagable:True",
            ),
            "TOOL",
            id="short-name-truncated",
        ),
        pytest.param(
            {"long_name": "LicensedUser", "is_licensed": True},
            (
                r"p\.set_owner\.long_name_set:True",
                r"p\.set_owner\.is_licensed:True",
            ),
            (
                r"p\.set_owner\.short_name_set:True",
                r"p\.set_owner\.is_unmessagable:True",
            ),
            "",
            id="licensed",
        ),
        pytest.param(
            {"long_name": "TestUser", "is_unmessagable": True},
            (
                r"p\.set_owner\.long_name_set:True",
                r"p\.set_owner\.is_unmessagable:True",
            ),
            (
                r"p\.set_owner\.short_name_set:True",
                r"p\.set_owner\.is_licensed:True",
            ),
            "",
            id="unmessagable",
        ),
    ],
)
def test_setOwner_logs_expected_fields_for_variants(
    caplog: LogCaptureFixture,
    autospec_local_node_iface: Callable[[type[Any]], MagicMock],
    owner_kwargs: dict[str, Any],
    expected_patterns: tuple[str, ...],
    unexpected_patterns: tuple[str, ...],
    expected_short_name: str,
) -> None:
    """Test setOwner variants set only the requested fields and truncate short names."""
    iface = autospec_local_node_iface(MeshInterface)
    anode = Node(iface, 123, noProto=True)
    anode._send_admin = MagicMock(return_value=mesh_pb2.MeshPacket())  # type: ignore[method-assign]

    with caplog.at_level(logging.DEBUG):
        anode.setOwner(**owner_kwargs)

    for pattern in expected_patterns:
        assert re.search(pattern, caplog.text, re.MULTILINE)
    for pattern in unexpected_patterns:
        assert not re.search(pattern, caplog.text, re.MULTILINE)
    sent_msg = anode._send_admin.call_args.args[0]
    assert sent_msg.set_owner.short_name == expected_short_name


@pytest.mark.unit
def test_waitForConfig_timeout(
    autospec_local_node_iface: Callable[[type[Any]], MagicMock],
) -> None:
    """Test waitForConfig returns False on timeout."""
    iface = autospec_local_node_iface(MeshInterface)
    anode = Node(iface, 123, noProto=True)
    # Mock timeout to simulate immediate timeout (waitForSet returns False)
    anode._timeout = MagicMock()
    anode._timeout.waitForSet.return_value = False

    result = anode.waitForConfig()

    assert result is False
    wait_call = anode._timeout.waitForSet.call_args
    assert wait_call.kwargs["attrs"] == ("is_set",)
    assert getattr(wait_call.args[0], "_node", None) is anode


@pytest.mark.unit
def test_waitForConfig_success(
    autospec_local_node_iface: Callable[[type[Any]], MagicMock],
) -> None:
    """Test waitForConfig returns True when config is available."""
    iface = autospec_local_node_iface(MeshInterface)
    anode = Node(iface, 123, noProto=True)

    # Set up the config to be "available"
    anode.localConfig = localonly_pb2.LocalConfig()

    # Mock the timeout to return True
    anode._timeout = MagicMock()
    anode._timeout.waitForSet.return_value = True

    result = anode.waitForConfig(attribute="lora")

    assert result is True
    wait_call = anode._timeout.waitForSet.call_args
    assert wait_call.kwargs["attrs"] == ("is_set",)
    assert getattr(wait_call.args[0], "_name", None) == "lora"


@pytest.mark.unit
def test_start_ota_local_node() -> None:
    """Test startOTA canonical signature on local node."""
    iface = create_autospec(MeshInterface, instance=True)
    anode = Node(iface, 1234567890, noProto=True)
    iface.localNode = anode

    captured: dict[str, object] = {}
    anode._send_admin = _make_fake_send_admin(  # type: ignore[method-assign,assignment]
        captured=captured
    )

    test_hash = b"\x01\x02\x03" * 8  # 24-byte hash
    anode.startOTA(mode=admin_pb2.OTAMode.OTA_WIFI, ota_file_hash=test_hash)

    sent_msg = cast(admin_pb2.AdminMessage, captured["msg"])
    assert sent_msg.ota_request.reboot_ota_mode == admin_pb2.OTAMode.OTA_WIFI
    assert sent_msg.ota_request.ota_hash == test_hash


@pytest.mark.unit
def test_start_ota_local_node_legacy_alias_keywords() -> None:
    """Test startOTA legacy aliases ota_mode/ota_hash remain supported."""
    iface = create_autospec(MeshInterface, instance=True)
    anode = Node(iface, 1234567890, noProto=True)
    iface.localNode = anode

    captured: dict[str, object] = {}
    anode._send_admin = _make_fake_send_admin(  # type: ignore[method-assign,assignment]
        captured=captured
    )

    test_hash = b"\x11\x22\x33" * 8
    with warnings.catch_warnings():
        warnings.simplefilter("error", DeprecationWarning)
        anode.startOTA(ota_mode=admin_pb2.OTAMode.OTA_WIFI, ota_hash=test_hash)

    sent_msg = cast(admin_pb2.AdminMessage, captured["msg"])
    assert sent_msg.ota_request.reboot_ota_mode == admin_pb2.OTAMode.OTA_WIFI
    assert sent_msg.ota_request.ota_hash == test_hash


@pytest.mark.unit
def test_start_ota_remote_node_raises_error() -> None:
    """Test startOTA on remote node raises MeshInterfaceError."""
    iface = create_autospec(MeshInterface, instance=True)
    local_node = Node(iface, 1234567890, noProto=True)
    remote_node = Node(iface, 9876543210, noProto=True)
    iface.localNode = local_node

    test_hash = b"\x01\x02\x03" * 8
    with pytest.raises(
        MeshInterface.MeshInterfaceError, match="startOTA only possible on local node"
    ):
        remote_node.startOTA(mode=admin_pb2.OTAMode.OTA_WIFI, ota_file_hash=test_hash)


@pytest.mark.unit
def test_requestConfig_with_module_config_descriptor(
    mock_serial_interface: MagicMock,
) -> None:
    """Verify requestConfig sets get_module_config_request for LocalModuleConfig fields.

    When configType belongs to LocalModuleConfig rather than LocalConfig,
    requestConfig should set get_module_config_request to the field index.
    """
    anode = Node(mock_serial_interface, "!12345678", noProto=True)
    mock_serial_interface.localNode = anode

    # Get a field descriptor from LocalModuleConfig (not LocalConfig)
    module_config = localonly_pb2.LocalModuleConfig()
    mqtt_field = module_config.DESCRIPTOR.fields_by_name["mqtt"]

    sent_messages: list[admin_pb2.AdminMessage] = []
    anode._send_admin = _make_fake_send_admin(  # type: ignore[method-assign,assignment]
        sent_messages=sent_messages
    )

    anode.requestConfig(mqtt_field)

    assert len(sent_messages) == 1
    sent_msg = sent_messages[0]
    # mqtt field has index 0, should be set as get_module_config_request
    assert sent_msg.WhichOneof("payload_variant") == "get_module_config_request"
    assert sent_msg.get_module_config_request == 0


@pytest.mark.unit
def test_showChannels_logs_snapshot_and_skips_disabled_entries(
    caplog: LogCaptureFixture,
    capsys: CaptureFixture[str],
    autospec_local_node_iface: Callable[[type[Any]], MagicMock],
) -> None:
    """ShowChannels should log channel snapshot and print only non-disabled channels."""
    anode = Node(autospec_local_node_iface(MeshInterface), "!12345678", noProto=True)
    primary = Channel(index=0, role=Channel.Role.PRIMARY)
    primary.settings.name = "primary"
    primary.settings.psk = b"\x01"
    disabled = Channel(index=1, role=Channel.Role.DISABLED)
    disabled.settings.name = "disabled"
    anode.channels = [primary, disabled]
    anode.localConfig.lora.hop_limit = 3

    with caplog.at_level(logging.DEBUG):
        anode.showChannels()

    out, _ = capsys.readouterr()
    assert "channel snapshot captured" in caplog.text
    assert "Index 0: PRIMARY" in out
    assert "Index 1:" not in out


@pytest.mark.unit
def test_turnOffEncryptionOnPrimaryChannel_requires_loaded_channels(
    autospec_local_node_iface: Callable[[type[Any]], MagicMock],
) -> None:
    """TurnOffEncryptionOnPrimaryChannel should fail when channels are missing."""
    anode = Node(autospec_local_node_iface(MeshInterface), "!12345678", noProto=True)
    anode.channels = []

    with pytest.raises(
        MeshInterface.MeshInterfaceError,
        match="Error: No channels have been read",
    ):
        anode.turnOffEncryptionOnPrimaryChannel()


@pytest.mark.unit
def test_turnOffEncryptionOnPrimaryChannel_updates_primary_and_writes(
    autospec_local_node_iface: Callable[[type[Any]], MagicMock],
) -> None:
    """TurnOffEncryptionOnPrimaryChannel should disable PSK and write channel 0."""
    anode = Node(autospec_local_node_iface(MeshInterface), "!12345678", noProto=True)
    primary = Channel(index=0, role=Channel.Role.PRIMARY)
    primary.settings.psk = b"\x01"
    anode.channels = [primary]
    anode._write_channel_snapshot = MagicMock()  # type: ignore[method-assign]

    anode.turnOffEncryptionOnPrimaryChannel()

    assert anode.channels[0].settings.psk == fromPSK("none")
    anode._write_channel_snapshot.assert_called_once()
    written_channel = anode._write_channel_snapshot.call_args.args[0]
    assert written_channel.index == 0
    assert written_channel.settings.psk == fromPSK("none")


@pytest.mark.unit
def test_writeChannel_out_of_range_raises_mesh_error(
    autospec_local_node_iface: Callable[[type[Any]], MagicMock],
) -> None:
    """WriteChannel should reject invalid channel indices."""
    anode = Node(autospec_local_node_iface(MeshInterface), "!12345678", noProto=True)
    anode.channels = [Channel(index=0, role=Channel.Role.PRIMARY)]

    with pytest.raises(
        MeshInterface.MeshInterfaceError,
        match=r"Channel index 1 out of range \(0-0\)",
    ):
        anode.writeChannel(1)


@pytest.mark.unit
def test_deleteChannel_rejects_non_secondary_or_disabled(
    autospec_local_node_iface: Callable[[type[Any]], MagicMock],
) -> None:
    """DeleteChannel should only allow SECONDARY or DISABLED channels."""
    anode = Node(autospec_local_node_iface(MeshInterface), "!12345678", noProto=True)
    anode.channels = [Channel(index=0, role=Channel.Role.PRIMARY)]

    with pytest.raises(
        MeshInterface.MeshInterfaceError,
        match="Only SECONDARY or DISABLED channels can be deleted",
    ):
        anode.deleteChannel(0)


@pytest.mark.unit
def test_deleteChannel_rewrites_following_channels_and_updates_admin_index(
    autospec_local_node_iface: Callable[[type[Any]], MagicMock],
) -> None:
    """DeleteChannel should start on pre-delete admin index and switch after that slot is rewritten."""
    iface = autospec_local_node_iface(MeshInterface)
    anode = Node(iface, "!12345678", noProto=True)
    iface.localNode = anode

    primary = Channel(index=0, role=Channel.Role.PRIMARY)
    primary.settings.name = "primary"
    admin_secondary = Channel(index=1, role=Channel.Role.SECONDARY)
    admin_secondary.settings.name = "admin"
    disabled = Channel(index=2, role=Channel.Role.DISABLED)
    anode.channels = [primary, admin_secondary, disabled]
    anode.ensureSessionKey = MagicMock()  # type: ignore[method-assign]
    anode._send_admin = MagicMock()  # type: ignore[method-assign]

    anode.deleteChannel(1)

    assert anode.channels is not None
    assert len(anode.channels) == CHANNEL_LIMIT
    written_indexes = [
        call.args[0].set_channel.index for call in anode._send_admin.call_args_list
    ]
    # Slot 2 was present in the incomplete cache and is already the same
    # normalized DISABLED protobuf. Unknown slots 3..7 remain conservative.
    assert written_indexes == [1, *range(3, CHANNEL_LIMIT)]
    admin_indexes = [
        _get_mock_call_arg(call, name="adminIndex", positional_index=3)
        for call in anode._send_admin.call_args_list
    ]
    assert admin_indexes[0] == 1
    assert all(index == 0 for index in admin_indexes[1:])


@pytest.mark.unit
def test_deleteChannel_switches_admin_index_after_rewriting_former_admin_slot(
    autospec_local_node_iface: Callable[[type[Any]], MagicMock],
) -> None:
    """DeleteChannel should keep using the old admin index until the old admin slot is rewritten."""
    iface = autospec_local_node_iface(MeshInterface)
    anode = Node(iface, "!12345678", noProto=True)
    iface.localNode = anode

    primary = Channel(index=0, role=Channel.Role.PRIMARY)
    primary.settings.name = "primary"
    removable = Channel(index=1, role=Channel.Role.SECONDARY)
    removable.settings.name = "remove-me"
    admin_secondary = Channel(index=2, role=Channel.Role.SECONDARY)
    admin_secondary.settings.name = "admin"
    disabled = Channel(index=3, role=Channel.Role.DISABLED)
    anode.channels = [primary, removable, admin_secondary, disabled]
    anode.ensureSessionKey = MagicMock()  # type: ignore[method-assign]
    anode._send_admin = MagicMock()  # type: ignore[method-assign]

    anode.deleteChannel(1)

    admin_indexes = [
        _get_mock_call_arg(call, name="adminIndex", positional_index=3)
        for call in anode._send_admin.call_args_list
    ]
    # Keep old admin index (2) through rewrite of old slot 2, then switch to 1.
    assert len(admin_indexes) > 2
    assert admin_indexes[:2] == [2, 2]
    assert all(index == 1 for index in admin_indexes[2:])


def _build_full_channel_table(
    highest_secondary_index: int,
) -> list[Channel]:
    """Build a complete channel table with a contiguous active prefix."""
    channels: list[Channel] = []
    for index in range(CHANNEL_LIMIT):
        channel = Channel(index=index)
        if index == 0:
            channel.role = Channel.Role.PRIMARY
            channel.settings.name = "primary"
        elif index <= highest_secondary_index:
            channel.role = Channel.Role.SECONDARY
            channel.settings.name = f"channel-{index}"
        else:
            channel.role = Channel.Role.DISABLED
        channels.append(channel)
    return channels


@pytest.mark.unit
@pytest.mark.parametrize(
    ("highest_secondary_index", "delete_index", "expected_writes"),
    (
        pytest.param(1, 1, [1], id="delete-only-secondary"),
        pytest.param(2, 1, [1, 2], id="shift-one-secondary"),
        pytest.param(3, 1, [1, 2, 3], id="shift-two-secondaries"),
        pytest.param(3, 2, [2, 3], id="delete-middle-secondary"),
    ),
)
def test_deleteChannel_writes_only_changed_complete_cache_slots(
    autospec_local_node_iface: Callable[[type[Any]], MagicMock],
    highest_secondary_index: int,
    delete_index: int,
    expected_writes: list[int],
) -> None:
    """A complete cache should not rewrite identical trailing disabled slots."""
    iface = autospec_local_node_iface(MeshInterface)
    anode = Node(iface, "!12345678", noProto=True)
    iface.localNode = anode
    anode.channels = _build_full_channel_table(highest_secondary_index)
    anode.ensureSessionKey = MagicMock()  # type: ignore[method-assign]
    anode._send_admin = MagicMock()  # type: ignore[method-assign]

    anode.deleteChannel(delete_index)

    written_indexes = [
        call.args[0].set_channel.index for call in anode._send_admin.call_args_list
    ]
    assert written_indexes == expected_writes
    assert all(
        _get_mock_call_arg(call, name="adminIndex", positional_index=3) == 0
        for call in anode._send_admin.call_args_list
    )
    assert anode.channels is not None
    assert [channel.index for channel in anode.channels] == list(range(CHANNEL_LIMIT))


@pytest.mark.unit
def test_deleteChannel_identical_trailing_disabled_slot_requires_no_write(
    autospec_local_node_iface: Callable[[type[Any]], MagicMock],
) -> None:
    """Deleting the final empty disabled slot should be an on-device no-op."""
    iface = autospec_local_node_iface(MeshInterface)
    anode = Node(iface, "!12345678", noProto=True)
    iface.localNode = anode
    anode.channels = _build_full_channel_table(1)
    anode.ensureSessionKey = MagicMock()  # type: ignore[method-assign]
    anode._send_admin = MagicMock()  # type: ignore[method-assign]

    anode.deleteChannel(CHANNEL_LIMIT - 1)

    anode.ensureSessionKey.assert_not_called()
    anode._send_admin.assert_not_called()
    assert anode.channels is not None
    assert len(anode.channels) == CHANNEL_LIMIT
    assert anode.channels[-1].role == Channel.Role.DISABLED


@pytest.mark.unit
def test_deleteChannel_compares_complete_payload_not_only_channel_role(
    autospec_local_node_iface: Callable[[type[Any]], MagicMock],
) -> None:
    """Settings changes in disabled slots must still be rewritten after a shift."""
    iface = autospec_local_node_iface(MeshInterface)
    anode = Node(iface, "!12345678", noProto=True)
    iface.localNode = anode
    anode.channels = _build_full_channel_table(1)
    anode.channels[2].settings.name = "stale-disabled-payload"
    anode.ensureSessionKey = MagicMock()  # type: ignore[method-assign]
    anode._send_admin = MagicMock()  # type: ignore[method-assign]

    anode.deleteChannel(1)

    written_indexes = [
        call.args[0].set_channel.index for call in anode._send_admin.call_args_list
    ]
    assert written_indexes == [1, 2]


@pytest.mark.unit
def test_channel_lookup_helpers_find_live_channels(
    autospec_local_node_iface: Callable[[type[Any]], MagicMock],
) -> None:
    """Live channel lookups should return the matching cached objects."""
    anode = Node(autospec_local_node_iface(MeshInterface), "!12345678", noProto=True)
    primary = Channel(index=0, role=Channel.Role.PRIMARY)
    primary.settings.name = "main"
    admin_channel = Channel(index=1, role=Channel.Role.SECONDARY)
    admin_channel.settings.name = "AdMiN"
    disabled = Channel(index=2, role=Channel.Role.DISABLED)
    anode.channels = [primary, admin_channel, disabled]

    assert anode.getChannelByName("main") is primary
    assert anode.getDisabledChannel() is disabled


@pytest.mark.unit
def test_channel_copy_helpers_return_isolated_snapshots(
    autospec_local_node_iface: Callable[[type[Any]], MagicMock],
) -> None:
    """Copy lookups should not expose mutable cached Channel instances."""
    anode = Node(autospec_local_node_iface(MeshInterface), "!12345678", noProto=True)
    primary = Channel(index=0, role=Channel.Role.PRIMARY)
    primary.settings.name = "main"
    disabled = Channel(index=1, role=Channel.Role.DISABLED)
    anode.channels = [primary, disabled]

    named_copy = anode.getChannelCopyByName("main")
    disabled_copy = anode.getDisabledChannelCopy()
    assert named_copy is not None and named_copy is not primary
    assert disabled_copy is not None and disabled_copy is not disabled

    named_copy.role = Channel.Role.DISABLED
    disabled_copy.role = Channel.Role.PRIMARY
    assert primary.role == Channel.Role.PRIMARY
    assert disabled.role == Channel.Role.DISABLED


@pytest.mark.unit
def test_admin_channel_index_and_none_channel_paths(
    autospec_local_node_iface: Callable[[type[Any]], MagicMock],
) -> None:
    """Admin lookup should be case-insensitive and helpers should tolerate no cache."""
    anode = Node(autospec_local_node_iface(MeshInterface), "!12345678", noProto=True)
    primary = Channel(index=0, role=Channel.Role.PRIMARY)
    admin_channel = Channel(index=1, role=Channel.Role.SECONDARY)
    admin_channel.settings.name = "AdMiN"
    anode.channels = [primary, admin_channel]

    assert anode._get_admin_channel_index() == 1
    assert anode.getAdminChannelIndex() == 1

    anode.channels = None
    assert anode.getChannelByName("main") is None
    assert anode.getDisabledChannel() is None
    assert anode.getChannelCopyByName("main") is None
    assert anode.getDisabledChannelCopy() is None


@pytest.mark.unit
def test_get_named_admin_channel_index_ignores_disabled_admin_channels(
    autospec_local_node_iface: Callable[[type[Any]], MagicMock],
) -> None:
    """_get_named_admin_channel_index should skip channels that are DISABLED."""
    anode = Node(autospec_local_node_iface(MeshInterface), "!12345678", noProto=True)
    primary = Channel(index=0, role=Channel.Role.PRIMARY)
    primary.settings.name = "primary"
    disabled_admin = Channel(index=1, role=Channel.Role.DISABLED)
    disabled_admin.settings.name = "admin"
    secondary = Channel(index=2, role=Channel.Role.SECONDARY)
    secondary.settings.name = "secondary"
    anode.channels = [primary, disabled_admin, secondary]

    assert anode._get_named_admin_channel_index() is None
    assert anode._get_admin_channel_index() == 0


@pytest.mark.unit
def test_deleteChannel_missing_or_out_of_range_validations(
    autospec_local_node_iface: Callable[[type[Any]], MagicMock],
) -> None:
    """DeleteChannel should validate missing channels and invalid indices."""
    anode = Node(autospec_local_node_iface(MeshInterface), "!12345678", noProto=True)
    anode.channels = None
    with pytest.raises(
        MeshInterface.MeshInterfaceError, match="Error: No channels have been read"
    ):
        anode.deleteChannel(0)

    anode.channels = [Channel(index=0, role=Channel.Role.SECONDARY)]
    with pytest.raises(
        MeshInterface.MeshInterfaceError,
        match=r"Channel index 5 out of range \(0-0\)",
    ):
        anode.deleteChannel(5)


@pytest.mark.unit
def test_deleteChannel_rewrite_uses_snapshot_when_channels_change_after_lock_release(
    autospec_local_node_iface: Callable[[type[Any]], MagicMock],
) -> None:
    """DeleteChannel should complete rewrites from a captured snapshot even if channels mutate mid-rewrite."""
    iface = autospec_local_node_iface(MeshInterface)
    anode = Node(iface, "!12345678", noProto=True)
    iface.localNode = anode
    anode.channels = [Channel(index=0, role=Channel.Role.SECONDARY)]
    anode.ensureSessionKey = MagicMock()  # type: ignore[method-assign]
    dropped_channels = False

    def _drop_channels_on_first_send(
        _msg: admin_pb2.AdminMessage,
        wantResponse: bool = False,
        onResponse: Callable[[dict[str, Any]], Any] | None = None,
        adminIndex: int | None = None,
    ) -> mesh_pb2.MeshPacket | None:
        nonlocal dropped_channels
        if not dropped_channels:
            anode.channels = None
            dropped_channels = True
        _ = (wantResponse, onResponse, adminIndex)
        return mesh_pb2.MeshPacket()

    anode._send_admin = MagicMock(side_effect=_drop_channels_on_first_send)  # type: ignore[method-assign]

    anode.deleteChannel(0)

    # Mid-rewrite local cache mutation should not affect sends from the captured
    # channel snapshot list.
    assert anode.channels is None
    assert anode._send_admin.call_count == CHANNEL_LIMIT
    written_indexes = [
        call.args[0].set_channel.index for call in anode._send_admin.call_args_list
    ]
    assert written_indexes == list(range(CHANNEL_LIMIT))


@pytest.mark.unit
def test_channel_lookup_helpers_return_none_when_no_match(
    autospec_local_node_iface: Callable[[type[Any]], MagicMock],
) -> None:
    """Lookup helpers should return no result when entries are absent."""
    anode = Node(autospec_local_node_iface(MeshInterface), "!12345678", noProto=True)
    anode.channels = [Channel(index=0, role=Channel.Role.SECONDARY)]

    assert anode.getChannelByName("missing") is None
    assert anode.getDisabledChannel() is None
    assert anode._get_admin_channel_index() == 0

@pytest.mark.unit
def test_fixup_channels_truncates_and_reindexes_to_limit(
    autospec_local_node_iface: Callable[[type[Any]], MagicMock],
) -> None:
    """_fixup_channels should truncate over-limit input and maintain contiguous indices."""
    anode = Node(autospec_local_node_iface(MeshInterface), "!12345678", noProto=True)
    anode.channels = [
        Channel(index=i, role=Channel.Role.SECONDARY) for i in range(CHANNEL_LIMIT + 2)
    ]

    anode._fixup_channels()

    assert anode.channels is not None
    assert len(anode.channels) == CHANNEL_LIMIT
    assert [ch.index for ch in anode.channels] == list(range(CHANNEL_LIMIT))

@pytest.mark.unit
def test_fill_channels_handles_none_and_pads_to_limit(
    autospec_local_node_iface: Callable[[type[Any]], MagicMock],
) -> None:
    """_fill_channels should no-op for None and pad existing channel lists to max size."""
    anode = Node(autospec_local_node_iface(MeshInterface), "!12345678", noProto=True)
    anode.channels = None
    anode._fill_channels()
    assert anode.channels is None

    anode.channels = [Channel(index=0, role=Channel.Role.PRIMARY)]
    anode._fill_channels()
    assert anode.channels is not None
    assert len(anode.channels) == CHANNEL_LIMIT
    assert anode.channels[-1].role == Channel.Role.DISABLED

@pytest.mark.unit
def test_onResponseRequestChannel_routing_paths(
    autospec_local_node_iface: Callable[[type[Any]], MagicMock],
) -> None:
    """Routing failures should terminate the request; success should await ADMIN_APP."""
    anode = Node(autospec_local_node_iface(MeshInterface), "!12345678", noProto=True)
    anode._request_channel = MagicMock()  # type: ignore[method-assign]

    anode.onResponseRequestChannel(
        {
            "decoded": {
                "portnum": "ROUTING_APP",
                "routing": {"errorReason": "NO_ROUTE"},
            }
        }
    )
    assert anode._channel_response_runtime.has_channel_request_failed() is True
    anode._request_channel.assert_not_called()

    channel = Channel(index=3, role=Channel.Role.SECONDARY)
    anode.partialChannels = [channel]
    anode._channel_response_runtime.mark_channel_request_sent(3)
    anode.onResponseRequestChannel(
        {"decoded": {"portnum": "ROUTING_APP", "routing": {"errorReason": "NONE"}}}
    )

    assert anode._channel_response_runtime.has_channel_request_failed() is False
    assert anode.partialChannels == [channel]
    anode._request_channel.assert_not_called()

@pytest.mark.unit
def test_onResponseRequestChannel_handles_partial_and_final_channel(
    autospec_local_node_iface: Callable[[type[Any]], MagicMock],
) -> None:
    """OnResponseRequestChannel should request next channel until the final channel arrives."""
    anode = Node(autospec_local_node_iface(MeshInterface), "!12345678", noProto=True)
    anode._request_channel = MagicMock()  # type: ignore[method-assign]

    partial = Channel(index=2, role=Channel.Role.SECONDARY)
    anode._channel_response_runtime.mark_channel_request_sent(2)
    anode.onResponseRequestChannel(
        {
            "decoded": {
                "portnum": "ADMIN_APP",
                "admin": {"raw": MagicMock(get_channel_response=partial)},
            }
        }
    )
    anode._request_channel.assert_called_once_with(3)

    final = Channel(index=CHANNEL_LIMIT - 1, role=Channel.Role.SECONDARY)
    anode._request_channel.reset_mock()
    anode._channel_response_runtime.mark_channel_request_sent(CHANNEL_LIMIT - 1)
    anode.onResponseRequestChannel(
        {
            "decoded": {
                "portnum": "ADMIN_APP",
                "admin": {"raw": MagicMock(get_channel_response=final)},
            }
        }
    )
    anode._request_channel.assert_not_called()
    assert anode.channels is not None
    assert len(anode.channels) == CHANNEL_LIMIT

@pytest.mark.unit
def test_get_channels_with_hash_handles_missing_fields(
    autospec_local_node_iface: Callable[[type[Any]], MagicMock],
) -> None:
    """_get_channels_with_hash should emit hashes only when both name and PSK are present."""
    anode = Node(autospec_local_node_iface(MeshInterface), "!12345678", noProto=True)
    with_hash = Channel(index=0, role=Channel.Role.PRIMARY)
    with_hash.settings.name = "hash-me"
    with_hash.settings.psk = b"\x01\x02"
    without_hash = Channel(index=1, role=Channel.Role.SECONDARY)
    anode.channels = [with_hash, without_hash]

    entries = anode._get_channels_with_hash()

    assert len(entries) == 2
    assert entries[0]["hash"] is not None
    assert entries[1]["hash"] is None

@pytest.mark.unit
def test_fixup_channels_returns_immediately_when_channels_none(
    autospec_local_node_iface: Callable[[type[Any]], MagicMock],
) -> None:
    """_fixup_channels should no-op when channels are unset."""
    anode = Node(autospec_local_node_iface(MeshInterface), "!12345678", noProto=True)
    anode.channels = None

    anode._fixup_channels()

    assert anode.channels is None

@pytest.mark.unit
def test_getURL_requests_lora_when_local_config_empty(
    autospec_local_node_iface: Callable[[type[Any]], MagicMock],
) -> None:
    """GetURL should request lora config when localConfig has no populated fields."""
    anode = Node(autospec_local_node_iface(MeshInterface), "!12345678", noProto=True)
    primary = Channel(index=0, role=Channel.Role.PRIMARY)
    primary.settings.name = "primary"
    primary.settings.psk = b"\x01"
    secondary = Channel(index=1, role=Channel.Role.SECONDARY)
    secondary.settings.name = "secondary"
    secondary.settings.psk = b"\x02"
    disabled = Channel(index=2, role=Channel.Role.DISABLED)
    anode.channels = [primary, secondary, disabled]
    anode.requestConfig = MagicMock()  # type: ignore[method-assign]

    def _populate_lora_and_return_true(*, attribute: str = "channels") -> bool:
        _ = attribute
        anode.localConfig.lora.hop_limit = 3
        return True

    anode.waitForConfig = MagicMock(  # type: ignore[method-assign]
        side_effect=_populate_lora_and_return_true
    )

    url = anode.getURL(includeAll=False)

    anode.requestConfig.assert_called_once_with(
        anode.localConfig.DESCRIPTOR.fields_by_name["lora"]
    )
    channel_set = _decode_channel_set_from_url(url)
    assert len(channel_set.settings) == 1
    assert channel_set.settings[0].name == "primary"
