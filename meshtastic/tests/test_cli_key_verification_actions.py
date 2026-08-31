"""CLI orchestration and rendering tests for firmware key verification."""

from typing import cast
from unittest.mock import MagicMock

import pytest

import meshtastic.cli.device_actions as device_actions
from meshtastic.protobuf import admin_pb2, mesh_pb2
from meshtastic.tests.cli_device_action_test_helpers import (
    device_action_context as _context,
)
from meshtastic.tests.cli_device_action_test_helpers import (
    device_action_hooks as _hooks,
)


def _interface() -> MagicMock:
    """Build a permissive interface double for key-verification CLI orchestration."""
    interface = MagicMock()
    interface.myInfo = MagicMock(my_node_num=1)
    return interface


def _args(stage: str | None, **overrides: object) -> dict[str, object]:
    """Build key-verification CLI argument defaults for one stage.

    Parameters
    ----------
    stage : str | None
        Value for ``--key-verify``, or ``None`` to leave the flag unset.
    **overrides : object
        Argument values that replace the focused-test defaults.

    Returns
    -------
    dict[str, object]
        Argument overrides consumed by ``device_action_context``.
    """
    values: dict[str, object] = {
        "key_verify": stage,
        "key_verify_nonce": 0,
        "key_verify_security_number": None,
        "key_verify_wait": 60.0,
        "dest": "!00000007",
    }
    values.update(overrides)
    return values


@pytest.mark.unit
def test_key_verification_action_noop_without_stage() -> None:
    """No key-verification flag leaves the connected outcome untouched."""
    interface = _interface()
    context = _context(interface, _args(None))
    hooks = _hooks()

    device_actions._handle_key_verification_action(context, hooks)

    assert context.outcome.close_now is False
    assert context.outcome.stop_processing is False
    build_admin = cast(MagicMock, hooks.build_key_verification_admin)
    build_admin.assert_not_called()


@pytest.mark.unit
def test_key_verification_initiate_resolves_peer_sends_and_reports() -> None:
    """Initiation resolves --dest, builds the request, waits, and prints guidance."""
    interface = _interface()
    interface.myInfo.my_node_num = 1
    context = _context(interface, _args("initiate", key_verify_wait=2.5))
    prints: list[str] = []
    request = admin_pb2.KeyVerificationAdmin(
        message_type=admin_pb2.KeyVerificationAdmin.INITIATE_VERIFICATION,
        remote_nodenum=7,
    )
    build_admin = MagicMock(return_value=request)
    notification = mesh_pb2.ClientNotification()
    notification.key_verification_number_request.remote_longname = "Repeater"
    notification.key_verification_number_request.nonce = 91
    send_notification = MagicMock(return_value=notification)
    hooks = _hooks(
        prints,
        build_key_verification_admin=build_admin,
        send_key_verification=send_notification,
    )

    device_actions._handle_key_verification_action(context, hooks)

    build_admin.assert_called_once_with(
        "initiate", remote_nodenum=7, nonce=0, security_number=None
    )
    send_notification.assert_called_once_with(interface, request, timeout=2.5)
    assert context.outcome.close_now is True
    assert context.outcome.stop_processing is True
    assert any("Repeater requests the security number" in line for line in prints)
    assert any("--key-verify-nonce 91" in line for line in prints)


@pytest.mark.unit
@pytest.mark.parametrize("dest", [None, "^all", "^local"])
def test_key_verification_initiate_rejects_non_peer_destinations(
    dest: str | None,
) -> None:
    """Initiation requires one actual remote peer rather than local/broadcast aliases."""
    interface = _interface()
    exits: list[tuple[str, int]] = []
    with pytest.raises(SystemExit):
        device_actions._handle_key_verification_action(
            _context(interface, _args("initiate", dest=dest)), _hooks(exits=exits)
        )
    assert exits and exits[0][1] == 1


