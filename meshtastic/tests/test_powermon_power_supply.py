"""Unit tests for PowerSupply voltage validation."""

import math
import warnings

import pytest

from meshtastic import powermon
from meshtastic.powermon.constants import MAX_SUPPLY_VOLTAGE_V
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
@pytest.mark.parametrize(
    "value",
    [True, math.nan, math.inf, -math.inf, -0.1, MAX_SUPPLY_VOLTAGE_V + 0.1],
)
def test_set_voltage_rejects_invalid_values(
    power_supply: PowerSupply,
    value: float | bool,
) -> None:
    """SetVoltage should reject bool, non-finite, negative, and out-of-range values."""
    with pytest.raises(PowerError):
        power_supply.setVoltage(value)


@pytest.mark.unit
@pytest.mark.usefixtures("reset_power_supply_deprecations")
def test_deprecated_current_aliases_warn_once_per_method(
    power_supply: PowerSupply,
) -> None:
    """Deprecated camelCase aliases should emit one deprecation warning per method."""
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        power_supply.getAverageCurrentmA()
        power_supply.getAverageCurrentmA()
        power_supply.getMinCurrentmA()
        power_supply.getMinCurrentmA()
        power_supply.getMaxCurrentmA()
        power_supply.getMaxCurrentmA()

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
@pytest.mark.parametrize(
    ("snake_alias", "canonical"),
    [
        ("get_average_current_mA", "getAverageCurrentMA"),
        ("get_min_current_mA", "getMinCurrentMA"),
        ("get_max_current_mA", "getMaxCurrentMA"),
    ],
)
def test_sim_power_supply_snake_case_aliases_are_stable_shims(
    monkeypatch: pytest.MonkeyPatch,
    snake_alias: str,
    canonical: str,
) -> None:
    """SimPowerSupply snake_case aliases should delegate without deprecation warnings."""
    monkeypatch.setattr("meshtastic.powermon.sim.time.time", lambda: 0.0)
    supply = SimPowerSupply()

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        value = getattr(supply, snake_alias)()

    assert value == pytest.approx(getattr(supply, canonical)())
    assert not [
        warning
        for warning in caught
        if issubclass(warning.category, DeprecationWarning)
    ]


@pytest.mark.unit
def test_sim_power_supply_reset_measurements_snake_case_alias(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """reset_measurements() should delegate to resetMeasurements() without warnings."""
    supply = SimPowerSupply()
    called = {"count": 0}

    def _fake_reset() -> None:
        called["count"] += 1

    monkeypatch.setattr(supply, "resetMeasurements", _fake_reset)
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        supply.reset_measurements()

    assert called["count"] == 1
    assert not [
        warning
        for warning in caught
        if issubclass(warning.category, DeprecationWarning)
    ]


@pytest.mark.unit
def test_powermon_public_exports_remain_available() -> None:
    """Powermon package should keep expected historical public exports."""
    expected_exports = {
        "PowerError",
        "PowerMeter",
        "PowerSupply",
        "PPK2PowerSupply",
        "RidenPowerSupply",
        "SimPowerSupply",
        "PowerStress",
    }
    assert expected_exports.issubset(set(powermon.__all__))
    for export_name in expected_exports:
        assert hasattr(powermon, export_name)
