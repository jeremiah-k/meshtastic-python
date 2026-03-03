"""Unit tests for PPK2 power-meter measurement state behavior."""

import math
import warnings
from unittest.mock import MagicMock

import pytest

try:
    from meshtastic.powermon.ppk2 import PPK2PowerSupply
    from meshtastic.powermon.power_supply import PowerSupply
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
def test_initial_pre_sample_stats_are_nan(ppk2_stub: "PPK2PowerSupply") -> None:
    """Pre-sample stats should start as NaN so no-data is distinguishable from zero."""
    ppk = ppk2_stub
    assert math.isnan(ppk.current_average)
    assert math.isnan(ppk.last_reported_min)
    assert math.isnan(ppk.last_reported_max)


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
def test_setIsSupply_allows_meter_mode_without_supply_voltage(
    monkeypatch: pytest.MonkeyPatch, ppk2_stub: "PPK2PowerSupply"
) -> None:
    """Amp-meter mode should not require supply voltage prevalidation."""
    ppk = ppk2_stub
    ppk.v = 0.0
    ppk.r = MagicMock()
    ppk.measurement_thread = MagicMock()
    ppk.measurement_thread.is_alive.return_value = True
    ppk.resetMeasurements = MagicMock()  # type: ignore[method-assign]
    monkeypatch.setattr("meshtastic.powermon.ppk2.time.sleep", lambda _: None)

    ppk.setIsSupply(is_supply=False)

    ppk.r.use_ampere_meter.assert_called_once()
    ppk.r.set_source_voltage.assert_called_once_with(0)


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
@pytest.mark.usefixtures("reset_power_supply_deprecations")
def test_average_current_camelcase_aliases_are_consistent(
    ppk2_stub: "PPK2PowerSupply",
) -> None:
    """CamelCase average-current aliases should be consistent for PPK2."""
    ppk = ppk2_stub
    ppk.getAverageCurrentMA = MagicMock(return_value=42.0)  # type: ignore[method-assign]

    assert ppk.getAverageCurrentMA() == 42.0
    with pytest.warns(DeprecationWarning):
        assert ppk.getAverageCurrentmA() == 42.0


@pytest.mark.unit
def test_ppk2_snake_case_current_aliases_delegate(
    ppk2_stub: "PPK2PowerSupply",
) -> None:
    """snake_case current aliases should delegate to canonical camelCase methods."""
    ppk = ppk2_stub
    ppk.getMinCurrentMA = MagicMock(return_value=1.1)  # type: ignore[method-assign]
    ppk.getMaxCurrentMA = MagicMock(return_value=2.2)  # type: ignore[method-assign]
    ppk.getAverageCurrentMA = MagicMock(return_value=3.3)  # type: ignore[method-assign]

    assert ppk.get_min_current_mA() == pytest.approx(1.1)
    assert ppk.get_max_current_mA() == pytest.approx(2.2)
    assert ppk.get_average_current_mA() == pytest.approx(3.3)


@pytest.mark.unit
@pytest.mark.usefixtures("reset_power_supply_deprecations")
def test_ppk2_lowercase_m_aliases_warn_once(ppk2_stub: "PPK2PowerSupply") -> None:
    """Deprecated lowercase-m aliases should emit one warning per method."""
    ppk = ppk2_stub
    ppk.getMinCurrentMA = MagicMock(return_value=1.1)  # type: ignore[method-assign]
    ppk.getMaxCurrentMA = MagicMock(return_value=2.2)  # type: ignore[method-assign]
    ppk.getAverageCurrentMA = MagicMock(return_value=3.3)  # type: ignore[method-assign]

    with pytest.warns(DeprecationWarning):
        assert ppk.getMinCurrentmA() == pytest.approx(1.1)
    with pytest.warns(DeprecationWarning):
        assert ppk.getMaxCurrentmA() == pytest.approx(2.2)
    with pytest.warns(DeprecationWarning):
        assert ppk.getAverageCurrentmA() == pytest.approx(3.3)

    with warnings.catch_warnings(record=True) as recorded:
        warnings.simplefilter("always")
        assert ppk.getMinCurrentmA() == pytest.approx(1.1)
        assert ppk.getMaxCurrentmA() == pytest.approx(2.2)
        assert ppk.getAverageCurrentmA() == pytest.approx(3.3)
    assert not [
        warning
        for warning in recorded
        if issubclass(warning.category, DeprecationWarning)
    ]


@pytest.mark.unit
def test_ppk2_reset_measurements_snake_case_alias(
    ppk2_stub: "PPK2PowerSupply",
) -> None:
    """reset_measurements() should call resetMeasurements()."""
    ppk = ppk2_stub
    ppk.resetMeasurements = MagicMock()  # type: ignore[method-assign]

    ppk.reset_measurements()

    ppk.resetMeasurements.assert_called_once()


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


@pytest.mark.unit
def test_ppk2_constructor_initializes_measurement_thread_and_compatibility_wrappers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PPK2PowerSupply.__init__ should configure thread and rely on shared PowerSupply wrappers."""

    class _FakePPK2Api:
        def __init__(self, port_name: str) -> None:
            self.port_name = port_name
            self.modifiers_called = False

        def get_modifiers(self) -> None:
            """Simulate loading PPK2 modifiers during construction."""
            self.modifiers_called = True

    monkeypatch.setattr(
        "meshtastic.powermon.ppk2.ppk2_api.PPK2_API",
        _FakePPK2Api,
    )

    ppk = PPK2PowerSupply(portName="COM9")

    assert ppk.measurement_thread.name == "ppk2 measurement"
    assert ppk.measurement_thread.daemon is True
    assert PPK2PowerSupply.getMinCurrentmA is PowerSupply.getMinCurrentmA
    assert PPK2PowerSupply.getMaxCurrentmA is PowerSupply.getMaxCurrentmA
    assert PPK2PowerSupply.getAverageCurrentmA is PowerSupply.getAverageCurrentmA
