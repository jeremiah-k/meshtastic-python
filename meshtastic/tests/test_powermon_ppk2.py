"""Unit tests for PPK2 power-meter measurement state behavior."""

import threading

import pytest

try:
    from ..powermon.ppk2 import PPK2PowerSupply
except ImportError:
    pytest.skip("Can't import PPK2PowerSupply", allow_module_level=True)


def _make_ppk2_stub() -> PPK2PowerSupply:
    """Create a minimally initialized PPK2PowerSupply test instance."""
    ppk = object.__new__(PPK2PowerSupply)
    ppk._result_lock = threading.Condition()
    ppk._want_measurement = threading.Condition()
    ppk.current_sum = 0
    ppk.current_num_samples = 0
    ppk.current_min = 0
    ppk.current_max = 0
    ppk.last_reported_min = 0
    ppk.last_reported_max = 0
    ppk.num_data_reads = 0
    ppk.total_data_len = 0
    ppk.max_data_len = 0
    return ppk


@pytest.mark.unit
def test_reset_measurements_preserves_last_reported_extrema() -> None:
    """reset_measurements() should clear accumulators while preserving last reported min/max."""
    ppk = _make_ppk2_stub()
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
def test_get_min_max_update_only_when_new_samples_exist() -> None:
    """Min/max getters should retain prior values until a new sample window exists."""
    ppk = _make_ppk2_stub()

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
