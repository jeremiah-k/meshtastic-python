"""Unit tests for Riden power supply integration helpers."""

from __future__ import annotations

import math
from datetime import datetime, timedelta
from typing import cast
from unittest.mock import MagicMock

import pytest

from ..powermon import power_supply as power_supply_module

try:
    from ..powermon.riden import RidenPowerSupply
except ImportError:
    pytest.skip("Can't import RidenPowerSupply", allow_module_level=True)


@pytest.mark.unit
def test_set_max_current_forwards_to_device(riden_stub: RidenPowerSupply) -> None:
    """Test that setMaxCurrent calls set_i_set on the underlying Riden object."""
    pps = riden_stub
    r_mock = cast(MagicMock, pps.r)
    pps.setMaxCurrent(0.123)
    r_mock.set_i_set.assert_called_once_with(0.123)


@pytest.mark.unit
def test_power_on_applies_voltage_and_enables_output(
    riden_stub: RidenPowerSupply,
) -> None:
    """Test that powerOn sets configured voltage and enables output."""
    pps = riden_stub
    r_mock = cast(MagicMock, pps.r)
    pps.v = 4.2
    pps.powerOn()
    r_mock.set_v_set.assert_called_once_with(4.2)
    r_mock.set_output.assert_called_once_with(True)


@pytest.mark.unit
def test_get_average_current_ma_converts_watts_to_ma(
    riden_stub: RidenPowerSupply,
) -> None:
    """Test that get_average_current_mA converts Watt-hours/time to mA."""
    pps = riden_stub
    pps.prevPowerTime = datetime.now() - timedelta(seconds=3600)
    pps.prevWattHour = 10.0
    pps._get_raw_watt_hour = MagicMock(return_value=11.0)  # type: ignore[method-assign]
    pps.v = 2.0

    current_ma = pps.get_average_current_mA()

    # 1 Wh over 1 hour == 1 W; mA = W / V * 1000 => 500 mA
    assert current_ma == pytest.approx(500.0, rel=1e-2)
    assert pps.prevWattHour == 11.0


@pytest.mark.unit
def test_get_average_current_ma_returns_nan_for_nonpositive_voltage(
    riden_stub: RidenPowerSupply,
) -> None:
    """Test that get_average_current_mA returns NaN when voltage is not positive."""
    pps = riden_stub
    pps._get_raw_watt_hour = MagicMock(  # type: ignore[method-assign]
        return_value=pps.prevWattHour
    )
    pps.v = 0.0
    assert math.isnan(pps.get_average_current_mA())


@pytest.mark.unit
def test_get_average_current_ma_consumes_window_on_nonpositive_elapsed(
    riden_stub: RidenPowerSupply,
) -> None:
    """Non-positive elapsed windows should return NaN and advance previous window state."""
    pps = riden_stub
    start = datetime.now()
    pps.prevPowerTime = start + timedelta(seconds=1)
    pps.prevWattHour = 10.0
    pps._get_raw_watt_hour = MagicMock(return_value=12.0)  # type: ignore[method-assign]

    result = pps.get_average_current_mA()

    assert math.isnan(result)
    assert pps.prevWattHour == pytest.approx(12.0)
    assert pps.prevPowerTime != start + timedelta(seconds=1)


@pytest.mark.unit
def test_get_average_current_camelcase_aliases_delegate(
    riden_stub: RidenPowerSupply,
) -> None:
    """CamelCase aliases should delegate to getAverageCurrentMA."""
    power_supply_module._warned_deprecations.clear()
    pps = riden_stub
    pps.getAverageCurrentMA = MagicMock(return_value=123.4)  # type: ignore[method-assign]
    delegated = pps.getAverageCurrentMA

    assert pps.get_average_current_mA() == 123.4
    with pytest.warns(DeprecationWarning):
        assert pps.getAverageCurrentmA() == 123.4
    assert delegated.call_count == 2


@pytest.mark.unit
def test_get_raw_watt_hour_updates_and_returns_wh(
    riden_stub: RidenPowerSupply,
) -> None:
    """_get_raw_watt_hour should call update() and return r.wh."""
    pps = riden_stub
    r_mock = cast(MagicMock, pps.r)
    r_mock.wh = 42.5
    value = pps._get_raw_watt_hour()
    r_mock.update.assert_called_once()
    assert value == 42.5
