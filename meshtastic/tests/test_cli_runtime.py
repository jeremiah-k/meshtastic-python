"""Focused tests for CLI serial reconnect helpers."""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import meshtastic.cli.runtime as runtime


@pytest.mark.unit
def test_serial_transport_live_requires_open_stream_and_reader() -> None:
    client = SimpleNamespace(stream=SimpleNamespace(isOpen=True), _rxThread=MagicMock())
    client._rxThread.is_alive.return_value = True
    assert runtime._serial_transport_is_live(client)
    client._rxThread.is_alive.return_value = False
    assert not runtime._serial_transport_is_live(client)


@pytest.mark.unit
def test_serial_should_reconnect_requires_lost_connection() -> None:
    client = SimpleNamespace(isConnected=MagicMock(), stream=None, _rxThread=None)
    client.isConnected.is_set.return_value = False
    assert runtime._serial_should_reconnect(client)
    client.isConnected.is_set.return_value = True
    assert not runtime._serial_should_reconnect(client)


@pytest.mark.unit
def test_listen_loop_poll_once_sleeps_long_when_non_serial(monkeypatch) -> None:
    client = object()
    sleep = MagicMock()
    monkeypatch.setattr(runtime.time, "sleep", sleep)
    assert runtime._listen_loop_poll_once(client) is False
    sleep.assert_called_once_with(runtime.MAIN_LOOP_IDLE_SLEEP_SECONDS)


@pytest.mark.unit
def test_listen_loop_poll_once_reconnects_serial(monkeypatch) -> None:
    client = MagicMock()
    monkeypatch.setattr(runtime, "_is_serial_reconnect_client", lambda _client: True)
    monkeypatch.setattr(runtime, "_serial_should_reconnect", lambda _client: True)
    poll = MagicMock()
    monkeypatch.setattr(runtime, "_poll_serial_reconnect", poll)
    assert runtime._listen_loop_poll_once(client)
    poll.assert_called_once_with(client)
