"""Focused edge-case tests for the extracted configure action runtime."""

from __future__ import annotations

import argparse
import contextvars
import os
import stat
import threading
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any, NoReturn, cast
from unittest.mock import MagicMock, create_autospec

import pytest

from meshtastic.cli import configure_actions, configure_values
from meshtastic.cli.configure_actions import (
    ConfigureActionHooks,
    ConfigureHooks,
    ConfigureReconnectResult,
)
from meshtastic.cli.context import ActionOutcome, CliContext, CliExit
from meshtastic.mesh_interface import MeshInterface

_PREFLIGHT_MODE: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "configure_edge_preflight", default=False
)


def _cli_exit(_message: str, return_value: int = 1) -> NoReturn:
    """Raise ``SystemExit`` in place of the production CLI-exit hook.

    Parameters
    ----------
    _message : str
        Ignored user-facing message.
    return_value : int
        Exit status carried by the raised exception.
    """
    raise SystemExit(return_value)


def _hooks(**overrides: Any) -> ConfigureHooks:
    """Build configure hooks with deterministic defaults.

    Parameters
    ----------
    **overrides : Any
        Hook values that should replace the focused-test defaults.

    Returns
    -------
    ConfigureHooks
        Fully populated configure-runtime dependency seams.
    """
    values: dict[str, Any] = {
        "cli_exit": _cli_exit,
        "cli_print": MagicMock(),
        "traverse_config": MagicMock(return_value=True),
        "preflight_mode": _PREFLIGHT_MODE,
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
    """Build a specced interface with controllable connection state.

    Parameters
    ----------
    connected : bool
        Whether the returned interface starts in the connected state.

    Returns
    -------
    MagicMock
        Autospecced ``MeshInterface`` double with a real ``threading.Event``.
    """
    iface = create_autospec(MeshInterface, instance=True)
    iface.isConnected = threading.Event()
    if connected:
        iface.isConnected.set()
    iface.waitForConfig = MagicMock()
    return iface


def _install_clock(
    monkeypatch: pytest.MonkeyPatch,
    *,
    monotonic: Callable[[], float] | None = None,
    sleep: Callable[[float], None] | None = None,
) -> None:
    """Install a configure-runtime clock without changing shared time functions."""
    current_time = configure_actions.time
    monkeypatch.setattr(
        configure_actions,
        "time",
        SimpleNamespace(
            monotonic=current_time.monotonic if monotonic is None else monotonic,
            sleep=current_time.sleep if sleep is None else sleep,
        ),
    )


@pytest.mark.unit
def test_reconnect_verify_reports_refresh_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No-disconnect refresh failures should become a config-reload result."""
    iface = _interface()
    iface.getNode.return_value = MagicMock()
    ticks = iter(float(value) for value in range(20))
    _install_clock(
        monkeypatch,
        monotonic=lambda: next(ticks, 20.0),
        sleep=lambda _seconds: None,
    )
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
    ticks = iter(float(value) for value in range(20))
    _install_clock(
        monkeypatch,
        monotonic=lambda: next(ticks, 20.0),
        sleep=lambda _seconds: None,
    )
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
        """Return the next scripted connection state.

        Returns
        -------
        bool
            Next scripted state, or the final state after the script is exhausted.
        """
        try:
            self._last = next(self._states)
        except StopIteration:
            pass
        return self._last

    def wait(self, _timeout: float) -> bool:
        """Return the scripted wait result without blocking.

        Parameters
        ----------
        _timeout : float
            Ignored wait budget supplied by the runtime.

        Returns
        -------
        bool
            Configured event-wait result.
        """
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
    _install_clock(
        monkeypatch,
        monotonic=lambda: next(ticks, 20.0),
        sleep=lambda _seconds: None,
    )

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
    _install_clock(
        monkeypatch,
        monotonic=lambda: 0.0,
        sleep=lambda _seconds: None,
    )

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
    _install_clock(monkeypatch, monotonic=lambda: next(ticks, 2.0))

    assert configure_actions._post_seturl_stability_check(iface, timeout=1.0) is False


@pytest.mark.unit
def test_seturl_stability_detects_drop_during_window(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A transport drop inside the stability window should retry then fail boundedly."""
    iface = MagicMock()
    # Per attempt: initial gate, pre-window gate, first window check.
    iface.isConnected = _ConnectionEvent(
        [True, True, False] * configure_actions.SETURL_STABILITY_MAX_ATTEMPTS
    )
    ticks = iter(float(value) for value in range(20))
    _install_clock(
        monkeypatch,
        monotonic=lambda: next(ticks, 20.0),
        sleep=lambda _seconds: None,
    )

    assert configure_actions._post_seturl_stability_check(iface, timeout=100.0) is False
    iface.waitForConfig.assert_not_called()
    assert caplog.text.count("Transport dropped during stability window") == (
        configure_actions.SETURL_STABILITY_MAX_ATTEMPTS
    )


@pytest.mark.unit
def test_seturl_stability_retries_config_reload_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Config reload failure after a stable transport should consume all attempts."""
    iface = MagicMock()
    iface.isConnected = _ConnectionEvent([True] * 30)
    iface.waitForConfig.side_effect = RuntimeError("reload")
    ticks = iter(float(value) for value in range(40))
    _install_clock(
        monkeypatch,
        monotonic=lambda: next(ticks, 40.0),
        sleep=lambda _seconds: None,
    )

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
        {"location": {"lat": 1, "lon": 2, 3: "non-string key"}},
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
        {"location": {"lat": 0, "lon": 0, "alt": 1.5}},
        {"location": {"lat": 0, "lon": 0, "alt": float("inf")}},
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
        configure_values._validate_direct_configuration(_hooks(), configuration)


@pytest.mark.unit
def test_direct_write_validation_preserves_alias_and_altitude_metadata() -> None:
    """Normalized direct-write values should carry all apply-time shape decisions."""
    values = configure_values._validate_direct_configuration(
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
        ("1: value", "configuration keys must be strings"),
        (
            "location:\n  lat: 0\n  lon: 0\n  1: value",
            "configuration.location keys must be strings",
        ),
        (
            "config:\n  1:\n    enabled: true",
            "configuration.config keys must be strings",
        ),
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
        direct_values=configure_values._DirectConfigureValues(
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
    _install_clock(monkeypatch, sleep=lambda _seconds: None)

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
        direct_values=configure_values._DirectConfigureValues(
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
    _install_clock(monkeypatch, sleep=lambda _seconds: None)

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
@pytest.mark.skipif(not hasattr(os, "fchmod"), reason="requires POSIX file modes")
def test_configure_export_restricts_existing_file_permissions(tmp_path: Path) -> None:
    """Secret-bearing exports must replace permissive modes with owner-only access."""
    export_path = tmp_path / "config.yaml"
    export_path.write_text("stale: true\n", encoding="utf-8")
    export_path.chmod(0o644)
    interface = cast(MeshInterface, MagicMock())
    hooks = ConfigureActionHooks(
        handle_set_command=MagicMock(),
        handle_configure_command=MagicMock(return_value=(False, False)),
        export_config=MagicMock(
            return_value="config:\n  security:\n    privateKey: secret\n"
        ),
        cli_exit=cast(CliExit, _cli_exit),
        cli_print=MagicMock(),
        is_local_destination=MagicMock(return_value=True),
    )
    context = CliContext(
        interface=interface,
        args=argparse.Namespace(
            set=None,
            configure=None,
            export_config=str(export_path),
            dest="^local",
        ),
        get_node_kwargs={},
        outcome=ActionOutcome(),
    )

    configure_actions._handle_configure_actions(context, hooks)

    assert export_path.read_text(encoding="utf-8") == (
        "config:\n  security:\n    privateKey: secret\n"
    )
    assert stat.S_IMODE(export_path.stat().st_mode) == (
        configure_actions.PRIVATE_CONFIG_FILE_MODE
    )


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


@pytest.mark.unit
def test_configure_result_reporting_handles_future_verification_result() -> None:
    """Reporting must not fail after commit when a verification seam returns a new result."""
    cli_print = MagicMock()
    hooks = _hooks(
        cli_print=cli_print,
        post_configure_reconnect_and_verify=MagicMock(return_value="future-result"),
    )

    configure_actions._report_configure_result(
        hooks,
        _interface(),
        destination="^local",
        is_local_target=True,
        settings_transaction_started=True,
        seturl_executed=False,
        channel_url=None,
        config_sections={},
        module_config_sections={},
    )

    cli_print.assert_called_once()
    message = cli_print.call_args.args[0]
    assert "unrecognized verification result" in message
    assert "future-result" in message


@pytest.mark.unit
def test_configure_command_result_preserves_two_item_tuple_contract() -> None:
    """Internal ACK metadata must not change the historical two-item result shape."""
    result = configure_actions._ConfigureCommandResult(False, False, request_sent=False)

    assert isinstance(result, tuple)
    assert result == (False, False)
    assert len(result) == 2
    assert result.settings_transaction_started is False
    assert result.local_channel_url_applied is False
    assert result.request_sent is False


@pytest.mark.unit
def test_configure_noop_does_not_arm_shared_ack_wait() -> None:
    """A confirmed configure no-op must not wait for an acknowledgment never sent."""
    interface = _interface()
    result = configure_actions._ConfigureCommandResult(False, False, request_sent=False)
    hooks = ConfigureActionHooks(
        handle_set_command=MagicMock(),
        handle_configure_command=MagicMock(return_value=result),
        export_config=MagicMock(),
        cli_exit=cast(CliExit, _cli_exit),
        cli_print=MagicMock(),
        is_local_destination=MagicMock(return_value=True),
    )
    context = CliContext(
        interface=interface,
        args=argparse.Namespace(
            set=None, configure=["config.yaml"], export_config=None, dest="^local"
        ),
        get_node_kwargs={},
        outcome=ActionOutcome(),
    )

    configure_actions._handle_configure_actions(context, hooks)

    assert context.outcome.close_now is True
    assert context.outcome.wait_for_ack_nak is False
    assert context.outcome.skip_ack_wait is False


@pytest.mark.unit
def test_set_ack_wait_survives_later_configure_noop() -> None:
    """A configure no-op must not disarm an ACK wait requested by ``--set``."""
    interface = _interface()
    result = configure_actions._ConfigureCommandResult(False, False, request_sent=False)
    set_command = MagicMock()
    hooks = ConfigureActionHooks(
        handle_set_command=set_command,
        handle_configure_command=MagicMock(return_value=result),
        export_config=MagicMock(),
        cli_exit=cast(CliExit, _cli_exit),
        cli_print=MagicMock(),
        is_local_destination=MagicMock(return_value=True),
    )
    context = CliContext(
        interface=interface,
        args=argparse.Namespace(
            set=[["lora.hop_limit", "3"]],
            configure=["config.yaml"],
            export_config=None,
            dest="^local",
        ),
        get_node_kwargs={},
        outcome=ActionOutcome(),
    )

    configure_actions._handle_configure_actions(context, hooks)

    set_command.assert_called_once_with(interface, context.args, {})
    assert context.outcome.wait_for_ack_nak is True
    assert context.outcome.skip_ack_wait is False


@pytest.mark.unit
def test_configure_plain_tuple_hook_retains_legacy_ack_behavior() -> None:
    """Downstream hook doubles returning plain tuples keep legacy ACK semantics."""
    interface = _interface()
    hooks = ConfigureActionHooks(
        handle_set_command=MagicMock(),
        handle_configure_command=MagicMock(return_value=(False, False)),
        export_config=MagicMock(),
        cli_exit=cast(CliExit, _cli_exit),
        cli_print=MagicMock(),
        is_local_destination=MagicMock(return_value=True),
    )
    context = CliContext(
        interface=interface,
        args=argparse.Namespace(
            set=None, configure=["config.yaml"], export_config=None, dest="^local"
        ),
        get_node_kwargs={},
        outcome=ActionOutcome(),
    )

    configure_actions._handle_configure_actions(context, hooks)

    assert context.outcome.wait_for_ack_nak is True


@pytest.mark.unit
def test_matching_channel_url_reports_no_request_sent(
    tmp_path: Path,
) -> None:
    """A redundant channel URL must preserve the two-item result without arming ACK."""
    path = tmp_path / "matching-url.yaml"
    path.write_text(
        "channel_url: https://meshtastic.org/e/#matching\n", encoding="utf-8"
    )
    interface = _interface()
    node = MagicMock()
    interface.getNode.return_value = node
    hooks = _hooks(
        channel_url_matches_current_device_state=MagicMock(return_value=True)
    )
    args = argparse.Namespace(configure=[str(path)], dest="^local")

    result = configure_actions._handle_configure_command(hooks, interface, args, {})

    assert result == (False, False)
    assert result.request_sent is False
    assert result.settings_transaction_started is False
    assert result.local_channel_url_applied is False
    node.setURL.assert_not_called()


@pytest.mark.unit
def test_configure_command_rejects_multiple_documents_before_device_access() -> None:
    """Repeated --configure options must not silently ignore later documents."""
    interface = MagicMock()
    cli_exit = MagicMock(side_effect=SystemExit(1))
    args = argparse.Namespace(
        configure=["first.yaml", "second.yaml"],
        dest="^local",
    )

    with pytest.raises(SystemExit):
        configure_actions._handle_configure_command(
            _hooks(cli_exit=cast(CliExit, cli_exit)),
            interface,
            args,
            {},
        )

    assert "only once" in str(cli_exit.call_args)
    interface.getNode.assert_not_called()


@pytest.mark.unit
def test_prepare_configure_execution_classifies_local_channel_url_only(
    tmp_path: Path,
) -> None:
    """A local channel-URL-only document should not require a settings transaction."""
    path = tmp_path / "local-url.yaml"
    path.write_text("channel_url: https://meshtastic.org/e/#local\n", encoding="utf-8")
    interface = _interface()
    hooks = _hooks(is_local_destination=MagicMock(return_value=True))
    args = argparse.Namespace(configure=[str(path)], dest="^local")

    plan = configure_actions._prepare_configure_execution(hooks, interface, args)

    assert plan.destination == "^local"
    assert plan.is_local_target is True
    assert plan.has_config_writes is False
    assert plan.prepared.direct_values.channel_url == "https://meshtastic.org/e/#local"


@pytest.mark.unit
def test_prepare_configure_execution_rejects_remote_mixed_writes_before_node_lookup(
    tmp_path: Path,
) -> None:
    """Unsupported remote setURL/config mixes should fail before target-node access."""
    path = tmp_path / "remote-mixed.yaml"
    path.write_text(
        "channel_url: https://meshtastic.org/e/#remote\n"
        "config:\n"
        "  bluetooth:\n"
        "    enabled: true\n",
        encoding="utf-8",
    )
    interface = _interface()
    cli_exit = MagicMock(side_effect=SystemExit(1))
    hooks = _hooks(
        cli_exit=cast(CliExit, cli_exit),
        is_local_destination=MagicMock(return_value=False),
    )
    args = argparse.Namespace(configure=[str(path)], dest="!87654321")

    with pytest.raises(SystemExit):
        configure_actions._handle_configure_command(hooks, interface, args, {})

    assert "separate operations" in str(cli_exit.call_args)
    interface.getNode.assert_not_called()
