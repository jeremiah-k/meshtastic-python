"""Unit tests for PowerSupply voltage validation."""

import importlib
import math
import sys
import types
import typing
import warnings
from typing import Any, cast

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


@pytest.mark.unit
def test_powermon_optional_backends_are_lazy_and_dependency_error_is_clear(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Optional backend access should not require dependencies at package import time."""
    backend_names = ("PPK2PowerSupply", "RidenPowerSupply")
    _missing = object()
    original_backends = {
        backend_name: powermon.__dict__.get(backend_name, _missing)
        for backend_name in backend_names
    }
    for backend_name in backend_names:
        monkeypatch.delitem(powermon.__dict__, backend_name, raising=False)

    real_import_module = importlib.import_module

    def _fake_import_module(
        name: str, package: str | None = None
    ) -> types.ModuleType:
        if package == "meshtastic.powermon" and name in (".ppk2", ".riden"):
            missing = ModuleNotFoundError("optional backend missing")
            missing.name = "riden" if name == ".riden" else "ppk2_api"
            raise missing
        return real_import_module(name, package)

    monkeypatch.setattr(importlib, "import_module", _fake_import_module)

    try:
        backend_cls = powermon.RidenPowerSupply
        with pytest.raises(ImportError, match="optional dependency"):
            backend_cls(portName="/dev/null")
    finally:
        for backend_name, original_backend in original_backends.items():
            if original_backend is _missing:
                powermon.__dict__.pop(backend_name, None)
            else:
                powermon.__dict__[backend_name] = original_backend


@pytest.mark.unit
def test_powermon_optional_backend_lookup_re_raises_unrelated_missing_module(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Optional backend shim should not mask backend-module import bugs."""
    _missing = object()
    original_riden_backend = powermon.__dict__.get("RidenPowerSupply", _missing)
    monkeypatch.delitem(powermon.__dict__, "RidenPowerSupply", raising=False)
    real_import_module = importlib.import_module

    def _fake_import_module(
        name: str, package: str | None = None
    ) -> types.ModuleType:
        if package == "meshtastic.powermon" and name == ".riden":
            missing = ModuleNotFoundError("backend import bug")
            missing.name = "meshtastic.powermon.riden_internal"
            raise missing
        return real_import_module(name, package)

    monkeypatch.setattr(importlib, "import_module", _fake_import_module)

    try:
        with pytest.raises(ModuleNotFoundError, match="backend import bug"):
            _ = powermon.RidenPowerSupply
    finally:
        if original_riden_backend is _missing:
            powermon.__dict__.pop("RidenPowerSupply", None)
        else:
            powermon.__dict__["RidenPowerSupply"] = original_riden_backend


@pytest.mark.unit
def test_powermon_module_dir_lists_optional_backends(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """__dir__ should advertise lazy optional backend symbols before first access."""
    for backend_name in ("PPK2PowerSupply", "RidenPowerSupply"):
        monkeypatch.delitem(powermon.__dict__, backend_name, raising=False)
    exported = dir(powermon)
    assert "PPK2PowerSupply" in exported
    assert "RidenPowerSupply" in exported


@pytest.mark.unit
def test_powermon_type_checking_import_branch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reload powermon with TYPE_CHECKING enabled to execute typing-only backend imports."""
    powermon_module = importlib.import_module("meshtastic.powermon")
    stub_ppk2 = types.ModuleType("meshtastic.powermon.ppk2")
    stub_riden = types.ModuleType("meshtastic.powermon.riden")
    cast(Any, stub_ppk2).PPK2PowerSupply = type("PPK2PowerSupply", (), {})
    cast(Any, stub_riden).RidenPowerSupply = type("RidenPowerSupply", (), {})

    # importlib.reload mutates the live meshtastic.powermon module object in
    # sys.modules. Clearing cached backend attributes before the final reload
    # intentionally restores lazy-loading behavior after TYPE_CHECKING=True.
    for backend_name in ("PPK2PowerSupply", "RidenPowerSupply"):
        monkeypatch.delitem(powermon_module.__dict__, backend_name, raising=False)

    with monkeypatch.context() as patch_context:
        patch_context.setitem(sys.modules, "meshtastic.powermon.ppk2", stub_ppk2)
        patch_context.setitem(sys.modules, "meshtastic.powermon.riden", stub_riden)
        patch_context.setattr(typing, "TYPE_CHECKING", True)
        reloaded = importlib.reload(powermon_module)
        assert "PPK2PowerSupply" in reloaded.__all__
        assert "RidenPowerSupply" in reloaded.__all__
        assert reloaded.PPK2PowerSupply is cast(Any, stub_ppk2).PPK2PowerSupply
        assert reloaded.RidenPowerSupply is cast(Any, stub_riden).RidenPowerSupply

    for backend_name in ("PPK2PowerSupply", "RidenPowerSupply"):
        monkeypatch.delitem(powermon_module.__dict__, backend_name, raising=False)
    reloaded = importlib.reload(powermon_module)
    assert "PPK2PowerSupply" not in reloaded.__dict__
    assert "RidenPowerSupply" not in reloaded.__dict__
