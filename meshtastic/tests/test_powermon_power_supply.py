"""Unit tests for PowerSupply voltage validation."""

import math
import warnings

import pytest

from meshtastic.powermon import power_supply as power_supply_module
from meshtastic.powermon.power_supply import PowerError, PowerSupply
from meshtastic.powermon.sim import SimPowerSupply


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


@pytest.mark.unit
def test_deprecated_current_aliases_warn_once_per_method(
    power_supply: PowerSupply,
) -> None:
    """Deprecated camelCase aliases should emit one deprecation warning per method."""
    previous_warned = set(power_supply_module._warned_deprecations)
    power_supply_module._warned_deprecations.clear()
    try:
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            power_supply.getAverageCurrentmA()
            power_supply.getAverageCurrentmA()
            power_supply.getMinCurrentmA()
            power_supply.getMinCurrentmA()
            power_supply.getMaxCurrentmA()
            power_supply.getMaxCurrentmA()
    finally:
        power_supply_module._warned_deprecations.clear()
        power_supply_module._warned_deprecations.update(previous_warned)

    deprecations = [
        warning
        for warning in caught
        if issubclass(warning.category, DeprecationWarning)
    ]
    messages = [str(warning.message) for warning in deprecations]
    assert (
        messages.count(
            "getAverageCurrentmA is deprecated, use getAverageCurrentMA instead."
        )
        == 1
    )
    assert (
        messages.count("getMinCurrentmA is deprecated, use getMinCurrentMA instead.")
        == 1
    )
    assert (
        messages.count("getMaxCurrentmA is deprecated, use getMaxCurrentMA instead.")
        == 1
    )


@pytest.mark.unit
def test_sim_power_supply_snake_case_alias_is_stable_shim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SimPowerSupply snake_case alias should delegate without emitting deprecation warnings."""
    monkeypatch.setattr("meshtastic.powermon.sim.time.time", lambda: 0.0)
    supply = SimPowerSupply()

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        value = supply.get_average_current_mA()

    assert value == pytest.approx(supply.getAverageCurrentMA())
    assert not [
        warning
        for warning in caught
        if issubclass(warning.category, DeprecationWarning)
    ]
