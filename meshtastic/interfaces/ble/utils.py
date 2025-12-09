"""Utility functions for BLE operations."""

import time
from typing import Optional

def _sleep(delay: float) -> None:
    """Allow callsites to throttle activity (wrapped for easier testing)."""
    time.sleep(delay)