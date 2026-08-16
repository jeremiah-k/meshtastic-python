"""Pure BLE lifecycle decision tests."""

from types import SimpleNamespace
from typing import cast

import pytest

from meshtastic.interfaces.ble.client import BLEClient
from meshtastic.interfaces.ble.lifecycle_decisions import (
    _DisconnectDisposition,
    _DisconnectOwnershipSnapshot,
    _ReceiveStartDisposition,
    _ReceiveStartSnapshot,
    _ReceiveThreadProbe,
    _decide_disconnect_ownership,
    _decide_receive_start,
)
from meshtastic.interfaces.ble.state import ConnectionState

pytestmark = pytest.mark.unit


def _receive_probe(
    *,
    ident: int | None = None,
    alive: bool = False,
    current: bool = False,
    failed: bool = False,
) -> _ReceiveThreadProbe:
    """Build one receive-thread probe for decision tests."""
    return _ReceiveThreadProbe(
        ident=ident,
        is_alive=alive,
        is_current=current,
        start_failure_confirmed=failed,
        display_name="receive-test",
    )


@pytest.mark.parametrize(
    ("snapshot", "probe", "expected"),
    [
        (
            _ReceiveStartSnapshot(None, False, None),
            _receive_probe(),
            _ReceiveStartDisposition.PROCEED,
        ),
        (
            _ReceiveStartSnapshot(cast(object, SimpleNamespace()), False, None),
            _receive_probe(alive=True),
            _ReceiveStartDisposition.SKIP_RUNNING,
        ),
        (
            _ReceiveStartSnapshot(cast(object, SimpleNamespace()), True, 9.5),
            _receive_probe(ident=17),
            _ReceiveStartDisposition.REPLACE_STALE_PENDING,
        ),
        (
            _ReceiveStartSnapshot(cast(object, SimpleNamespace()), False, None),
            _receive_probe(ident=17),
            _ReceiveStartDisposition.REPLACE_DEAD,
        ),
        (
            _ReceiveStartSnapshot(cast(object, SimpleNamespace()), False, None),
            _receive_probe(),
            _ReceiveStartDisposition.WAIT_INCONCLUSIVE,
        ),
        (
            _ReceiveStartSnapshot(cast(object, SimpleNamespace()), True, 9.5),
            _receive_probe(),
            _ReceiveStartDisposition.WAIT_PENDING,
        ),
        (
            _ReceiveStartSnapshot(cast(object, SimpleNamespace()), True, 8.0),
            _receive_probe(),
            _ReceiveStartDisposition.REPLACE_STALE_PENDING_TIMEOUT,
        ),
        (
            _ReceiveStartSnapshot(cast(object, SimpleNamespace()), False, None),
            _receive_probe(failed=True),
            _ReceiveStartDisposition.CLEAR_FAILED_START,
        ),
    ],
)
def test_decide_receive_start_classifies_stable_snapshot(
    snapshot: _ReceiveStartSnapshot,
    probe: _ReceiveThreadProbe,
    expected: _ReceiveStartDisposition,
) -> None:
    """Stable receive snapshots should map to one explicit lifecycle decision."""
    decision = _decide_receive_start(
        snapshot,
        probe,
        now=10.0,
        pending_timeout=1.0,
    )

    assert decision.disposition is expected


def test_decide_receive_start_current_thread_without_timeout_defers_restart() -> None:
    """A current receive thread should defer replacement and initialize pending time."""
    existing = cast(object, SimpleNamespace())
    decision = _decide_receive_start(
        _ReceiveStartSnapshot(existing, False, None),
        _receive_probe(current=True),
        now=10.0,
        pending_timeout=1.0,
    )

    assert decision.disposition is _ReceiveStartDisposition.DEFER_CURRENT
    assert decision.initialize_pending_since is True
    assert decision.schedule_deferred_restart is True
    assert decision.pending_age == 0.0


def test_decide_receive_start_current_thread_timeout_requests_deferred_restart() -> None:
    """A current receive thread stuck pending past the timeout should defer replacement."""
    existing = cast(object, SimpleNamespace())
    decision = _decide_receive_start(
        _ReceiveStartSnapshot(existing, True, 8.0),
        _receive_probe(current=True),
        now=10.0,
        pending_timeout=1.0,
    )

    assert decision.disposition is _ReceiveStartDisposition.DEFER_CURRENT_TIMEOUT
    assert decision.pending_age == 2.0


