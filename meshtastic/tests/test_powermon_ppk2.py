"""Unit tests for PPK2 power-meter measurement state behavior."""

from unittest.mock import MagicMock

import pytest

from ..powermon import power_supply as power_supply_module

try:
    from ..powermon.ppk2 import PPK2PowerSupply
except ImportError:
    pytest.skip("Can't import PPK2PowerSupply", allow_module_level=True)


@pytest.mark.unit
def test_reset_measurements_preserves_last_reported_extrema(
    ppk2_stub: "PPK2PowerSupply",
) -> None:
    """reset_measurements() should clear accumulators while preserving last reported min/max."""
    ppk = ppk2_stub
    ppk.current_sum = 100_000
    ppk.current_num_samples = 8
    ppk.current_min = 2_100
    ppk.current_max = 9_400
    ppk.num_data_reads = 5
    ppk.total_data_len = 1_024
    ppk.max_data_len = 512

    ppk.reset_measurements()

    assert ppk.current_sum == 0
    assert ppk.current_num_samples == 0
    assert ppk.num_data_reads == 0
    assert ppk.total_data_len == 0
    assert ppk.max_data_len == 0
    assert ppk.get_min_current_mA() == pytest.approx(2.1)
    assert ppk.get_max_current_mA() == pytest.approx(9.4)


@pytest.mark.unit
def test_get_min_max_update_only_when_new_samples_exist(
    ppk2_stub: "PPK2PowerSupply",
) -> None:
    """Min/max getters should retain prior values until a new sample window exists."""
    ppk = ppk2_stub

    ppk.current_num_samples = 3
    ppk.current_min = 3_000
    ppk.current_max = 7_000
    assert ppk.get_min_current_mA() == pytest.approx(3.0)
    assert ppk.get_max_current_mA() == pytest.approx(7.0)

    # After reset with no new samples, getters should still return last reported values.
    ppk.reset_measurements()
    ppk.current_min = 1_000
    ppk.current_max = 2_000
    assert ppk.current_num_samples == 0
    assert ppk.get_min_current_mA() == pytest.approx(3.0)
    assert ppk.get_max_current_mA() == pytest.approx(7.0)

    # New sample window updates the reported values.
    ppk.current_num_samples = 4
    ppk.current_min = 4_000
    ppk.current_max = 8_000
    assert ppk.get_min_current_mA() == pytest.approx(4.0)
    assert ppk.get_max_current_mA() == pytest.approx(8.0)


@pytest.mark.unit
def test_setIsSupply_starts_measurement_once(
    monkeypatch: pytest.MonkeyPatch, ppk2_stub: "PPK2PowerSupply"
) -> None:
    """setIsSupply() should start measurement and reader thread when not already running."""
    ppk = ppk2_stub
    ppk.v = 3.3
    ppk.r = MagicMock()
    # Create a mock thread that has ident = None (never started)
    mock_thread = MagicMock()
    mock_thread.is_alive.return_value = False
    mock_thread.ident = None
    ppk.measurement_thread = mock_thread
    ppk.resetMeasurements = MagicMock()  # type: ignore[method-assign]
    monkeypatch.setattr("meshtastic.powermon.ppk2.time.sleep", lambda _: None)

    ppk.setIsSupply(is_supply=True)

    ppk.r.set_source_voltage.assert_called_once_with(3300)
    ppk.r.start_measuring.assert_called_once()
    ppk.r.use_source_meter.assert_called_once()
    # The original mock_thread.start() should be called (not a new thread created)
    mock_thread.start.assert_called_once()
    assert ppk.resetMeasurements.call_count == 2


@pytest.mark.unit
def test_setIsSupply_does_not_restart_when_already_measuring(
    monkeypatch: pytest.MonkeyPatch, ppk2_stub: "PPK2PowerSupply"
) -> None:
    """setIsSupply() should not re-issue start_measuring when reader thread is active."""
    ppk = ppk2_stub
    ppk.v = 3.3
    ppk.r = MagicMock()
    ppk.measurement_thread = MagicMock()
    ppk.measurement_thread.is_alive.return_value = True
    ppk.resetMeasurements = MagicMock()  # type: ignore[method-assign]
    monkeypatch.setattr("meshtastic.powermon.ppk2.time.sleep", lambda _: None)

    ppk.setIsSupply(is_supply=False)

    ppk.r.set_source_voltage.assert_called_once_with(3300)
    ppk.r.start_measuring.assert_not_called()
    ppk.r.use_ampere_meter.assert_called_once()
    ppk.measurement_thread.start.assert_not_called()
    assert ppk.resetMeasurements.call_count == 2


@pytest.mark.unit
def test_setIsSupply_rechecks_thread_liveness_before_reader_restart(
    monkeypatch: pytest.MonkeyPatch, ppk2_stub: "PPK2PowerSupply"
) -> None:
    """If reader thread dies mid-call, setIsSupply() should still restart measurement safely."""
    ppk = ppk2_stub
    ppk.v = 3.3
    ppk.r = MagicMock()
    old_thread = MagicMock()
    old_thread.is_alive.side_effect = [True, False]
    old_thread.ident = 123
    ppk.measurement_thread = old_thread
    replacement_thread = MagicMock()
    replacement_thread.ident = None
    replacement_thread.is_alive.return_value = False
    monkeypatch.setattr(
        "meshtastic.powermon.ppk2.threading.Thread", lambda **_: replacement_thread
    )
    ppk.resetMeasurements = MagicMock()  # type: ignore[method-assign]
    monkeypatch.setattr("meshtastic.powermon.ppk2.time.sleep", lambda _: None)

    ppk.setIsSupply(is_supply=False)

    ppk.r.start_measuring.assert_called_once()
    replacement_thread.start.assert_called_once()
    assert ppk.measurement_thread is replacement_thread


@pytest.mark.unit
def test_average_current_camelcase_aliases_are_consistent(
    ppk2_stub: "PPK2PowerSupply",
) -> None:
    """CamelCase average-current aliases should be consistent for PPK2."""
    previous_warned = set(power_supply_module._warned_deprecations)
    power_supply_module._warned_deprecations.clear()
    try:
        ppk = ppk2_stub
        ppk.getAverageCurrentMA = MagicMock(return_value=42.0)  # type: ignore[method-assign]

        assert ppk.getAverageCurrentMA() == 42.0
        with pytest.warns(DeprecationWarning):
            assert ppk.getAverageCurrentmA() == 42.0
    finally:
        power_supply_module._warned_deprecations.clear()
        power_supply_module._warned_deprecations.update(previous_warned)


@pytest.mark.unit
def test_close_always_attempts_transport_cleanup(
    ppk2_stub: "PPK2PowerSupply",
) -> None:
    """close() should always attempt device transport cleanup, even on normal thread exit."""
    ppk = ppk2_stub
    ppk.r = MagicMock()
    ppk.r.ser = MagicMock()
    ppk.measurement_thread = MagicMock()
    ppk.measurement_thread.is_alive.return_value = False

    ppk.close()

    ppk.r.stop_measuring.assert_called_once()
    ppk.r.close.assert_called_once()
    ppk.r.ser.close.assert_called_once()
