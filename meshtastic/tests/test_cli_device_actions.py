"""Focused unit tests for extracted device-action failure boundaries."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

from meshtastic.cli import device_actions
from meshtastic.cli.context import ActionOutcome, CliContext


class _DummyTCPInterface:
    """Minimal TCP-interface stand-in for OTA failure-path tests."""

    hostname = "mesh.local"

    def __init__(self) -> None:
        self.node = MagicMock()

    def getNode(self, *_args: object, **_kwargs: object) -> MagicMock:  # noqa: N802
        """Return the local-node test double expected by the OTA helper."""
        return self.node


def _returning_cli_exit(*_args: object, **_kwargs: object) -> None:
    """Model an injected exit seam that incorrectly returns to its caller."""


def _hooks(**overrides: object) -> device_actions.DeviceActionHooks:
    """Build device-action hooks with a deliberately returning exit seam."""
    values: dict[str, object] = {
        "cli_exit": _returning_cli_exit,
        "cli_print": MagicMock(),
        "set_pref": MagicMock(return_value=True),
        "is_local_destination": MagicMock(return_value=True),
        "send_local_factory_reset_and_wait": MagicMock(),
        "post_factory_reset_ready_probe": MagicMock(),
        "handle_ota_update": MagicMock(),
        "build_lockdown_auth": MagicMock(return_value=object()),
        "read_lockdown_passphrase_file": MagicMock(return_value=b"secret"),
        "send_lockdown_auth": MagicMock(return_value=None),
        "validate_lockdown_passphrase": MagicMock(return_value=b"secret"),
    }
    values.update(overrides)
    return device_actions.DeviceActionHooks(**values)  # type: ignore[arg-type]


def _context(interface: object, **args: object) -> CliContext:
    """Build the minimal connected CLI context needed by a focused handler test."""
    return CliContext(
        interface=interface,  # type: ignore[arg-type]
        args=SimpleNamespace(**args),
        get_node_kwargs={},
        outcome=ActionOutcome(),
    )


def _lockdown_context() -> CliContext:
    """Build a local unlock request that avoids interactive confirmation."""
    return _context(
        MagicMock(),
        lockdown_provision=False,
        lockdown_unlock=True,
        lockdown_lock_now=False,
        lockdown_disable=False,
        lockdown_yes=False,
        lockdown_passphrase_file="secret.txt",
        lockdown_passphrase=None,
        insecure_lockdown_passphrase_on_command_line=False,
        lockdown_boots=None,
        lockdown_valid_until=None,
        lockdown_max_session_seconds=None,
        lockdown_wait=1.0,
        dest="^local",
    )


def _prepare_ota(
    monkeypatch: pytest.MonkeyPatch, ota_factory: Any
) -> _DummyTCPInterface:
    """Install OTA seams and return a compatible TCP-interface test double."""
    monkeypatch.setattr(
        device_actions.meshtastic.tcp_interface, "TCPInterface", _DummyTCPInterface
    )
    monkeypatch.setattr(device_actions.meshtastic.ota, "ESP32WiFiOTA", ota_factory)
    monkeypatch.setattr(device_actions.time, "sleep", lambda _seconds: None)
    return _DummyTCPInterface()


def _run_ota(interface: _DummyTCPInterface) -> None:
    """Invoke the OTA helper with the returning-exit test seam."""
    device_actions.handle_ota_update(
        interface,  # type: ignore[arg-type]
        SimpleNamespace(ota_update="firmware.bin", dest="^local"),
        {},
        cli_exit=_returning_cli_exit,  # type: ignore[arg-type]
        cli_print=MagicMock(),
        is_local_destination=lambda *_args: True,
    )


@pytest.mark.unit
@pytest.mark.parametrize("raw_altitude", ["not-an-altitude", str(1 << 31)])
def test_altitude_guards_reject_returning_cli_exit(raw_altitude: str) -> None:
    """Invalid altitude input must not continue into position assignment."""
    context = _context(
        MagicMock(),
        set_time=None,
        remove_position=False,
        setlat=None,
        setlon=None,
        setalt=raw_altitude,
        dest="^local",
    )

    with pytest.raises(AssertionError, match="cli_exit returned unexpectedly"):
        device_actions._handle_device_actions(context, _hooks())


@pytest.mark.unit
def test_invalid_position_fields_guard_rejects_returning_cli_exit() -> None:
    """Unknown position fields must not fall through after the exit seam returns."""
    interface = MagicMock()
    position = interface.getNode.return_value.localConfig.position
    position.PositionFlags.Value.side_effect = ValueError("unknown field")
    context = _context(interface, pos_fields=["bogus"], dest="^local")

    with pytest.raises(AssertionError, match="cli_exit returned unexpectedly"):
        device_actions._handle_position_fields(context, _hooks())


@pytest.mark.unit
def test_ota_constructor_guard_rejects_returning_cli_exit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """OTA setup failure must not continue with an unbound OTA client."""

    def _raise_ota_error(*_args: object, **_kwargs: object) -> None:
        raise device_actions.meshtastic.ota.OTAError("invalid image")

    interface = _prepare_ota(monkeypatch, _raise_ota_error)

    with pytest.raises(AssertionError, match="cli_exit returned unexpectedly"):
        _run_ota(interface)


@pytest.mark.unit
@pytest.mark.parametrize(
    "failure",
    [
        device_actions.meshtastic.ota.OTAError("invalid"),
        device_actions.meshtastic.ota.OTATransportError("lost"),
    ],
)
def test_ota_update_guards_reject_returning_cli_exit(
    monkeypatch: pytest.MonkeyPatch, failure: Exception
) -> None:
    """OTA update failures must not fall through to the success path."""
    ota = MagicMock()
    ota.hash_bytes.return_value = b"hash"
    ota.update.side_effect = failure
    interface = _prepare_ota(monkeypatch, MagicMock(return_value=ota))

    with pytest.raises(AssertionError, match="cli_exit returned unexpectedly"):
        _run_ota(interface)

    expected_calls = (
        device_actions.OTA_MAX_RETRIES
        if isinstance(failure, device_actions.meshtastic.ota.OTATransportError)
        else 1
    )
    assert ota.update.call_count == expected_calls


@pytest.mark.unit
@pytest.mark.parametrize(
    ("hook_name", "failure"),
    [
        ("build_lockdown_auth", ValueError("invalid")),
        ("send_lockdown_auth", RuntimeError("lost")),
    ],
)
def test_lockdown_action_guards_reject_returning_cli_exit(
    hook_name: str, failure: Exception
) -> None:
    """Lockdown failures must not continue with unbound auth or status values."""
    hooks = _hooks(**{hook_name: MagicMock(side_effect=failure)})

    with pytest.raises(AssertionError, match="cli_exit returned unexpectedly"):
        device_actions._handle_lockdown_action(_lockdown_context(), hooks)


@pytest.mark.unit
def test_lockdown_cli_secret_guard_rejects_returning_cli_exit() -> None:
    """A refused command-line secret must not be validated after the exit call."""
    args = SimpleNamespace(
        lockdown_passphrase_file=None,
        lockdown_passphrase="secret",
        insecure_lockdown_passphrase_on_command_line=False,
    )

    with pytest.raises(AssertionError, match="cli_exit returned unexpectedly"):
        device_actions._read_lockdown_passphrase(args, "unlock", _hooks())


@pytest.mark.unit
def test_lockdown_confirmation_mismatch_guard_rejects_returning_cli_exit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Mismatched provisioning secrets must not continue into validation."""
    monkeypatch.setattr(
        device_actions.getpass, "getpass", MagicMock(side_effect=["a", "b"])
    )
    args = SimpleNamespace(
        lockdown_passphrase_file=None,
        lockdown_passphrase=None,
        insecure_lockdown_passphrase_on_command_line=False,
    )

    with pytest.raises(AssertionError, match="cli_exit returned unexpectedly"):
        device_actions._read_lockdown_passphrase(args, "provision", _hooks())
