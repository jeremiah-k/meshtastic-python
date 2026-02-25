"""code logging power consumption of meshtastic devices."""

import math
import time

from .power_supply import PowerSupply


class SimPowerSupply(PowerSupply):
    """A simulated power supply for testing."""

    def getAverageCurrentMA(self) -> float:
        """Return average current of last measurement in mA (since last call to this method).

        Returns
        -------
        float
            The average current in mA.
        """

        # Sim an approximately 20mA load that varies sinusoidally
        return 20.0 + 5 * math.sin(time.time())
