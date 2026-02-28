"""code logging power consumption of meshtastic devices."""

import math
import time
from typing import Final

from .power_supply import PowerSupply

# Simulation constants
SIM_BASE_CURRENT_MA: Final[float] = 20.0  # Base simulated current in milliamperes.
SIM_CURRENT_VARIATION_MA: Final[float] = (
    5.0  # Amplitude of sinusoidal current variation in milliamperes.
)


class SimPowerSupply(PowerSupply):
    """A simulated power supply for testing."""

    def getAverageCurrentMA(self) -> float:
        """Return a simulated instantaneous current sample in mA.

        Returns
        -------
        float
            Simulated current in mA for the current measurement interval;
            not a cumulative average across calls.
        """

        # Sim an approximately 20mA load that varies sinusoidally
        return SIM_BASE_CURRENT_MA + SIM_CURRENT_VARIATION_MA * math.sin(time.time())

    # COMPAT_STABLE_SHIM: alias for getAverageCurrentMA
    def get_average_current_mA(self) -> float:
        """Shim for getAverageCurrentMA."""
        return self.getAverageCurrentMA()
