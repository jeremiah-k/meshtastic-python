"""Classes for logging power consumption of Meshtastic devices."""

import math
import threading
import warnings
from collections.abc import Callable
from datetime import datetime
from numbers import Real
from typing import cast

from .constants import MAX_SUPPLY_VOLTAGE_V

_warned_deprecations: set[str] = set()
_warned_deprecations_lock: threading.Lock = threading.Lock()


def _warn_deprecated_once(key: str, message: str) -> None:
    """Emit a deprecation warning once per process for a given key."""
    with _warned_deprecations_lock:
        if key in _warned_deprecations:
            return
        _warned_deprecations.add(key)
    warnings.warn(
        message,
        DeprecationWarning,
        # caller -> deprecated alias -> _deprecated_alias_current_method ->
        # _warn_deprecated_once: stacklevel=4 points at the original caller.
        # Verified by test_deprecated_current_aliases_warn_once_per_method.
        stacklevel=4,
    )


class PowerError(Exception):
    """An exception class for powermon errors."""

    def __init__(self, message: str) -> None:
        self.message = message
        super().__init__(self.message)


class PowerMeter:
    """Abstract class for power meters.

    The public current-measurement API retains legacy snake_case names for
    compatibility with existing callers and subclasses.
    Subclasses may optionally expose a numeric ``v`` attribute (volts) when a
    nominal supply voltage is known. Consumers use this only when ``v > 0`` and
    ignore boolean values, so subclasses should provide a real numeric value.
    """

    def __init__(self) -> None:
        """Initialize the PowerMeter object."""
        self.prevPowerTime = datetime.now()

    def close(self) -> None:
        """Close the power meter."""

    def getAverageCurrentMA(self) -> float:
        """Return average current of last measurement in mA (since last call to this method)."""
        return math.nan

    def _deprecated_alias_current_method(
        self, old_name: str, new_name: str, target_method_name: str
    ) -> float:
        """Warn once and delegate a deprecated current alias to the canonical method."""
        _warn_deprecated_once(
            old_name,
            f"{old_name} is deprecated, use {new_name} instead.",
        )
        target = cast(Callable[[], float], getattr(self, target_method_name))
        return target()

    # COMPAT_STABLE_SHIM: alias for getAverageCurrentMA
    def get_average_current_mA(self) -> float:
        """Shim for getAverageCurrentMA."""
        return self.getAverageCurrentMA()

    # COMPAT_DEPRECATE: legacy camelCase alias with unit-style typo.
    def getAverageCurrentmA(self) -> float:
        """Return average current via a deprecated camelCase alias.

        Prefer getAverageCurrentMA for consistent unit capitalization.
        """
        return self._deprecated_alias_current_method(
            "getAverageCurrentmA",
            "getAverageCurrentMA",
            "getAverageCurrentMA",
        )

    def getMinCurrentMA(self) -> float:
        """Return min current in mA since last call to this method.

        Returns
        -------
        float
            Minimum current in milliamperes.
        """
        # Preserve legacy fallback semantics for subclasses that only override
        # getAverageCurrentMA().
        return self.getAverageCurrentMA()

    # COMPAT_STABLE_SHIM: alias for getMinCurrentMA
    def get_min_current_mA(self) -> float:
        """Shim for getMinCurrentMA.

        Returns
        -------
        float
            Minimum current in milliamperes.
        """
        return self.getMinCurrentMA()

    # COMPAT_DEPRECATE: legacy camelCase alias with unit-style typo.
    def getMinCurrentmA(self) -> float:
        """Return the minimum current using a deprecated alias.

        Returns
        -------
        float
            Minimum current in milliamperes.
        """
        return self._deprecated_alias_current_method(
            "getMinCurrentmA",
            "getMinCurrentMA",
            "getMinCurrentMA",
        )

    def getMaxCurrentMA(self) -> float:
        """Return max current in mA since last call to this method.

        Returns
        -------
        float
            Maximum current in milliamperes.
        """
        # Preserve legacy fallback semantics for subclasses that only override
        # getAverageCurrentMA().
        return self.getAverageCurrentMA()

    # COMPAT_STABLE_SHIM: alias for getMaxCurrentMA
    def get_max_current_mA(self) -> float:
        """Shim for getMaxCurrentMA.

        Returns
        -------
        float
            Maximum current in milliamperes.
        """
        return self.getMaxCurrentMA()

    # COMPAT_DEPRECATE: legacy camelCase alias with unit-style typo.
    def getMaxCurrentmA(self) -> float:
        """Return the maximum current using a deprecated alias.

        Returns
        -------
        float
            Maximum current in milliamperes.
        """
        return self._deprecated_alias_current_method(
            "getMaxCurrentmA",
            "getMaxCurrentMA",
            "getMaxCurrentMA",
        )

    def resetMeasurements(self) -> None:
        """Reset current measurements."""

    # COMPAT_STABLE_SHIM: alias for resetMeasurements
    def reset_measurements(self) -> None:
        """Shim for resetMeasurements."""
        self.resetMeasurements()


class PowerSupply(PowerMeter):
    """Abstract class for power supplies."""

    def __init__(self) -> None:
        """Initialize the PowerSupply object."""
        super().__init__()
        self._v: float = 0.0

    @property
    def v(self) -> float:
        """Configured output voltage in volts."""
        return self._v

    @v.setter
    def v(self, value: float | int) -> None:
        """Backward-compatible voltage assignment route."""
        self.setVoltage(value)

    def setVoltage(self, v: float | int) -> None:
        """Validate and set the configured output voltage.

        Parameters
        ----------
        v : float | int
            Requested output voltage in volts; must be a real, non-negative value.

        Raises
        ------
        PowerError
            If ``v`` is not a finite real number.
        PowerError
            If ``v`` is negative.
        PowerError
            If ``v`` exceeds ``MAX_SUPPLY_VOLTAGE_V``.
        """
        if isinstance(v, bool) or not isinstance(v, Real):
            raise PowerError("Voltage must be a real number")  # noqa: TRY003
        voltage = float(v)
        if not math.isfinite(voltage):
            raise PowerError("Voltage must be finite")  # noqa: TRY003
        if voltage < 0:
            raise PowerError("Voltage cannot be negative")  # noqa: TRY003
        if voltage > MAX_SUPPLY_VOLTAGE_V:
            raise PowerError(  # noqa: TRY003
                f"Voltage cannot exceed {MAX_SUPPLY_VOLTAGE_V}V"
            )
        self._v = voltage

    def powerOn(self) -> None:
        """Turn on the power supply (using the voltage set in self.v)."""
