"""Unit tests for powermon stress helpers and client behavior."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from ..powermon.stress import PowerStress, PowerStressClient, onPowerStressResponse
from ..protobuf import powermon_pb2


@pytest.mark.unit
def test_on_power_stress_response_sets_flag() -> None:
    """Test that onPowerStressResponse marks interface.gotResponse as True."""
    iface = SimpleNamespace(gotResponse=False)
    onPowerStressResponse({"decoded": {}}, iface)
    assert iface.gotResponse is True


@pytest.mark.unit
def test_power_stress_client_defaults_node_id_from_iface() -> None:
    """PowerStressClient should use local node id when node_id is not provided."""
    iface = SimpleNamespace(myInfo=SimpleNamespace(my_node_num=12345))
    client = PowerStressClient(iface)
    assert client.node_id == 12345


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
    assert kwargs["wantAck"] is True
    assert kwargs["wantResponse"] is True
    assert kwargs["onResponseAckPermitted"] is True


@pytest.mark.unit
def test_sync_power_stress_wait_until_ack_success() -> None:
    """Test that syncPowerStress returns True when ack fires in run-until-ack mode."""
    iface = MagicMock()
    iface.myInfo.my_node_num = 1
    client = PowerStressClient(iface)

    def _fake_send(cmd: int, num_seconds: float = 0.0, onResponse=None):
        _ = cmd, num_seconds
        assert onResponse is not None
        onResponse({"decoded": {}})

    client.sendPowerStress = _fake_send  # type: ignore[method-assign]
    assert client.syncPowerStress(powermon_pb2.PowerStressMessage.BT_ON) is True


@pytest.mark.unit
def test_sync_power_stress_wait_until_ack_timeout() -> None:
    """Test that syncPowerStress returns False when ack is not received in time."""
    iface = MagicMock()
    iface.myInfo.my_node_num = 1
    client = PowerStressClient(iface)

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
def test_sync_power_stress_timed_mode_without_ack(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Test that syncPowerStress fails timed mode when ack callback never fires."""
    iface = MagicMock()
    iface.myInfo.my_node_num = 1
    client = PowerStressClient(iface)
    monkeypatch.setattr("meshtastic.powermon.stress.time.sleep", lambda _: None)

    client.sendPowerStress = MagicMock(return_value=None)  # type: ignore[method-assign]
    assert (
        client.syncPowerStress(
            powermon_pb2.PowerStressMessage.LED_ON,
            num_seconds=1.0,
        )
        is False
    )


@pytest.mark.unit
def test_sync_power_stress_timed_mode_with_ack(monkeypatch: pytest.MonkeyPatch) -> None:
    """Test that syncPowerStress succeeds timed mode when ack callback fires."""
    iface = MagicMock()
    iface.myInfo.my_node_num = 1
    client = PowerStressClient(iface)
    monkeypatch.setattr("meshtastic.powermon.stress.time.sleep", lambda _: None)

    def _fake_send(cmd: int, num_seconds: float = 0.0, onResponse=None):
        _ = cmd, num_seconds
        assert onResponse is not None
        onResponse({"decoded": {}})

    client.sendPowerStress = _fake_send  # type: ignore[method-assign]
    assert (
        client.syncPowerStress(
            powermon_pb2.PowerStressMessage.LED_ON,
            num_seconds=1.0,
        )
        is True
    )


@pytest.mark.unit
def test_power_stress_run_aborts_when_print_info_ack_missing() -> None:
    """PowerStress.run should continue on PRINT_INFO failure but abort on first state failure."""
    iface = MagicMock()
    ps = PowerStress(iface)
    ps.client = MagicMock()
    ps.client.syncPowerStress.return_value = False

    ps.run()

    # PRINT_INFO failure now logs warning and continues, then aborts on LED_ON
    assert ps.client.syncPowerStress.call_count == 2
    first_cmd = ps.client.syncPowerStress.call_args_list[0].args[0]
    second_cmd = ps.client.syncPowerStress.call_args_list[1].args[0]
    assert first_cmd == powermon_pb2.PowerStressMessage.PRINT_INFO
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
    first_cmd = ps.client.syncPowerStress.call_args_list[0].args[0]
    second_cmd = ps.client.syncPowerStress.call_args_list[1].args[0]
    assert first_cmd == powermon_pb2.PowerStressMessage.PRINT_INFO
    assert second_cmd == powermon_pb2.PowerStressMessage.LED_ON


@pytest.mark.unit
def test_power_stress_run_completes_all_states() -> None:
    """PowerStress.run should execute all configured states when all commands are acknowledged."""
    iface = MagicMock()
    ps = PowerStress(iface)
    ps.client = MagicMock()
    ps.client.syncPowerStress.return_value = True

    ps.run()

    # PRINT_INFO + 6 test states
    assert ps.client.syncPowerStress.call_count == 7
