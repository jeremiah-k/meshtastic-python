"""Focused CLI validation tests for channel and node-list options."""

import sys
from unittest.mock import MagicMock, patch

import pytest

from meshtastic.__main__ import main
from meshtastic.tcp_interface import TCPInterface


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_ch_add_with_index_explains_how_to_target_channels(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "meshtastic",
            "--host",
            "meshtastic.local",
            "--ch-add",
            "test",
            "--ch-index",
            "1",
        ],
    )
    interface = MagicMock(autospec=TCPInterface)
    interface.__enter__ = MagicMock(return_value=interface)
    interface.__exit__ = MagicMock(return_value=None)

    with patch("meshtastic.tcp_interface.TCPInterface", return_value=interface):
        with pytest.raises(SystemExit) as exc_info:
            main()

    assert exc_info.value.code == 1
    _out, err = capsys.readouterr()
    assert "--ch-add chooses the next free channel index automatically" in err
    assert "remove --ch-index and retry" in err


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_nodes_show_fields_rejects_unknown_field_with_choices(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "meshtastic",
            "--host",
            "meshtastic.local",
            "--nodes",
            "--show-fields",
            "user.id,channel_aeros index",
        ],
    )
    interface = MagicMock(autospec=TCPInterface)
    interface.__enter__ = MagicMock(return_value=interface)
    interface.__exit__ = MagicMock(return_value=None)
    interface.nodesByNum = {
        1: {"num": 1, "user": {"id": "!00000001", "longName": "Node"}, "channel": 0}
    }

    with patch("meshtastic.tcp_interface.TCPInterface", return_value=interface):
        with pytest.raises(SystemExit) as exc_info:
            main()

    assert exc_info.value.code == 1
    interface.showNodes.assert_not_called()
    _out, err = capsys.readouterr()
    assert "Unknown --show-fields value(s): channel_aeros index" in err
    assert "user.id" in err
    assert "channel" in err


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_nodes_show_fields_accepts_schema_fields_absent_from_node_database(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "meshtastic",
            "--host",
            "meshtastic.local",
            "--nodes",
            "--show-fields",
            "user.id,environmentMetrics.temperature",
        ],
    )
    interface = MagicMock(autospec=TCPInterface)
    interface.__enter__ = MagicMock(return_value=interface)
    interface.__exit__ = MagicMock(return_value=None)
    interface.nodesByNum = {1: {"num": 1, "user": {"id": "!00000001"}, "channel": 0}}

    with patch("meshtastic.tcp_interface.TCPInterface", return_value=interface):
        main()

    interface.showNodes.assert_called_once_with(
        True, ["user.id", "environmentMetrics.temperature"]
    )

@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
@pytest.mark.parametrize("nodes_by_num", [None, {}])
def test_nodes_show_fields_rejects_unknown_field_without_node_database(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    nodes_by_num: object,
) -> None:
    """Schema validation must still run before any nodes have been synchronized."""
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "meshtastic",
            "--host",
            "meshtastic.local",
            "--nodes",
            "--show-fields",
            "definitely.notAField",
        ],
    )
    interface = MagicMock(autospec=TCPInterface)
    interface.__enter__ = MagicMock(return_value=interface)
    interface.__exit__ = MagicMock(return_value=None)
    interface.nodesByNum = nodes_by_num

    with patch("meshtastic.tcp_interface.TCPInterface", return_value=interface):
        with pytest.raises(SystemExit) as exc_info:
            main()

    assert exc_info.value.code == 1
    interface.showNodes.assert_not_called()
    _out, err = capsys.readouterr()
    assert "Unknown --show-fields value(s): definitely.notAField" in err
    assert "Available fields:\n" in err
    assert "user.id" in err


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_nodes_show_fields_accepts_schema_field_without_node_database(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Known schema fields should remain usable before NodeDB population."""
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "meshtastic",
            "--host",
            "meshtastic.local",
            "--nodes",
            "--show-fields",
            "environmentMetrics.temperature",
        ],
    )
    interface = MagicMock(autospec=TCPInterface)
    interface.__enter__ = MagicMock(return_value=interface)
    interface.__exit__ = MagicMock(return_value=None)
    interface.nodesByNum = {}

    with patch("meshtastic.tcp_interface.TCPInterface", return_value=interface):
        main()

    interface.showNodes.assert_called_once_with(
        True, ["environmentMetrics.temperature"]
    )
