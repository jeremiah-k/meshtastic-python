"""Regression tests for channel-setting CLI validation."""

import sys
from unittest.mock import MagicMock, patch

import pytest

from meshtastic.__main__ import main
from meshtastic.node import Node
from meshtastic.protobuf import channel_pb2, localonly_pb2
from meshtastic.tcp_interface import TCPInterface


def _interface_with_channels() -> tuple[MagicMock, MagicMock]:
    interface = MagicMock(autospec=TCPInterface)
    interface.__enter__ = MagicMock(return_value=interface)
    interface.__exit__ = MagicMock(return_value=None)
    node = MagicMock(autospec=Node)
    node.localConfig = localonly_pb2.LocalConfig()
    node.moduleConfig = localonly_pb2.LocalModuleConfig()
    node.channels = [channel_pb2.Channel(index=0), channel_pb2.Channel(index=1)]
    interface.getNode.return_value = node
    return interface, node


def _run_channel_set(
    monkeypatch: pytest.MonkeyPatch,
    interface: MagicMock,
    *setting: str,
) -> None:
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "meshtastic",
            "--host",
            "meshtastic.local",
            "--ch-index",
            "1",
            "--ch-set",
            *setting,
        ],
    )
    with patch("meshtastic.tcp_interface.TCPInterface", return_value=interface):
        main()


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_invalid_non_psk_channel_value_exits_without_writing(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    interface, node = _interface_with_channels()

    with pytest.raises(SystemExit) as exc_info:
        _run_channel_set(monkeypatch, interface, "uplink_enabled", "not-a-boolean")

    assert exc_info.value.code == 1
    node.writeChannel.assert_not_called()
    out, err = capsys.readouterr()
    assert "expected boolean" in err
    assert "does not have an attribute uplink_enabled" not in out
    assert "Traceback" not in out + err


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_unknown_channel_field_reports_choices_without_writing(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Unknown fields preserve the historical nonfatal choices-only contract."""
    interface, node = _interface_with_channels()
    original_channel = channel_pb2.Channel()
    original_channel.CopyFrom(node.channels[1])

    _run_channel_set(monkeypatch, interface, "not_a_channel_field", "1")

    node.writeChannel.assert_not_called()
    assert node.channels[1] == original_channel
    out, err = capsys.readouterr()
    assert "does not have an attribute not_a_channel_field" in out
    assert "Choices are..." in out
    assert "Writing modified channels to device" not in out
    assert err == ""


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_valid_non_psk_channel_value_still_writes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    interface, node = _interface_with_channels()

    _run_channel_set(monkeypatch, interface, "uplink_enabled", "true")

    assert node.channels[1].settings.uplink_enabled is True
    node.writeChannel.assert_called_once_with(1)


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_channel_set_batch_does_not_partially_mutate_on_later_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A rejected later entry must leave earlier channel values untouched."""
    interface, node = _interface_with_channels()
    original = node.channels[1].settings.uplink_enabled
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "meshtastic",
            "--host",
            "meshtastic.local",
            "--ch-index",
            "1",
            "--ch-set",
            "uplink_enabled",
            "true",
            "--ch-set",
            "downlink_enabled",
            "not-a-boolean",
        ],
    )

    with patch("meshtastic.tcp_interface.TCPInterface", return_value=interface):
        with pytest.raises(SystemExit):
            main()

    assert node.channels[1].settings.uplink_enabled is original
    node.writeChannel.assert_not_called()


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_channel_set_batch_does_not_mutate_on_later_unknown_field(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A later unknown field cancels an otherwise valid channel update atomically."""
    interface, node = _interface_with_channels()
    original_channel = channel_pb2.Channel()
    original_channel.CopyFrom(node.channels[1])
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "meshtastic",
            "--host",
            "meshtastic.local",
            "--ch-index",
            "1",
            "--ch-set",
            "uplink_enabled",
            "true",
            "--ch-set",
            "not_a_channel_field",
            "1",
        ],
    )

    with patch("meshtastic.tcp_interface.TCPInterface", return_value=interface):
        main()

    node.writeChannel.assert_not_called()
    assert node.channels[1] == original_channel
    out, err = capsys.readouterr()
    assert "does not have an attribute not_a_channel_field" in out
    assert "Choices are..." in out
    assert "Writing modified channels to device" not in out
    assert err == ""


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_unknown_channel_field_does_not_hide_later_invalid_value(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Validation continues after an unknown field while the whole batch stays atomic."""
    interface, node = _interface_with_channels()
    original_channel = channel_pb2.Channel()
    original_channel.CopyFrom(node.channels[1])
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "meshtastic",
            "--host",
            "meshtastic.local",
            "--ch-index",
            "1",
            "--ch-set",
            "not_a_channel_field",
            "1",
            "--ch-set",
            "uplink_enabled",
            "not-a-boolean",
        ],
    )

    with patch("meshtastic.tcp_interface.TCPInterface", return_value=interface):
        with pytest.raises(SystemExit) as exc_info:
            main()

    assert exc_info.value.code == 1
    node.writeChannel.assert_not_called()
    assert node.channels[1] == original_channel
    out, err = capsys.readouterr()
    assert "does not have an attribute not_a_channel_field" in out
    assert "expected boolean" in err
    assert "Traceback" not in out + err
