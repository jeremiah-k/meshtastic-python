"""Classes for logging power consumption of Meshtastic devices."""

import math
import threading
import warnings
from datetime import datetime
from numbers import Real

_warned_deprecations: set[str] = set()
_warned_deprecations_lock = threading.Lock()


def _warn_deprecated_once(key: str, message: str) -> None:
    """Emit a deprecation warning once per process for a given key."""
    with _warned_deprecations_lock:
        if key in _warned_deprecations:
            return
        _warned_deprecations.add(key)
    warnings.warn(
        message,
        DeprecationWarning,
        stacklevel=3,
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

    # COMPAT_STABLE_SHIM: alias for getAverageCurrentMA
    def get_average_current_mA(self) -> float:
        """Shim for getAverageCurrentMA."""
        return self.getAverageCurrentMA()

    # COMPAT_DEPRECATE: legacy camelCase alias with unit-style typo.
    def getAverageCurrentmA(self) -> float:
        """Return average current via a deprecated camelCase alias.

        Prefer getAverageCurrentMA for consistent unit capitalization.
        """
        _warn_deprecated_once(
            "getAverageCurrentmA",
            "getAverageCurrentmA is deprecated, use getAverageCurrentMA instead.",
        )
        return self.getAverageCurrentMA()

    def getMinCurrentMA(self) -> float:
        """Return min current in mA since last call to this method.

        Returns
        -------
        float
            Minimum current in milliamperes.
        """
        # Subclasses must override for a better implementation
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
        _warn_deprecated_once(
            "getMinCurrentmA",
            "getMinCurrentmA is deprecated, use getMinCurrentMA instead.",
        )
        return self.getMinCurrentMA()

    def getMaxCurrentMA(self) -> float:
        """Return max current in mA since last call to this method.

        Returns
        -------
        float
            Maximum current in milliamperes.
        """
        # Subclasses must override for a better implementation
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
        _warn_deprecated_once(
            "getMaxCurrentmA",
            "getMaxCurrentmA is deprecated, use getMaxCurrentMA instead.",
        )
        return self.getMaxCurrentMA()

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
        """
        if isinstance(v, bool) or not isinstance(v, Real):
            raise PowerError("Voltage must be a real number")  # noqa: TRY003
        voltage = float(v)
        if not math.isfinite(voltage):
            raise PowerError("Voltage must be finite")  # noqa: TRY003
        if voltage < 0:
            raise PowerError("Voltage cannot be negative")  # noqa: TRY003
        self._v = voltage

    def powerOn(self) -> None:
        """Turn on the power supply (using the voltage set in self.v)."""
