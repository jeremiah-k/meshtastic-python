"""code logging power consumption of meshtastic devices."""

import math
import warnings
from datetime import datetime
from numbers import Real


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

    def get_average_current_mA(self) -> float:
        """Shim for getAverageCurrentMA."""
        return self.getAverageCurrentMA()

    # COMPAT_DEPRECATE: legacy camelCase alias with unit-style typo.
    def getAverageCurrentmA(self) -> float:
        """Return average current via a deprecated camelCase alias.

        Prefer getAverageCurrentMA for consistent unit capitalization.
        """
        warnings.warn(
            "getAverageCurrentmA is deprecated, use getAverageCurrentMA instead.",
            DeprecationWarning,
            stacklevel=2,
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
        warnings.warn(
            "getMinCurrentmA is deprecated, use getMinCurrentMA instead.",
            DeprecationWarning,
            stacklevel=2,
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
        warnings.warn(
            "getMaxCurrentmA is deprecated, use getMaxCurrentMA instead.",
            DeprecationWarning,
            stacklevel=2,
        )
        return self.getMaxCurrentMA()

    def resetMeasurements(self) -> None:
        """Reset current measurements."""

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
