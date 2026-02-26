"""Powermon module constants and configuration."""

# Voltage constants (volts)
MIN_SUPPLY_VOLTAGE_V = 0.8
"""Minimum supply voltage supported by power supplies (0.8V)."""

MAX_SUPPLY_VOLTAGE_V = 5.0
"""Maximum supply voltage supported by power supplies (5.0V)."""

# Unit conversion factors
MICROAMPS_PER_MILLIAMP = 1000
"""Microamperes per milliampere - conversion factor for current measurements."""

MILLIAMPS_PER_AMP = 1000
"""Milliamperes per ampere - conversion factor for current calculations."""

MILLIVOLTS_PER_VOLT = 1000
"""Millivolts per volt - conversion factor for voltage values."""

SECONDS_PER_HOUR = 3600
"""Seconds per hour - conversion factor for watt-hour calculations."""
