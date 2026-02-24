"""Unit tests for Riden power supply integration helpers."""

from __future__ import annotations

import math
from datetime import datetime, timedelta
from unittest.mock import MagicMock

import pytest

try:
    from ..powermon.riden import RidenPowerSupply
except ImportError:
    pytest.skip("Can't import RidenPowerSupply", allow_module_level=True)


def _make_riden_stub() -> RidenPowerSupply:
    """Create a minimally initialized RidenPowerSupply test instance."""
    pps = object.__new__(RidenPowerSupply)
    pps.r = MagicMock()
    pps.prevPowerTime = datetime.now() - timedelta(seconds=10)
    pps.prevWattHour = 100.0
    pps.v = 3.3
    return pps


@pytest.mark.unit
def test_set_max_current_forwards_to_device() -> None:
    """Test that setMaxCurrent calls set_i_set on the underlying Riden object."""
    pps = _make_riden_stub()
    pps.setMaxCurrent(0.123)
    pps.r.set_i_set.assert_called_once_with(0.123)


@pytest.mark.unit
def test_power_on_applies_voltage_and_enables_output() -> None:
    """Test that powerOn sets configured voltage and enables output."""
    pps = _make_riden_stub()
    pps.v = 4.2
    pps.powerOn()
    pps.r.set_v_set.assert_called_once_with(4.2)
    pps.r.set_output.assert_called_once_with(True)


@pytest.mark.unit
def test_get_average_current_ma_converts_watts_to_ma() -> None:
    """Test that get_average_current_mA converts Watt-hours/time to mA."""
    pps = _make_riden_stub()
    pps.prevPowerTime = datetime.now() - timedelta(seconds=3600)
    pps.prevWattHour = 10.0
    pps._getRawWattHour = MagicMock(return_value=11.0)  # type: ignore[method-assign]
    pps.v = 2.0

    current_ma = pps.get_average_current_mA()

    # 1 Wh over 1 hour == 1 W; mA = W / V * 1000 => 500 mA
    assert current_ma == pytest.approx(500.0, rel=1e-2)
    assert pps.prevWattHour == 11.0


@pytest.mark.unit
def test_get_average_current_ma_returns_nan_for_nonpositive_voltage() -> None:
    """Test that get_average_current_mA returns NaN when voltage is not positive."""
    pps = _make_riden_stub()
    pps._getRawWattHour = MagicMock(return_value=pps.prevWattHour)  # type: ignore[method-assign]
    pps.v = 0.0
    assert math.isnan(pps.get_average_current_mA())


@pytest.mark.unit
def test_get_raw_watt_hour_updates_and_returns_wh() -> None:
    """_getRawWattHour should call update() and return r.wh."""
    pps = _make_riden_stub()
    pps.r.wh = 42.5
    value = pps._getRawWattHour()
    pps.r.update.assert_called_once()
    assert value == 42.5
