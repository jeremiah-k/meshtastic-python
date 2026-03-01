"""Unit tests for BLE notification subscription management."""

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
