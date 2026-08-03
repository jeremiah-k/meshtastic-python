"""Meshtastic unit tests for __main__.py."""

# pylint: disable=W0613,R0917

import base64
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import MagicMock

import pytest
import yaml

import meshtastic.__main__ as main_module
from meshtastic.tests._main_legacy_support import (
    build_configure_interface as _build_configure_interface,
    patch_fast_monotonic as _patch_fast_monotonic,
    run_main_configure_file as _run_main_configure_file,
)

# from ..radioconfig_pb2 import UserPreferences
# import meshtastic.config_pb2
from ..protobuf import config_pb2, localonly_pb2

# from ..ble_interface import BLEInterface


# from ..remote_hardware import onGPIOreceive
# from ..config_pb2 import Config


SDS_DISABLED_SENTINEL: int = 4_294_967_295
MAIN_LOCAL_ADDR: str = cast(str, main_module.__dict__["LOCAL_ADDR"])


def _get_config_field(config: Any, dotted_path: str) -> Any:
    """Walk a dotted `section.field` path on a protobuf Config message."""
    obj = config
    for part in dotted_path.split("."):
        obj = getattr(obj, part)
    return obj


@pytest.fixture(autouse=True)
def _mock_newer_version_check(monkeypatch: pytest.MonkeyPatch) -> None:
    """Prevent external network calls during unit tests in this module.

    Parameters
    ----------
    monkeypatch : pytest.MonkeyPatch
        Pytest monkeypatching fixture.
    """
    monkeypatch.setattr("meshtastic.util.check_if_newer_version", lambda: None)


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_main_configure_phase3_verified_with_matching_config_values(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config_path = tmp_path / "phase3_verified.yaml"
    config_path.write_text(
        yaml.safe_dump({"config": {"power": {"ls_secs": 222}}}),
        encoding="utf-8",
    )
    target_local = localonly_pb2.LocalConfig()
    iface, target_node = _build_configure_interface(
        target_local, localonly_pb2.LocalModuleConfig()
    )
    iface.isConnected = threading.Event()
    iface.isConnected.set()
    iface.waitForConfig = MagicMock()
    target_node.requestConfig = MagicMock(
        side_effect=lambda field_desc: (
            setattr(target_local.power, "ls_secs", 222)
            if getattr(field_desc, "name", "") == "power"
            else None
        )
    )
    _patch_fast_monotonic(monkeypatch)
    _run_main_configure_file(config_path, iface, monkeypatch)
    out, _ = capsys.readouterr()
    assert "All settings verified" in out


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_main_configure_phase1_direct_write_order(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "phase1_order.yaml"
    config_path.write_text(
        yaml.safe_dump(
            {
                "owner": "OrderTest",
                "owner_short": "OT",
                "location": {"lat": 1.0, "lon": 2.0, "alt": 3.0},
                "canned_messages": "A|B|C",
                "ringtone": "24:d=16,o=5,b=100:c",
                "channel_url": "https://meshtastic.org/e/#CgYSAQABAA",
            }
        ),
        encoding="utf-8",
    )
    iface, target_node = _build_configure_interface()
    _patch_fast_monotonic(monkeypatch)
    _run_main_configure_file(config_path, iface, monkeypatch)

    phase1_methods = (
        "setOwner",
        "setFixedPosition",
        "set_canned_message",
        "set_ringtone",
        "setURL",
    )
    method_names = [c[0] for c in target_node.method_calls]
    relevant = [m for m in method_names if m in phase1_methods]
    expected = [
        "setOwner",
        "setOwner",
        "setFixedPosition",
        "set_canned_message",
        "set_ringtone",
        "setURL",
    ]
    assert relevant == expected


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_main_configure_channel_url_is_terminal_phase1_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "terminal_seturl.yaml"
    config_path.write_text(
        yaml.safe_dump(
            {
                "owner": "TerminalTest",
                "location": {"lat": 10.0, "lon": 20.0, "alt": 30.0},
                "channel_url": "https://meshtastic.org/e/#CgYSAQABAA",
            }
        ),
        encoding="utf-8",
    )
    iface, target_node = _build_configure_interface()
    _patch_fast_monotonic(monkeypatch)
    _run_main_configure_file(config_path, iface, monkeypatch)

    method_names = [c[0] for c in target_node.method_calls]
    seturl_indices = [i for i, m in enumerate(method_names) if m == "setURL"]
    assert len(seturl_indices) == 1
    seturl_idx = seturl_indices[0]
    after_seturl = method_names[seturl_idx + 1 :]
    for method in (
        "setFixedPosition",
        "set_canned_message",
        "set_ringtone",
        "setOwner",
    ):
        assert method not in after_seturl


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_main_configure_seturl_unstable_aborts_before_phase2(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config_path = tmp_path / "unstable_seturl.yaml"
    config_path.write_text(
        yaml.safe_dump(
            {
                "channel_url": "https://meshtastic.org/e/#CgYSAQABAA",
                "config": {"power": {"ls_secs": 222}},
            }
        ),
        encoding="utf-8",
    )
    target_local = localonly_pb2.LocalConfig()
    iface, target_node = _build_configure_interface(
        target_local, localonly_pb2.LocalModuleConfig()
    )
    iface.isConnected = threading.Event()
    iface.isConnected.set()
    iface.waitForConfig = MagicMock()
    monkeypatch.setattr(
        "meshtastic.__main__._post_seturl_stability_check",
        lambda *a, **k: False,
    )
    _patch_fast_monotonic(monkeypatch)
    with pytest.raises(SystemExit):
        _run_main_configure_file(config_path, iface, monkeypatch)

    target_node.beginSettingsTransaction.assert_not_called()
    _, err = capsys.readouterr()
    assert "transport did not stabilize" in err


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_main_configure_seturl_stable_proceeds_to_phase2(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "stable_seturl.yaml"
    config_path.write_text(
        yaml.safe_dump(
            {
                "channel_url": "https://meshtastic.org/e/#CgYSAQABAA",
                "config": {"power": {"ls_secs": 222}},
            }
        ),
        encoding="utf-8",
    )
    target_local = localonly_pb2.LocalConfig()
    iface, target_node = _build_configure_interface(
        target_local, localonly_pb2.LocalModuleConfig()
    )
    iface.isConnected = threading.Event()
    iface.isConnected.set()
    iface.waitForConfig = MagicMock()
    monkeypatch.setattr(
        "meshtastic.__main__._post_seturl_stability_check",
        lambda *a, **k: True,
    )
    _patch_fast_monotonic(monkeypatch)
    _run_main_configure_file(config_path, iface, monkeypatch)

    target_node.beginSettingsTransaction.assert_called_once()
    target_node.commitSettingsTransaction.assert_called_once()


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_main_configure_phase3_no_reconnect_needed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config_path = tmp_path / "phase3_no_reboot.yaml"
    config_path.write_text(
        yaml.safe_dump({"owner": "TestUser"}),
        encoding="utf-8",
    )
    iface, target_node = _build_configure_interface()
    _run_main_configure_file(config_path, iface, monkeypatch)
    out, _ = capsys.readouterr()
    assert "no reboot expected" in out


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_main_configure_channel_url_only_reports_possible_reconnect(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config_path = tmp_path / "phase1_channel_url_only.yaml"
    config_path.write_text(
        yaml.safe_dump({"channel_url": "https://meshtastic.org/e/#CgcSAQE6AggN"}),
        encoding="utf-8",
    )
    iface, target_node = _build_configure_interface()
    _run_main_configure_file(config_path, iface, monkeypatch)
    out, _ = capsys.readouterr()
    assert "Phase 1: Applying direct configuration" in out
    assert (
        "Configuration applied. Channel URL updates may still trigger reconnect/reboot."
        in out
    )
    assert "Configuration applied (no reboot expected)." not in out
    target_node.setURL.assert_called_once()


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_main_configure_channel_url_skip_when_already_matching(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config_path = tmp_path / "phase1_channel_url_skip.yaml"
    config_path.write_text(
        yaml.safe_dump({"channel_url": "https://meshtastic.org/e/#CgcSAQE6AggN"}),
        encoding="utf-8",
    )
    iface, target_node = _build_configure_interface()
    monkeypatch.setattr(
        "meshtastic.__main__._channel_url_matches_current_device_state",
        lambda *a, **k: True,
    )
    _run_main_configure_file(config_path, iface, monkeypatch)
    out, _ = capsys.readouterr()
    assert "Channel url already matches device state; skipping apply." in out
    assert "Configuration applied (no reboot expected)." in out
    target_node.setURL.assert_not_called()


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_main_configure_phase3_channel_url_verified(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from ..protobuf import apponly_pb2, channel_pb2

    config_path = tmp_path / "phase3_channel_url.yaml"
    channel_settings = channel_pb2.ChannelSettings()
    channel_settings.psk = b"\x01"
    channel_settings.name = "test"
    cs = apponly_pb2.ChannelSet()
    cs.settings.add().CopyFrom(channel_settings)
    cs.lora_config.region = config_pb2.Config.LoRaConfig.RegionCode.Value("US")
    cs.lora_config.hop_limit = 3
    raw = cs.SerializeToString()
    b64 = base64.b64encode(raw, altchars=b"-_").decode().rstrip("=")
    test_url = f"https://meshtastic.org/e/#{b64}"
    config_path.write_text(
        yaml.safe_dump(
            {
                "channel_url": test_url,
                "config": {"power": {"ls_secs": 222}},
            }
        ),
        encoding="utf-8",
    )
    target_local = localonly_pb2.LocalConfig()
    target_local.lora.region = config_pb2.Config.LoRaConfig.RegionCode.Value("US")
    target_local.lora.hop_limit = 3
    iface, target_node = _build_configure_interface(
        target_local, localonly_pb2.LocalModuleConfig()
    )
    primary_channel = channel_pb2.Channel()
    primary_channel.role = channel_pb2.Channel.Role.PRIMARY
    primary_channel.settings.CopyFrom(channel_settings)

    def _request_channels_side_effect(*_args: object) -> None:
        target_node.channels = [primary_channel]

    target_node.channels = [primary_channel]
    iface.isConnected = threading.Event()
    iface.isConnected.set()
    iface.waitForConfig = MagicMock()
    target_node.requestConfig = MagicMock(
        side_effect=lambda field_desc: (
            setattr(target_local.power, "ls_secs", 222)
            if getattr(field_desc, "name", "") == "power"
            else None
        )
    )
    target_node.requestChannels = MagicMock(side_effect=_request_channels_side_effect)
    monkeypatch.setattr(
        "meshtastic.__main__._verify_channel_url_against_state",
        lambda *a, **k: False,
    )
    monkeypatch.setattr(
        "meshtastic.__main__._post_seturl_stability_check",
        lambda *a, **k: True,
    )
    _patch_fast_monotonic(monkeypatch)
    _run_main_configure_file(config_path, iface, monkeypatch)
    out, _ = capsys.readouterr()
    assert "Could not fully verify" in out


@pytest.mark.unit
def test_post_seturl_stability_check_triggers_reconnect_when_disconnected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    event = threading.Event()
    iface = SimpleNamespace(
        isConnected=event,
        waitForConfig=MagicMock(),
    )
    iface.connect = MagicMock(side_effect=event.set)
    monotonic_value = [0.0]

    def _monotonic() -> float:
        monotonic_value[0] += 0.1
        return monotonic_value[0]

    monkeypatch.setattr(main_module.time, "monotonic", _monotonic)
    monkeypatch.setattr("time.sleep", lambda _: None)

    assert (
        main_module._post_seturl_stability_check(cast(Any, iface), timeout=2.0) is True
    )
    iface.connect.assert_called()
    iface.waitForConfig.assert_called_once()


@pytest.mark.unit
@pytest.mark.usefixtures("reset_mt_config")
def test_configure_redacts_channel_url_progress_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Channel URLs contain channel keys and must not be echoed while applying YAML."""
    secret_url = "https://meshtastic.org/e/#distinctive-channel-key"
    config_path = tmp_path / "channel.yaml"
    config_path.write_text(
        yaml.safe_dump({"channel_url": secret_url}), encoding="utf-8"
    )
    target_node = MagicMock()
    target_node.getURL.return_value = "https://meshtastic.org/e/#old-key"
    iface = MagicMock()
    iface.getNode.return_value = target_node
    iface.localNode = target_node
    args = SimpleNamespace(configure=[str(config_path)], dest=main_module.LOCAL_ADDR)
    monkeypatch.setattr(main_module.time, "sleep", lambda _seconds: None)

    main_module._handle_configure_command(iface, args, {})

    out, err = capsys.readouterr()
    target_node.setURL.assert_called_once_with(secret_url)
    assert "Setting channel url to <redacted>" in out
    assert secret_url not in out
    assert "distinctive-channel-key" not in out
    assert err == ""
