"""Unit tests for PowerSupply voltage validation."""

import math

import pytest

from meshtastic.powermon.power_supply import PowerError, PowerSupply


@pytest.mark.unit
def test_set_voltage_accepts_finite_non_negative_real() -> None:
    """SetVoltage should accept finite non-negative numeric values."""
    supply = PowerSupply()

    supply.setVoltage(3.3)

    assert supply.v == pytest.approx(3.3)


@pytest.mark.unit
def test_voltage_property_setter_delegates_to_set_voltage() -> None:
    """Assigning `v` should route through setVoltage validation."""
    supply = PowerSupply()

    supply.v = 5.0

    assert supply.v == pytest.approx(5.0)


@pytest.mark.unit
def test_set_voltage_accepts_zero() -> None:
    """Zero volts is a valid non-negative boundary value."""
    supply = PowerSupply()

    supply.setVoltage(0.0)

    assert supply.v == pytest.approx(0.0)


@pytest.mark.unit
@pytest.mark.parametrize("value", [True, math.nan, math.inf, -math.inf, -0.1])
def test_set_voltage_rejects_bool_nonfinite_and_negative(value: float | bool) -> None:
    """SetVoltage should reject bool, non-finite, and negative values."""
    supply = PowerSupply()

    with pytest.raises(PowerError):
        supply.setVoltage(value)  # type: ignore[arg-type]
