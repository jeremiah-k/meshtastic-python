"""code logging power consumption of meshtastic devices."""

import logging
import math
from datetime import datetime
from typing import TYPE_CHECKING

from .power_supply import PowerSupply

if TYPE_CHECKING:
    from riden.riden import Riden  # pyright: ignore[reportMissingTypeStubs]
else:
    from riden import Riden  # pyright: ignore[reportMissingTypeStubs]


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
        self.prevWattHour = self._getRawWattHour()
        self.nowWattHour = self.prevWattHour
        super().__init__()  # we call this late so that the port is already open and _getRawWattHour callback works

    def setMaxCurrent(self, i: float) -> None:
        """Set the maximum current the supply will provide."""
        self.r.set_i_set(i)

    def powerOn(self) -> None:
        """Power on the supply, with reasonable defaults for meshtastic devices."""
        self.r.set_v_set(
            self.v
        )  # my WM1110 devboard header is directly connected to the 3.3V rail
        self.r.set_output(True)

    def get_average_current_mA(self) -> float:
        """Return average current of last measurement in mA since last call to this method."""
        now = datetime.now()
        nowWattHour = self._getRawWattHour()
        watts = (
            (nowWattHour - self.prevWattHour)
            / (now - self.prevPowerTime).total_seconds()
            * 3600
        )
        self.prevPowerTime = now
        self.prevWattHour = nowWattHour
        if self.v <= 0:
            return math.nan
        return (watts / self.v) * 1000

    def _getRawWattHour(self) -> float:
        """Get the current watt-hour reading."""
        self.r.update()
        return self.r.wh
