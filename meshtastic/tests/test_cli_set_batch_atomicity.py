"""Regression tests for atomic validation of multi-entry --set commands."""

import sys
from unittest.mock import MagicMock, patch

import pytest

from meshtastic.__main__ import main
from meshtastic.node import Node
from meshtastic.protobuf import localonly_pb2
from meshtastic.tcp_interface import TCPInterface


def _interface_with_config_node() -> tuple[MagicMock, MagicMock]:
    interface = MagicMock(autospec=TCPInterface)
    interface.__enter__ = MagicMock(return_value=interface)
    interface.__exit__ = MagicMock(return_value=None)
    node = MagicMock(autospec=Node)
    node.localConfig = localonly_pb2.LocalConfig()
    node.moduleConfig = localonly_pb2.LocalModuleConfig()
    interface.getNode.return_value = node
    return interface, node


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_invalid_batch_is_rejected_before_any_config_mutation(
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
            "--set",
            "bluetooth.enabled",
            "true",
            "--set",
            "lora.hop_limit",
            "not_a_number",
        ],
    )
    interface, node = _interface_with_config_node()

    with patch("meshtastic.tcp_interface.TCPInterface", return_value=interface):
        with pytest.raises(SystemExit) as exc_info:
            main()

    assert exc_info.value.code == 1
    assert node.localConfig.bluetooth.enabled is False
    assert node.localConfig.lora.hop_limit == 0
    node.beginSettingsTransaction.assert_not_called()
    node.writeConfig.assert_not_called()
    node.commitSettingsTransaction.assert_not_called()
    _out, err = capsys.readouterr()
    assert "--set batch rejected before applying changes" in err
    assert "lora.hop_limit" in err


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_preflight_reports_every_invalid_entry(
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
            "--set",
            "lora.hop_limit",
            "bad",
            "--set",
            "power.ls_secs",
            "also_bad",
        ],
    )
    interface, node = _interface_with_config_node()

    with patch("meshtastic.tcp_interface.TCPInterface", return_value=interface):
        with pytest.raises(SystemExit):
            main()

    _out, err = capsys.readouterr()
    assert "lora.hop_limit" in err
    assert "power.ls_secs" in err
    node.writeConfig.assert_not_called()


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_valid_multi_section_batch_still_uses_one_transaction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "meshtastic",
            "--host",
            "meshtastic.local",
            "--set",
            "bluetooth.enabled",
            "true",
            "--set",
            "power.ls_secs",
            "300",
        ],
    )
    interface, node = _interface_with_config_node()

    with patch("meshtastic.tcp_interface.TCPInterface", return_value=interface):
        main()

    assert node.localConfig.bluetooth.enabled is True
    assert node.localConfig.power.ls_secs == 300
    node.beginSettingsTransaction.assert_called_once_with()
    assert {call.args[0] for call in node.writeConfig.call_args_list} == {
        "bluetooth",
        "power",
    }
    node.commitSettingsTransaction.assert_called_once_with()


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_nonfatal_validation_rejection_still_prevents_prior_batch_mutation(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A validation warning later in a batch must not apply an earlier entry."""
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "meshtastic",
            "--host",
            "meshtastic.local",
            "--set",
            "bluetooth.enabled",
            "true",
            "--set",
            "network.wifi_psk",
            "short",
        ],
    )
    interface, node = _interface_with_config_node()

    with patch("meshtastic.tcp_interface.TCPInterface", return_value=interface):
        main()

    assert node.localConfig.bluetooth.enabled is False
    node.writeConfig.assert_not_called()
    out, err = capsys.readouterr()
    assert "network.wifi_psk must be 8 or more characters" in out
    assert err == ""
