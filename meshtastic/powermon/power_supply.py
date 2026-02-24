"""code logging power consumption of meshtastic devices."""

import math
from datetime import datetime
from numbers import Real


class PowerError(Exception):
    """An exception class for powermon errors."""

    def __init__(self, message: str) -> None:
        self.message = message
        super().__init__(self.message)


class PowerMeter:
    """Abstract class for power meters."""

    def __init__(self) -> None:
        """Initialize the PowerMeter object."""
        self.prevPowerTime = datetime.now()

    def close(self) -> None:
        """Close the power meter."""

    def get_average_current_mA(self) -> float:
        """Return average current of last measurement in mA (since last call to this method)."""
        return math.nan

    def get_min_current_mA(self) -> float:
        """Return min current in mA (since last call to this method)."""
        # Subclasses must override for a better implementation
        return self.get_average_current_mA()

    def get_max_current_mA(self) -> float:
        """Return max current in mA (since last call to this method)."""
        # Subclasses must override for a better implementation
        return self.get_average_current_mA()

    def reset_measurements(self) -> None:
        """Reset current measurements."""


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
    def v(self, value: float) -> None:
        """Backward-compatible voltage assignment route."""
        self.setVoltage(value)

    def setVoltage(self, v: float) -> None:
        """Validate and set the configured output voltage.

        Parameters
        ----------
        v : float
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
