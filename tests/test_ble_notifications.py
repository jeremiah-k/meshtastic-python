"""Unit tests for BLE notification subscription management."""

from typing import Any

import pytest

from meshtastic.interfaces.ble.notifications import (
    NotificationManager,
    SubscriptionTokenExhaustedError,
)


@pytest.mark.unit
def test_subscribe_raises_domain_error_when_token_space_exhausted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Token wrap exhaustion should raise SubscriptionTokenExhaustedError."""
    manager = NotificationManager()
    monkeypatch.setattr(manager, "_MAX_SUBSCRIPTION_TOKEN", 1)
    manager._active_subscriptions = {
        0: ("first", lambda _sender, _data: None),
        1: ("second", lambda _sender, _data: None),
    }
    manager._subscription_counter = 0

    with pytest.raises(SubscriptionTokenExhaustedError):
        manager._subscribe("third", lambda _sender, _data: None)


@pytest.mark.unit
def test_resubscribe_all_stops_after_cleanup_epoch_change() -> None:
    """Resubscribe loop should abort stale iterations after cleanup invalidates state."""
    manager = NotificationManager()
    manager._subscribe("char-1", lambda _sender, _data: None)
    manager._subscribe("char-2", lambda _sender, _data: None)

    class _Client:
        """Stub BLE client that triggers cleanup on first characteristic."""

        def __init__(self) -> None:
            self.calls: list[str] = []

        def start_notify(
            self,
            characteristic: str,
            _callback: Any,
            *,
            timeout: float | None = None,
        ) -> None:
            _ = timeout
            self.calls.append(characteristic)
            if characteristic == "char-1":
                manager._cleanup_all()

    client = _Client()
    manager._resubscribe_all(client, timeout=1.0)

    assert client.calls == ["char-1"]
    assert len(manager) == 0
    assert manager._get_callback("char-1") is None
