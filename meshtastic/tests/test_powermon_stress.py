"""Unit tests for powermon stress helpers and client behavior."""

import logging
from collections.abc import Callable
from typing import Any
from unittest.mock import MagicMock, call

import pytest

from ..powermon.stress import (
    DEFAULT_STRESS_STATE_DURATION_S,
    PowerStress,
    PowerStressClient,
    handle_power_stress_response,
    handlePowerStressResponse,
    onPowerStressResponse,
)
from ..protobuf import portnums_pb2, powermon_pb2


def _fake_send(
    cmd: powermon_pb2.PowerStressMessage.Opcode.ValueType,
    num_seconds: float = 0.0,
    onResponse: Callable[[dict[str, Any]], None] | None = None,
) -> None:
    """Shared fake sendPowerStress helper for sync tests."""
    _ = cmd, num_seconds
    assert onResponse is not None
    onResponse({"decoded": {}})


@pytest.mark.unit
def test_handle_power_stress_response_sets_flag() -> None:
    """Test that handlePowerStressResponse marks interface.gotResponse as True."""
    iface = MagicMock()
    iface.gotResponse = False
    handlePowerStressResponse({"decoded": {}}, iface)
    assert iface.gotResponse is True


@pytest.mark.unit
def test_on_power_stress_response_alias_sets_flag() -> None:
    """Compatibility alias should preserve callback behavior."""
    iface = MagicMock()
    iface.gotResponse = False
    onPowerStressResponse({"decoded": {}}, iface)
    assert iface.gotResponse is True


@pytest.mark.unit
def test_handle_power_stress_response_alias_sets_flag() -> None:
    """snake_case compatibility alias should preserve callback behavior."""
    iface = MagicMock()
    iface.gotResponse = False
    handle_power_stress_response({"decoded": {}}, iface)
    assert iface.gotResponse is True


@pytest.mark.unit
def test_power_stress_client_defaults_node_id_from_iface() -> None:
    """PowerStressClient should use local node id when node_id is not provided."""
    iface = MagicMock()
    iface.myInfo.my_node_num = 12345
    client = PowerStressClient(iface)
    assert client.node_id == 12345


@pytest.mark.unit
def test_power_stress_client_raises_when_iface_not_initialized() -> None:
    """PowerStressClient should fail fast when myInfo.my_node_num is unavailable."""
    iface = MagicMock()
    iface.myInfo = None
    with pytest.raises(ValueError, match="myInfo.my_node_num unavailable"):
        PowerStressClient(iface)


@pytest.mark.unit
def test_send_power_stress_calls_send_data_with_expected_args() -> None:
    """Test that sendPowerStress sends a POWERSTRESS_APP message with ack/response."""
    iface = MagicMock()
    iface.myInfo.my_node_num = 123
    client = PowerStressClient(iface)

    client.sendPowerStress(powermon_pb2.PowerStressMessage.CPU_IDLE, num_seconds=2.5)

    iface.sendData.assert_called_once()
    args, kwargs = iface.sendData.call_args
    msg = args[0]
    assert msg.cmd == powermon_pb2.PowerStressMessage.CPU_IDLE
    assert msg.num_seconds == pytest.approx(2.5)
    assert args[1] == 123
    assert args[2] == portnums_pb2.POWERSTRESS_APP
    assert kwargs["wantAck"] is True
    assert kwargs["wantResponse"] is True
    assert kwargs["onResponseAckPermitted"] is True


@pytest.mark.unit
def test_sync_power_stress_wait_until_ack_success(
    power_stress_client: tuple[MagicMock, PowerStressClient],
) -> None:
    """Test that syncPowerStress returns True when ack fires in run-until-ack mode."""
    _, client = power_stress_client

    client.sendPowerStress = _fake_send  # type: ignore[method-assign]
    assert client.syncPowerStress(powermon_pb2.PowerStressMessage.BT_ON) is True


@pytest.mark.unit
def test_sync_power_stress_wait_until_ack_timeout(
    power_stress_client: tuple[MagicMock, PowerStressClient],
) -> None:
    """Test that syncPowerStress returns False when ack is not received in time."""
    _, client = power_stress_client

    client.sendPowerStress = MagicMock(return_value=None)  # type: ignore[method-assign]
    assert (
        client.syncPowerStress(
            powermon_pb2.PowerStressMessage.BT_ON,
            num_seconds=0.0,
            ack_timeout=0.01,
        )
        is False
    )


@pytest.mark.unit
def test_sync_power_stress_negative_duration_uses_ack_wait_path(
    caplog: pytest.LogCaptureFixture,
    power_stress_client: tuple[MagicMock, PowerStressClient],
) -> None:
    """Negative durations should be handled via the run-until-ack timeout path."""
    _, client = power_stress_client
    client.sendPowerStress = MagicMock(return_value=None)  # type: ignore[method-assign]

    with caplog.at_level(logging.WARNING):
        assert (
            client.syncPowerStress(
                powermon_pb2.PowerStressMessage.BT_ON,
                num_seconds=-1.0,
                ack_timeout=0.01,
            )
            is False
        )

    client.sendPowerStress.assert_called_once()
    sent_args, sent_kwargs = client.sendPowerStress.call_args
    assert sent_args[0] == powermon_pb2.PowerStressMessage.BT_ON
    assert sent_kwargs["num_seconds"] == 0.0
    assert "Negative num_seconds" in caplog.text


