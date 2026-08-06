"""Focused edge-case tests for the extracted configure action runtime."""

from __future__ import annotations

import argparse
import contextvars
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import MagicMock

import pytest

from meshtastic.cli import configure_actions
from meshtastic.cli.configure_actions import (
    ConfigureActionHooks,
    ConfigureHooks,
    ConfigureReconnectResult,
)
from meshtastic.cli.context import ActionOutcome, CliContext, CliExit
from meshtastic.mesh_interface import MeshInterface


def _cli_exit(_message: str, return_value: int = 1) -> None:
    raise SystemExit(return_value)


def _hooks(**overrides: Any) -> ConfigureHooks:
    values: dict[str, Any] = {
        "cli_exit": cast(CliExit, _cli_exit),
        "cli_print": MagicMock(),
        "traverse_config": MagicMock(return_value=True),
        "preflight_mode": contextvars.ContextVar(
            "configure_edge_preflight", default=False
        ),
        "is_local_destination": MagicMock(return_value=True),
        "post_seturl_stability_check": MagicMock(return_value=True),
        "post_configure_reconnect_and_verify": MagicMock(
            return_value=ConfigureReconnectResult.VERIFIED
        ),
        "channel_url_matches_current_device_state": MagicMock(return_value=False),
        "pace_configure_write": MagicMock(),
    }
    values.update(overrides)
    return ConfigureHooks(**values)


def _interface(connected: bool = True) -> MagicMock:
    iface = MagicMock(spec=MeshInterface)
    iface.isConnected = threading.Event()
    if connected:
        iface.isConnected.set()
    iface.waitForConfig = MagicMock()
    return iface