@pytest.mark.unit
def test_key_verification_initiate_rejects_local_numeric_peer() -> None:
    """A numeric destination equal to the attached node cannot verify itself."""
    interface = _interface()
    interface.myInfo.my_node_num = 7
    exits: list[tuple[str, int]] = []
    with pytest.raises(SystemExit):
        device_actions._handle_key_verification_action(
            _context(interface, _args("initiate")), _hooks(exits=exits)
        )
    assert "remote peer" in exits[0][0]


@pytest.mark.unit
def test_key_verification_reports_build_and_send_failures() -> None:
    """Validation and runtime errors use the stable CLI termination surface."""
    interface = _interface()
    exits: list[tuple[str, int]] = []
    build_admin = MagicMock(side_effect=ValueError("bad nonce"))
    hooks = _hooks(exits=exits, build_key_verification_admin=build_admin)
    with pytest.raises(SystemExit):
        device_actions._handle_key_verification_action(
            _context(interface, _args("verify", key_verify_nonce=-1)), hooks
        )
    assert exits[-1] == ("Invalid key-verification options: bad nonce", 1)

    exits.clear()
    send_notification = MagicMock(side_effect=TimeoutError("no notification"))
    hooks = _hooks(exits=exits, send_key_verification=send_notification)
    with pytest.raises(SystemExit):
        device_actions._handle_key_verification_action(
            _context(interface, _args("verify", key_verify_nonce=5)), hooks
        )
    assert exits[-1] == ("Key verification failed: no notification", 1)


@pytest.mark.unit
def test_key_verification_reporter_covers_progress_and_decision_variants() -> None:
    """Every firmware notification variant renders actionable operator guidance."""
    prints: list[str] = []
    hooks = _hooks(prints)

    device_actions._report_key_verification_notification(None, "verify", hooks)
    device_actions._report_key_verification_notification(None, "no-verify", hooks)
    device_actions._report_key_verification_notification(None, "provide", hooks)

    inform = mesh_pb2.ClientNotification()
    inform.key_verification_number_inform.remote_longname = "Responder"
    inform.key_verification_number_inform.security_number = 1234
    inform.key_verification_number_inform.nonce = 11
    device_actions._report_key_verification_notification(inform, "initiate", hooks)

    final = mesh_pb2.ClientNotification()
    final.key_verification_final.remote_longname = "Responder"
    final.key_verification_final.verification_characters = "AB-CD"
    final.key_verification_final.nonce = 12
    device_actions._report_key_verification_notification(final, "provide", hooks)

    final_no_chars = mesh_pb2.ClientNotification()
    final_no_chars.key_verification_final.remote_longname = "Responder"
    final_no_chars.key_verification_final.nonce = 13
    device_actions._report_key_verification_notification(
        final_no_chars, "provide", hooks
    )

    plain = mesh_pb2.ClientNotification(message="device note")
    device_actions._report_key_verification_notification(plain, "provide", hooks)

    joined = "\n".join(prints)
    assert "decision sent: accepted" in joined
    assert "decision sent: rejected" in joined
    assert "device reported no notification" in joined
    assert "Security number for Responder: 001234" in joined
    assert "Verification characters: AB-CD" in joined
    assert "Key-verification notification: device note" in joined


@pytest.mark.unit
def test_key_verification_initiate_rejects_unparseable_destination() -> None:
    """Invalid node-id syntax terminates cleanly instead of leaking ValueError."""
    exits: list[tuple[str, int]] = []
    with pytest.raises(SystemExit):
        device_actions._handle_key_verification_action(
            _context(_interface(), _args("initiate", dest="not-a-node")),
            _hooks(exits=exits),
        )
    assert "Could not parse --dest" in exits[0][0]


@pytest.mark.unit
def test_key_verification_initiate_rejects_numeric_broadcast_destination() -> None:
    """The numeric broadcast address is rejected even without the ^all alias."""
    exits: list[tuple[str, int]] = []
    with pytest.raises(SystemExit):
        device_actions._handle_key_verification_action(
            _context(_interface(), _args("initiate", dest="4294967295")),
            _hooks(exits=exits),
        )
    assert "broadcast address" in exits[0][0]
