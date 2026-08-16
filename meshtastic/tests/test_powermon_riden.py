"""Unit tests for Riden power supply integration helpers."""

from __future__ import annotations

import math
import time
from typing import cast
from unittest.mock import MagicMock

import pytest

from ..powermon.power_supply import PowerError

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
def test_power_on_rejects_non_positive_voltage(
    riden_stub: RidenPowerSupply,
) -> None:
    """PowerOn should fail fast when the configured voltage is not positive."""
    pps = riden_stub
    pps.v = 0.0
    r_mock = cast(MagicMock, pps.r)

    with pytest.raises(
        PowerError,
        match=r"Voltage must be set to a positive value before powerOn\(\)\.",
    ):
        pps.powerOn()

    r_mock.set_v_set.assert_not_called()
    r_mock.set_output.assert_not_called()


@pytest.mark.unit
def test_get_average_current_ma_converts_watts_to_ma(
    riden_stub: RidenPowerSupply,
) -> None:
    """Test that get_average_current_mA converts Watt-hours/time to mA."""
    pps = riden_stub
    pps.prevPowerTime = time.monotonic() - 3600.0
    pps.prevWattHour = 10.0
    pps._get_raw_watt_hour = MagicMock(return_value=11.0)  # type: ignore[method-assign]
    pps.v = 2.0

    current_ma = pps.get_average_current_mA()

    # 1 Wh over 1 hour == 1 W; mA = W / V * 1000 => 500 mA
    assert current_ma == pytest.approx(500.0, rel=1e-2)
    assert pps.nowWattHour == pytest.approx(11.0)
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
    start = time.monotonic()
    pps.prevPowerTime = start + 1.0
    pps.prevWattHour = 10.0
    pps.v = 3.3
    pps._get_raw_watt_hour = MagicMock(return_value=12.0)  # type: ignore[method-assign]

    result = pps.get_average_current_mA()

    assert math.isnan(result)
    assert pps.prevWattHour == pytest.approx(12.0)
    assert pps.prevPowerTime > start
    assert pps.prevPowerTime <= time.monotonic()


@pytest.mark.unit
def test_get_average_current_ma_returns_nan_on_watt_hour_rollback(
    riden_stub: RidenPowerSupply,
) -> None:
    """Counter rollback/reset windows should return NaN and resync baseline state."""
    pps = riden_stub
    start = time.monotonic()
    pps.prevPowerTime = start - 3600.0
    pps.prevWattHour = 10.0
    pps._get_raw_watt_hour = MagicMock(return_value=9.0)  # type: ignore[method-assign]
    pps.v = 3.3

    result = pps.getAverageCurrentMA()

    assert math.isnan(result)
    assert pps.nowWattHour == pytest.approx(9.0)
    assert pps.prevWattHour == pytest.approx(9.0)
    assert pps.prevPowerTime > start
    assert pps.prevPowerTime <= time.monotonic()


@pytest.mark.unit
@pytest.mark.usefixtures("reset_power_supply_deprecations")
def test_get_average_current_camelcase_aliases_delegate(
    riden_stub: RidenPowerSupply,
) -> None:
    """snake_case and legacy aliases should delegate to canonical getAverageCurrentMA."""
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


@pytest.mark.unit
def test_get_raw_watt_hour_legacy_alias_delegates(
    riden_stub: RidenPowerSupply,
) -> None:
    """Legacy `_getRawWattHour` alias should delegate to `_get_raw_watt_hour`."""
    pps = riden_stub
    pps._get_raw_watt_hour = MagicMock(return_value=7.25)  # type: ignore[method-assign]

    assert pps._getRawWattHour() == pytest.approx(7.25)
    pps._get_raw_watt_hour.assert_called_once()


@pytest.mark.unit
def test_close_closes_modbus_master_and_serial_once(
    riden_stub: RidenPowerSupply,
) -> None:
    """Close should release transport resources idempotently without powering off."""
    pps = riden_stub
    device = cast(MagicMock, pps.r)
    device.master = MagicMock()
    device.serial = MagicMock()
    pps._closed = False

    pps.close()
    pps.close()

    device.master.close.assert_called_once_with()
    device.serial.close.assert_called_once_with()
    device.set_output.assert_not_called()
    assert pps._closed is True


@pytest.mark.unit
def test_constructor_closes_transport_when_post_open_initialization_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Constructor failures after opening Riden transport must release it."""
    serial_handle = MagicMock()
    device = MagicMock()
    device.type = "RD6006"
    device.sn = "1234"
    device.fw = 1
    device.master = MagicMock()
    device.serial = serial_handle
    device.set_date_time.side_effect = RuntimeError("clock failed")
    monkeypatch.setattr(
        "meshtastic.powermon.riden.serial.Serial", MagicMock(return_value=serial_handle)
    )
    monkeypatch.setattr(
        "meshtastic.powermon.riden.Riden", MagicMock(return_value=device)
    )

    with pytest.raises(RuntimeError, match="clock failed"):
        RidenPowerSupply("COM9")

    device.master.close.assert_called_once_with()
    serial_handle.close.assert_called_once_with()


@pytest.mark.unit
def test_constructor_closes_serial_when_riden_initialization_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Failures inside the third-party Riden constructor must not leak serial."""
    serial_handle = MagicMock()
    serial_factory = MagicMock(return_value=serial_handle)
    monkeypatch.setattr("meshtastic.powermon.riden.serial.Serial", serial_factory)
    monkeypatch.setattr(
        "meshtastic.powermon.riden.Riden",
        MagicMock(side_effect=RuntimeError("riden init failed")),
    )

    with pytest.raises(RuntimeError, match="riden init failed"):
        RidenPowerSupply("COM9")

    serial_factory.assert_called_once_with(port="COM9", baudrate=115200)
    serial_handle.close.assert_called_once_with()
