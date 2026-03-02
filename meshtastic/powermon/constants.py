"""Powermon module constants and configuration."""

from typing import Final

# Voltage constants (volts)
MIN_SUPPLY_VOLTAGE_V: Final[float] = 0.8  # Minimum supply voltage (0.8V).
MAX_SUPPLY_VOLTAGE_V: Final[float] = 5.0  # Maximum supply voltage (5.0V).

# Unit conversion factors
MICROAMPS_PER_MILLIAMP: Final[int] = 1000  # Microamperes per milliampere.
MILLIAMPS_PER_AMP: Final[int] = 1000  # Milliamperes per ampere.
MILLIVOLTS_PER_VOLT: Final[int] = 1000  # Millivolts per volt.
SECONDS_PER_HOUR: Final[int] = 3600  # Seconds per hour.
