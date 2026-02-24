"""Unit tests for PowerSupply voltage validation."""

import math

import pytest

from meshtastic.powermon.power_supply import PowerError, PowerSupply


@pytest.mark.unit
def test_set_voltage_accepts_finite_non_negative_real(
    power_supply: PowerSupply,
) -> None:
    """SetVoltage should accept finite non-negative numeric values."""
    power_supply.setVoltage(3.3)

    assert power_supply.v == pytest.approx(3.3)


@pytest.mark.unit
def test_voltage_property_setter_delegates_to_set_voltage(
    power_supply: PowerSupply,
) -> None:
    """Assigning `v` should route through setVoltage validation."""
    power_supply.v = 5.0

    assert power_supply.v == pytest.approx(5.0)


@pytest.mark.unit
def test_set_voltage_accepts_zero(power_supply: PowerSupply) -> None:
    """Zero volts is a valid non-negative boundary value."""
    power_supply.setVoltage(0.0)

    assert power_supply.v == pytest.approx(0.0)


@pytest.mark.unit
@pytest.mark.parametrize("value", [True, math.nan, math.inf, -math.inf, -0.1])
def test_set_voltage_rejects_bool_nonfinite_and_negative(
    power_supply: PowerSupply,
    value: float | bool,
) -> None:
    """SetVoltage should reject bool, non-finite, and negative values."""
    with pytest.raises(PowerError):
        power_supply.setVoltage(value)  # type: ignore[arg-type]
