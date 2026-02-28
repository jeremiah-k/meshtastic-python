"""Classes for logging power consumption of Meshtastic devices."""

import logging
import math
from datetime import datetime
from typing import Any, cast

import riden

from .constants import MILLIAMPS_PER_AMP, SECONDS_PER_HOUR
from .power_supply import PowerSupply

Riden = cast(type[Any], riden.Riden)  # type: ignore[attr-defined]


class RidenPowerSupply(PowerSupply):
    """Interface for talking to Riden programmable bench-top power supplies.
    Only RD6006 tested but others should be similar.
    """

    def __init__(self, portName: str = "/dev/ttyUSB0") -> None:
        """Initialize the RidenPowerSupply object.

        portName (str, optional): The port name of the power supply. Defaults to "/dev/ttyUSB0".
        """
        self.r = r = Riden(port=portName, baudrate=115200, address=1)
        logging.info(
            "Connected to Riden power supply: model %s, sn %s, firmware %s. Date/time updated.",
            r.type,
            r.sn,
            r.fw,
        )
        r.set_date_time(datetime.now())
        # Keep base init after port open so timing/voltage state is available.
        super().__init__()
        self.prevWattHour = self._get_raw_watt_hour()
        self.prevPowerTime = datetime.now()

    def setMaxCurrent(self, i: float) -> None:
        """Set the maximum current the supply will provide."""
        self.r.set_i_set(i)

    def powerOn(self) -> None:
        """Power on the supply, with reasonable defaults for meshtastic devices."""
        self.r.set_v_set(
            self.v
        )  # my WM1110 devboard header is directly connected to the 3.3V rail
        self.r.set_output(True)

    def getAverageCurrentMA(self) -> float:
        """Return average current of last measurement in mA since last call to this method."""
        now = datetime.now()
        nowWattHour = self._get_raw_watt_hour()
        elapsed_s = (now - self.prevPowerTime).total_seconds()
        if elapsed_s <= 0:
            # Consume the window to avoid stale deltas on subsequent reads.
            self.prevPowerTime = now
            self.prevWattHour = nowWattHour
            return math.nan
        watts = ((nowWattHour - self.prevWattHour) / elapsed_s) * SECONDS_PER_HOUR
        # Intentional: consume this measurement window even when voltage <= 0 to avoid a
        # large energy spike after voltage recovers.
        self.prevPowerTime = now
        self.prevWattHour = nowWattHour
        if self.v <= 0:
            return math.nan
        return (watts / self.v) * MILLIAMPS_PER_AMP

    def _get_raw_watt_hour(self) -> float:
        """Get the current watt-hour reading."""
        self.r.update()
        return float(self.r.wh)