@pytest.mark.unit
def test_sync_power_stress_timed_mode_without_ack(
    monkeypatch: pytest.MonkeyPatch,
    power_stress_client: tuple[MagicMock, PowerStressClient],
) -> None:
    """Test that syncPowerStress fails timed mode when ack callback never fires."""
    _, client = power_stress_client
    monkeypatch.setattr("meshtastic.powermon.stress.STRESS_DURATION_BUFFER_S", 0.0)

    client.sendPowerStress = MagicMock(return_value=None)  # type: ignore[method-assign]
    assert (
        client.syncPowerStress(
            powermon_pb2.PowerStressMessage.LED_ON,
            num_seconds=0.01,
        )
        is False
    )


@pytest.mark.unit
def test_sync_power_stress_timed_mode_with_ack(
    power_stress_client: tuple[MagicMock, PowerStressClient],
) -> None:
    """Test that syncPowerStress succeeds timed mode when ack callback fires."""
    _, client = power_stress_client

    client.sendPowerStress = _fake_send  # type: ignore[method-assign]
    assert (
        client.syncPowerStress(
            powermon_pb2.PowerStressMessage.LED_ON,
            num_seconds=1.0,
        )
        is True
    )


@pytest.mark.unit
def test_sync_power_stress_timed_mode_with_ack_enforces_minimum_dwell(
    monkeypatch: pytest.MonkeyPatch,
    power_stress_client: tuple[MagicMock, PowerStressClient],
) -> None:
    """Timed mode should preserve minimum dwell budget even when ack arrives quickly."""
    _, client = power_stress_client
    client.sendPowerStress = _fake_send  # type: ignore[method-assign]
    monkeypatch.setattr("meshtastic.powermon.stress.STRESS_DURATION_BUFFER_S", 0.2)
    monotonic = MagicMock(side_effect=[10.0, 10.05])
    sleep = MagicMock()
    monkeypatch.setattr("meshtastic.powermon.stress.time.monotonic", monotonic)
    monkeypatch.setattr("meshtastic.powermon.stress.time.sleep", sleep)

    assert (
        client.syncPowerStress(
            powermon_pb2.PowerStressMessage.LED_ON,
            num_seconds=1.0,
        )
        is True
    )
    sleep.assert_called_once_with(pytest.approx(1.15))


@pytest.mark.unit
def test_power_stress_run_continues_after_print_info_failure_then_aborts_on_first_state_failure() -> (
    None
):
    """PowerStress.run should continue after PRINT_INFO failure and abort on the first state failure."""
    iface = MagicMock()
    ps = PowerStress(iface)
    ps.client = MagicMock()
    ps.client.syncPowerStress.return_value = False

    ps.run()

    # PRINT_INFO failure now logs warning and continues, then aborts on LED_ON
    assert ps.client.syncPowerStress.call_count == 2
    first_call = ps.client.syncPowerStress.call_args_list[0]
    first_cmd = first_call.args[0]
    second_cmd = ps.client.syncPowerStress.call_args_list[1].args[0]
    assert first_cmd == powermon_pb2.PowerStressMessage.PRINT_INFO
    assert first_call.kwargs == {"ack_timeout": DEFAULT_STRESS_STATE_DURATION_S}
    assert second_cmd == powermon_pb2.PowerStressMessage.LED_ON


@pytest.mark.unit
def test_power_stress_run_stops_on_first_failed_state() -> None:
    """PowerStress.run should stop when any state command fails to receive ack."""
    iface = MagicMock()
    ps = PowerStress(iface)
    ps.client = MagicMock()
    # First call (PRINT_INFO) succeeds, first state fails.
    ps.client.syncPowerStress.side_effect = [True, False]

    ps.run()

    assert ps.client.syncPowerStress.call_count == 2
    first_call = ps.client.syncPowerStress.call_args_list[0]
    first_cmd = first_call.args[0]
    second_cmd = ps.client.syncPowerStress.call_args_list[1].args[0]
    assert first_cmd == powermon_pb2.PowerStressMessage.PRINT_INFO
    assert first_call.kwargs == {"ack_timeout": DEFAULT_STRESS_STATE_DURATION_S}
    assert second_cmd == powermon_pb2.PowerStressMessage.LED_ON


@pytest.mark.unit
def test_power_stress_run_completes_all_states() -> None:
    """PowerStress.run should execute all configured states when all commands are acknowledged."""
    iface = MagicMock()
    ps = PowerStress(iface)
    ps.client = MagicMock()
    ps.client.syncPowerStress.return_value = True

    ps.run()

    expected_call_count = 1 + len(ps.states)
    assert ps.client.syncPowerStress.call_count == expected_call_count
    expected_calls = [
        call(
            powermon_pb2.PowerStressMessage.PRINT_INFO,
            ack_timeout=DEFAULT_STRESS_STATE_DURATION_S,
        )
    ] + [call(state, DEFAULT_STRESS_STATE_DURATION_S) for state in ps.states]
    ps.client.syncPowerStress.assert_has_calls(expected_calls, any_order=False)