def _client_with_bleak(bleak_client: object) -> BLEClient:
    """Return a minimal typed client double with one bleak-client identity."""
    return cast(BLEClient, SimpleNamespace(bleak_client=bleak_client))


def test_decide_disconnect_ownership_resolves_bleak_callback_to_owned_client() -> None:
    """A bleak callback matching the owned transport should resolve to its BLEClient."""
    bleak_client = SimpleNamespace()
    current_client = _client_with_bleak(bleak_client)
    decision = _decide_disconnect_ownership(
        _DisconnectOwnershipSnapshot(
            current_state=ConnectionState.CONNECTED,
            current_client=current_client,
            target_client=None,
            bleak_client=cast(object, bleak_client),
            is_closing=False,
            publish_pending=False,
            replacement_pending=False,
            disconnect_notified=False,
        )
    )

    assert decision.disposition is _DisconnectDisposition.ACCEPT
    assert decision.target_client is current_client


def test_decide_disconnect_ownership_rejects_stale_client() -> None:
    """A disconnect for a non-owned client must not claim the current session."""
    current_client = _client_with_bleak(SimpleNamespace())
    stale_client = _client_with_bleak(SimpleNamespace())
    decision = _decide_disconnect_ownership(
        _DisconnectOwnershipSnapshot(
            current_state=ConnectionState.CONNECTED,
            current_client=current_client,
            target_client=stale_client,
            bleak_client=None,
            is_closing=False,
            publish_pending=False,
            replacement_pending=False,
            disconnect_notified=False,
        )
    )

    assert decision.disposition is _DisconnectDisposition.IGNORE_STALE


def test_decide_disconnect_ownership_rejects_unowned_connecting_signal() -> None:
    """A non-owned disconnect must not interrupt an in-progress connection."""
    decision = _decide_disconnect_ownership(
        _DisconnectOwnershipSnapshot(
            current_state=ConnectionState.CONNECTING,
            current_client=None,
            target_client=None,
            bleak_client=None,
            is_closing=False,
            publish_pending=True,
            replacement_pending=False,
            disconnect_notified=False,
        )
    )

    assert decision.disposition is _DisconnectDisposition.IGNORE_CONNECTING


def test_decide_disconnect_ownership_rejects_unowned_idle_signal() -> None:
    """A disconnect without any owned client or provisional claim is stale."""
    decision = _decide_disconnect_ownership(
        _DisconnectOwnershipSnapshot(
            current_state=ConnectionState.CONNECTED,
            current_client=None,
            target_client=None,
            bleak_client=None,
            is_closing=False,
            publish_pending=False,
            replacement_pending=False,
            disconnect_notified=False,
        )
    )

    assert decision.disposition is _DisconnectDisposition.IGNORE_UNOWNED


def test_decide_disconnect_ownership_rejects_duplicate_provisional_signal() -> None:
    """A previously-notified provisional session should classify as duplicate."""
    decision = _decide_disconnect_ownership(
        _DisconnectOwnershipSnapshot(
            current_state=ConnectionState.CONNECTED,
            current_client=None,
            target_client=None,
            bleak_client=None,
            is_closing=False,
            publish_pending=True,
            replacement_pending=False,
            disconnect_notified=True,
        )
    )

    assert decision.disposition is _DisconnectDisposition.IGNORE_DUPLICATE


def test_decide_disconnect_ownership_preserves_shutdown_semantics() -> None:
    """Shutdown disconnects remain a non-reconnect early return rather than stale noise."""
    decision = _decide_disconnect_ownership(
        _DisconnectOwnershipSnapshot(
            current_state=ConnectionState.CONNECTED,
            current_client=None,
            target_client=None,
            bleak_client=None,
            is_closing=True,
            publish_pending=True,
            replacement_pending=False,
            disconnect_notified=False,
        )
    )

    assert decision.disposition is _DisconnectDisposition.IGNORE_SHUTDOWN