@pytest.mark.unit
def test_reconnect_verify_reports_refresh_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No-disconnect refresh failures should become a config-reload result."""
    iface = _interface()
    iface.getNode.return_value = MagicMock()
    monkeypatch.setattr(configure_actions.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(
        configure_actions,
        "_refresh_no_disconnect_verify_state",
        MagicMock(side_effect=RuntimeError("refresh failed")),
    )

    result = configure_actions._post_configure_reconnect_and_verify(
        iface,
        timeout=1.0,
        node_dest="^local",
        verify_config_fields={"power": {"ls_secs": 1}},
    )

    assert result is ConfigureReconnectResult.CONFIG_RELOAD_FAILED


@pytest.mark.unit
def test_reconnect_verify_reports_unexpected_verifier_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unexpected verifier exceptions should remain an incomplete verification."""
    iface = _interface()
    iface.getNode.return_value = MagicMock()
    monkeypatch.setattr(configure_actions.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(
        configure_actions, "_refresh_no_disconnect_verify_state", MagicMock()
    )
    monkeypatch.setattr(
        configure_actions,
        "_verify_post_reconnect_config",
        MagicMock(side_effect=RuntimeError("verify failed")),
    )

    result = configure_actions._post_configure_reconnect_and_verify(
        iface,
        timeout=1.0,
        node_dest="^local",
        verify_config_fields={"power": {"ls_secs": 1}},
    )

    assert result is ConfigureReconnectResult.VERIFICATION_INCOMPLETE


class _ConnectionEvent:
    """Mutable connection-event double for deterministic stability tests."""

    def __init__(self, states: list[bool], *, wait_result: bool = False) -> None:
        self._states = iter(states)
        self._last = False
        self.wait_result = wait_result

    def is_set(self) -> bool:
        try:
            self._last = next(self._states)
        except StopIteration:
            pass
        return self._last

    def wait(self, _timeout: float) -> bool:
        return self.wait_result


@pytest.mark.unit
def test_seturl_stability_reconnect_hook_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A reconnect hook that restores connectivity should reach config reload."""
    iface = MagicMock()
    iface.isConnected = _ConnectionEvent([False, True, True, True, True])
    iface._attempt_reconnect = MagicMock(return_value=True)
    iface.waitForConfig = MagicMock()
    ticks = iter(float(value) for value in range(20))
    monkeypatch.setattr(configure_actions.time, "monotonic", lambda: next(ticks, 20.0))
    monkeypatch.setattr(configure_actions.time, "sleep", lambda _seconds: None)

    assert configure_actions._post_seturl_stability_check(iface, timeout=10.0)
    iface._attempt_reconnect.assert_called_once_with()
    iface.waitForConfig.assert_called_once_with()


@pytest.mark.unit
def test_seturl_stability_reconnect_and_connect_fail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reconnect trigger failures should fall through to a bounded false result."""
    iface = MagicMock()
    iface.isConnected = _ConnectionEvent([False] * 20)
    iface._attempt_reconnect = MagicMock(side_effect=RuntimeError("reconnect"))
    iface.connect = MagicMock(side_effect=RuntimeError("connect"))
    monkeypatch.setattr(configure_actions.time, "monotonic", lambda: 0.0)
    monkeypatch.setattr(configure_actions.time, "sleep", lambda _seconds: None)

    assert configure_actions._post_seturl_stability_check(iface, timeout=1.0) is False
    assert (
        iface._attempt_reconnect.call_count
        == configure_actions.SETURL_STABILITY_MAX_ATTEMPTS
    )
    assert iface.connect.call_count == configure_actions.SETURL_STABILITY_MAX_ATTEMPTS


@pytest.mark.unit
def test_seturl_stability_respects_expired_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An exhausted budget must terminate before another reconnect attempt."""
    iface = MagicMock()
    iface.isConnected = _ConnectionEvent([False])
    ticks = iter([0.0, 2.0])
    monkeypatch.setattr(configure_actions.time, "monotonic", lambda: next(ticks, 2.0))

    assert configure_actions._post_seturl_stability_check(iface, timeout=1.0) is False


@pytest.mark.unit
def test_seturl_stability_detects_drop_during_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A transport drop inside the stability window should retry then fail boundedly."""
    iface = MagicMock()
    # Per attempt: connected gate, first window check false. Repeat three times.
    iface.isConnected = _ConnectionEvent([True, False, True, False, True, False])
    ticks = iter(float(value) for value in range(20))
    monkeypatch.setattr(configure_actions.time, "monotonic", lambda: next(ticks, 20.0))
    monkeypatch.setattr(configure_actions.time, "sleep", lambda _seconds: None)

    assert configure_actions._post_seturl_stability_check(iface, timeout=1.0) is False


@pytest.mark.unit
def test_seturl_stability_retries_config_reload_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Config reload failure after a stable transport should consume all attempts."""
    iface = MagicMock()
    iface.isConnected = _ConnectionEvent([True] * 30)
    iface.waitForConfig.side_effect = RuntimeError("reload")
    ticks = iter(float(value) for value in range(40))
    monkeypatch.setattr(configure_actions.time, "monotonic", lambda: next(ticks, 40.0))
    monkeypatch.setattr(configure_actions.time, "sleep", lambda _seconds: None)

    assert configure_actions._post_seturl_stability_check(iface, timeout=100.0) is False
    assert (
        iface.waitForConfig.call_count
        == configure_actions.SETURL_STABILITY_MAX_ATTEMPTS
    )


@pytest.mark.unit
def test_refresh_verify_state_skips_unknown_sections() -> None:
    """Unknown config and module-config sections should be skipped without mutation."""
    node = MagicMock()
    node.localConfig.DESCRIPTOR.fields_by_name = {}
    node.moduleConfig.DESCRIPTOR.fields_by_name = {}

    configure_actions._refresh_no_disconnect_verify_state(
        node,
        verify_channel_url=None,
        verify_config_fields={"futureConfig": {"x": 1}},
        verify_module_config_fields={"futureModule": {"x": 1}},
    )

    node.localConfig.ClearField.assert_not_called()
    node.moduleConfig.ClearField.assert_not_called()


@pytest.mark.unit
@pytest.mark.parametrize(
    ("local_config", "expected"),
    [
        (None, None),
        (SimpleNamespace(HasField=None), None),
        (SimpleNamespace(HasField=lambda _name: False), None),
    ],
)
def test_device_lora_config_handles_absent_state(
    local_config: Any, expected: Any
) -> None:
    """LoRa state probing should tolerate every unloaded-state representation."""
    node = SimpleNamespace(localConfig=local_config)
    assert configure_actions._device_lora_config(node) is expected


@pytest.mark.unit
def test_device_lora_config_returns_loaded_message() -> None:
    """Loaded LoRa state should be returned once and reused by verifiers."""
    lora = object()
    node = SimpleNamespace(
        localConfig=SimpleNamespace(HasField=lambda name: name == "lora", lora=lora)
    )
    assert configure_actions._device_lora_config(node) is lora


@pytest.mark.unit
def test_post_reconnect_verification_rejects_disconnected_transport() -> None:
    """Verification must not read stale node state while disconnected."""
    iface = _interface(connected=False)
    result = configure_actions._verify_post_reconnect_config(iface, "^local")
    assert result is ConfigureReconnectResult.VERIFICATION_INCOMPLETE
    iface.getNode.assert_not_called()


@pytest.mark.unit
def test_post_reconnect_verification_tracks_channel_url_success() -> None:
    """A matching URL with loaded LoRa state should complete verification."""
    iface = _interface()
    node = MagicMock()
    node.localConfig.HasField.return_value = True
    iface.getNode.return_value = node
    verify_url = MagicMock(return_value=True)

    result = configure_actions._verify_post_reconnect_config(
        iface,
        "^local",
        verify_channel_url="https://example.invalid/#ok",
        verify_channel_url_against_state=verify_url,
    )

    assert result is ConfigureReconnectResult.VERIFIED
    assert verify_url.call_args.kwargs["device_lora_config"] is node.localConfig.lora


@pytest.mark.unit
def test_post_reconnect_module_verification_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A module-config mismatch should return incomplete verification."""
    iface = _interface()
    iface.getNode.return_value = MagicMock()
    monkeypatch.setattr(
        configure_actions, "_verify_config_sections", MagicMock(return_value=False)
    )

    result = configure_actions._verify_post_reconnect_config(
        iface,
        "^local",
        verify_module_config_fields={"telemetry": {"enabled": True}},
    )

    assert result is ConfigureReconnectResult.VERIFICATION_INCOMPLETE


@pytest.mark.unit
def test_post_reconnect_detects_disconnect_after_comparison(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A disconnect after values match must prevent a false verified result."""
    iface = _interface()
    iface.isConnected = MagicMock()
    iface.isConnected.is_set.side_effect = [True, False]
    iface.getNode.return_value = MagicMock()
    monkeypatch.setattr(
        configure_actions, "_verify_config_sections", MagicMock(return_value=True)
    )

    result = configure_actions._verify_post_reconnect_config(
        iface,
        "^local",
        verify_config_fields={"power": {"ls_secs": 1}},
    )

    assert result is ConfigureReconnectResult.VERIFICATION_INCOMPLETE


@pytest.mark.unit
def test_close_failed_settings_transaction_does_not_retry_failed_commit() -> None:
    """A failed normal commit must not be retried when device state is unknown."""
    node = MagicMock()
    cli_print = MagicMock()
    configure_actions._close_failed_settings_transaction(
        _hooks(cli_print=cli_print), node, commit_attempted=True
    )
    node.commitSettingsTransaction.assert_not_called()
    assert "state is unknown" in str(cli_print.call_args)


@pytest.mark.unit
def test_close_failed_settings_transaction_reports_close_failure() -> None:
    """Failure of the only available transaction-close operation must be visible."""
    node = MagicMock()
    node.commitSettingsTransaction.side_effect = RuntimeError("commit")
    cli_print = MagicMock()
    configure_actions._close_failed_settings_transaction(
        _hooks(cli_print=cli_print), node, commit_attempted=False
    )
    assert node.commitSettingsTransaction.call_count == 1
    assert "open transaction" in str(cli_print.call_args)


@pytest.mark.unit
@pytest.mark.parametrize(
    "configuration",
    [
        {"owner": None},
        {"owner_short": "   "},
        {"location": []},
        {"location": {"lat": 1, "lon": 2, "bogus": 3}},
        {"location": {"lat": 1}},
        {"location": {"lat": True, "lon": 0}},
        {"location": {"lat": "bad", "lon": 0}},
        {"location": {"lat": 0, "lon": False}},
        {"location": {"lat": 0, "lon": "bad"}},
        {"location": {"lat": 91, "lon": 0}},
        {"location": {"lat": 0, "lon": 181}},
        {"location": {"lat": 0, "lon": 0, "alt": True}},
        {"location": {"lat": 0, "lon": 0, "alt": "bad"}},
        {"location": {"lat": 0, "lon": 0, "alt": 1 << 31}},
        {"canned_messages": 123},
        {"ringtone": object()},
        {"channel_url": 123},
        {"channel_url": "   "},
    ],
)
def test_direct_write_validation_rejects_all_invalid_direct_write_shapes(
    configuration: dict[str, Any],
) -> None:
    """Ensure deterministic direct-write validation fails before device mutation."""
    with pytest.raises(SystemExit):
        configure_actions._validate_direct_configuration(_hooks(), configuration)


@pytest.mark.unit
def test_direct_write_validation_preserves_alias_and_altitude_metadata() -> None:
    """Normalized direct-write values should carry all apply-time shape decisions."""
    values = configure_actions._validate_direct_configuration(
        _hooks(),
        {
            "owner": " Owner ",
            "ownerShort": " OS ",
            "location": {"lat": 90, "lon": 180, "alt": -(1 << 31)},
            "channelUrl": " https://example.invalid/#abc ",
            "canned_messages": "hello",
            "ringtone": "tone",
        },
    )
    assert values.owner == "Owner"
    assert values.owner_short == "OS"
    assert values.location == (90.0, 180.0, -(1 << 31))
    assert values.altitude_specified is True
    assert values.channel_url_key == "channelUrl"
    assert values.channel_url == "https://example.invalid/#abc"


@pytest.mark.unit
@pytest.mark.parametrize(
    ("text", "expected_fragment"),
    [
        ("", "empty"),
        ("[]", "mapping/dictionary"),
        ("{}", "nothing to configure"),
        ("unknown: true", "Unknown top-level key(s)"),
        ("owner_short: A\nownerShort: B", "both 'owner_short' and 'ownerShort'"),
        ("channel_url: x\nchannelUrl: y", "both 'channel_url' and 'channelUrl'"),
        ("config: []", "'config' must be a non-empty mapping"),
        ("module_config: []", "'module_config' must be a non-empty mapping"),
    ],
)
def test_configure_document_rejects_invalid_top_level_shapes(
    tmp_path: Path, text: str, expected_fragment: str
) -> None:
    """Document structural failures should terminate with actionable diagnostics."""
    path = tmp_path / "bad.yaml"
    path.write_text(text, encoding="utf-8")
    cli_exit = MagicMock(side_effect=SystemExit(1))
    with pytest.raises(SystemExit):
        configure_actions._load_and_validate_configure_document(
            _hooks(cli_exit=cast(CliExit, cli_exit)), str(path)
        )
    assert expected_fragment in str(cli_exit.call_args)


@pytest.mark.unit
def test_configure_document_reports_read_and_parse_failures(tmp_path: Path) -> None:
    """Filesystem and YAML parse failures should not leak raw tracebacks."""
    for path in [tmp_path / "missing.yaml", tmp_path / "invalid.yaml"]:
        if path.name == "invalid.yaml":
            path.write_text("[unterminated", encoding="utf-8")
        with pytest.raises(SystemExit):
            configure_actions._load_and_validate_configure_document(_hooks(), str(path))


@pytest.mark.unit
def test_apply_direct_configuration_prints_explicit_altitude_and_applies_all_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Prepared direct writes should apply without rereading raw YAML shape."""
    node = MagicMock()
    cli_print = MagicMock()
    hooks = _hooks(cli_print=cli_print)
    prepared = configure_actions._PreparedConfigureDocument(
        direct_values=configure_actions._DirectConfigureValues(
            owner="Owner",
            owner_short="OS",
            location=(1.0, 2.0, 3),
            altitude_specified=True,
            canned_messages="hello",
            ringtone="tone",
            channel_url="https://example.invalid/#abc",
            channel_url_key="channelUrl",
        ),
        config_sections={},
        module_config_sections={},
    )
    monkeypatch.setattr(configure_actions.time, "sleep", lambda _seconds: None)

    result = configure_actions._apply_direct_configuration(hooks, node, prepared)

    assert result is True
    node.setFixedPosition.assert_called_once_with(1.0, 2.0, 3)
    node.setURL.assert_called_once_with("https://example.invalid/#abc")
    assert "Fixing altitude at 3 meters" in [
        c.args[0] for c in cli_print.call_args_list
    ]


@pytest.mark.unit
def test_apply_settings_transaction_reports_write_failure() -> None:
    """A failed section write should close the opened transaction and re-raise."""
    node = MagicMock()
    node.writeConfig.side_effect = RuntimeError("write failed")
    hooks = _hooks(traverse_config=MagicMock(return_value=True))

    with pytest.raises(RuntimeError, match="write failed"):
        configure_actions._apply_settings_transaction(
            hooks,
            node,
            config_sections={"power": {"ls_secs": 1}},
            module_config_sections={},
        )

    node.beginSettingsTransaction.assert_called_once_with()
    node.commitSettingsTransaction.assert_called_once_with()


@pytest.mark.unit
def test_apply_settings_transaction_fails_when_traversal_rejects_section() -> None:
    """A traversal rejection should fail closed and close the transaction."""
    node = MagicMock()
    hooks = _hooks(traverse_config=MagicMock(return_value=False))

    with pytest.raises(SystemExit):
        configure_actions._apply_settings_transaction(
            hooks,
            node,
            config_sections={"power": {"ls_secs": 1}},
            module_config_sections={},
        )
    node.commitSettingsTransaction.assert_called_once_with()


@pytest.mark.unit
def test_configure_actions_remote_export_and_write_failure(tmp_path: Path) -> None:
    """Export dispatch should stop on remote targets and report local write failures."""
    interface = cast(MeshInterface, MagicMock())
    remote_context = CliContext(
        interface=interface,
        args=argparse.Namespace(
            set=None,
            configure=None,
            export_config=str(tmp_path / "unused"),
            dest="!remote",
        ),
        get_node_kwargs={},
        outcome=ActionOutcome(),
    )
    export_config = MagicMock(return_value="yaml")
    action_hooks = ConfigureActionHooks(
        handle_set_command=MagicMock(),
        handle_configure_command=MagicMock(return_value=(False, False)),
        export_config=export_config,
        cli_exit=cast(CliExit, _cli_exit),
        cli_print=MagicMock(),
        is_local_destination=MagicMock(return_value=False),
    )
    configure_actions._handle_configure_actions(remote_context, action_hooks)
    assert remote_context.outcome.stop_processing is True
    export_config.assert_not_called()

    local_context = CliContext(
        interface=interface,
        args=argparse.Namespace(
            set=None,
            configure=None,
            export_config=str(tmp_path / "missing" / "config.yaml"),
            dest="^all",
        ),
        get_node_kwargs={},
        outcome=ActionOutcome(),
    )
    local_hooks = ConfigureActionHooks(
        handle_set_command=action_hooks.handle_set_command,
        handle_configure_command=action_hooks.handle_configure_command,
        export_config=action_hooks.export_config,
        cli_exit=action_hooks.cli_exit,
        cli_print=action_hooks.cli_print,
        is_local_destination=MagicMock(return_value=True),
    )
    with pytest.raises(SystemExit):
        configure_actions._handle_configure_actions(local_context, local_hooks)


@pytest.mark.unit
def test_seturl_stability_marks_midwindow_drop_unstable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A connection that drops after entering the stability window must retry."""
    iface = MagicMock()
    iface.isConnected = _ConnectionEvent([True, True, False])
    ticks = iter(float(value) for value in range(20))
    monkeypatch.setattr(configure_actions.time, "monotonic", lambda: next(ticks, 20.0))
    monkeypatch.setattr(configure_actions.time, "sleep", lambda _seconds: None)

    assert configure_actions._post_seturl_stability_check(iface, timeout=100.0) is False
    iface.waitForConfig.assert_not_called()


@pytest.mark.unit
def test_post_reconnect_local_config_mismatch_is_incomplete(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A local-config mismatch should terminate verification before module checks."""
    iface = _interface()
    iface.getNode.return_value = MagicMock()
    verifier = MagicMock(return_value=False)
    monkeypatch.setattr(configure_actions, "_verify_config_sections", verifier)

    result = configure_actions._verify_post_reconnect_config(
        iface,
        "^local",
        verify_config_fields={"power": {"ls_secs": 1}},
    )

    assert result is ConfigureReconnectResult.VERIFICATION_INCOMPLETE
    verifier.assert_called_once()


@pytest.mark.unit
def test_apply_direct_configuration_rejects_inconsistent_normalized_url() -> None:
    """Prepared configure values must never carry a URL without its source alias."""
    prepared = configure_actions._PreparedConfigureDocument(
        direct_values=configure_actions._DirectConfigureValues(
            channel_url="https://example.invalid/#abc", channel_url_key=None
        ),
        config_sections={},
        module_config_sections={},
    )
    with pytest.raises(AssertionError, match="source key"):
        configure_actions._apply_direct_configuration(_hooks(), MagicMock(), prepared)


@pytest.mark.unit
def test_settings_transaction_logs_unknown_fields_without_rejecting_section(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unknown nested fields may be skipped while known fields still persist."""
    node = MagicMock()
    monkeypatch.setattr(configure_actions.time, "sleep", lambda _seconds: None)

    def _traverse(
        _section: str, _values: dict[str, Any], _root: Any, *, failed_fields: list[str]
    ) -> bool:
        failed_fields.append("future_field")
        return True

    configure_actions._apply_settings_transaction(
        _hooks(traverse_config=_traverse),
        node,
        config_sections={"power": {"future_field": 1}},
        module_config_sections={},
    )

    node.writeConfig.assert_called_once_with("power")
    node.commitSettingsTransaction.assert_called_once_with()


@pytest.mark.unit
def test_configure_actions_no_export_and_stdout_export(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Configure dispatch should cover the no-export fast path and stdout payload path."""
    interface = cast(MeshInterface, MagicMock())
    export_config = MagicMock(return_value="config: true\n")
    hooks = ConfigureActionHooks(
        handle_set_command=MagicMock(),
        handle_configure_command=MagicMock(return_value=(False, False)),
        export_config=export_config,
        cli_exit=cast(CliExit, _cli_exit),
        cli_print=MagicMock(),
        is_local_destination=MagicMock(return_value=True),
    )
    no_export = CliContext(
        interface=interface,
        args=argparse.Namespace(
            set=None, configure=None, export_config=None, dest="^all"
        ),
        get_node_kwargs={},
        outcome=ActionOutcome(),
    )
    configure_actions._handle_configure_actions(no_export, hooks)
    export_config.assert_not_called()

    stdout_export = CliContext(
        interface=interface,
        args=argparse.Namespace(
            set=None, configure=None, export_config="-", dest="^all"
        ),
        get_node_kwargs={},
        outcome=ActionOutcome(),
    )
    configure_actions._handle_configure_actions(stdout_export, hooks)
    assert "config: true" in capsys.readouterr().out


@pytest.mark.unit
def test_channel_url_match_requires_loaded_lora_state() -> None:
    """Channel URL short-circuit comparison should reject unloaded LoRa state."""
    node = SimpleNamespace(localConfig=None)
    comparator = MagicMock(return_value=True)

    assert not configure_actions._channel_url_matches_current_device_state(
        node,
        "https://example.invalid/#abc",
        verify_channel_url_against_state=comparator,
    )
    comparator.assert_not_called()


@pytest.mark.unit
def test_channel_url_match_delegates_with_normalized_loaded_state() -> None:
    """Loaded channel and LoRa state should be forwarded to the URL comparator."""
    lora = object()
    channels = [object()]
    node = SimpleNamespace(
        localConfig=SimpleNamespace(HasField=lambda name: name == "lora", lora=lora),
        channels=channels,
    )
    comparator = MagicMock(return_value=True)

    assert configure_actions._channel_url_matches_current_device_state(
        node,
        "https://example.invalid/#abc",
        verify_channel_url_against_state=comparator,
    )
    comparator.assert_called_once_with(
        "https://example.invalid/#abc",
        device_channels=channels,
        device_lora_config=lora,
        emit_warnings=False,
    )


@pytest.mark.unit
def test_channel_refresh_tolerates_legacy_node_without_cache_invalidator() -> None:
    """Channel verification refresh should support node doubles and older node seams."""
    node = SimpleNamespace(
        requestChannels=MagicMock(),
        localConfig=MagicMock(),
        moduleConfig=MagicMock(),
    )

    configure_actions._refresh_no_disconnect_verify_state(
        node,
        verify_channel_url="https://example.invalid/#abc",
        verify_config_fields=None,
        verify_module_config_fields=None,
    )

    node.requestChannels.assert_called_once_with(0)
