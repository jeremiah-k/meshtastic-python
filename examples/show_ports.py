# pylint: disable=duplicate-code
"""Simple program to show serial ports."""

import logging

from meshtastic.util import findPorts

LOGGER = logging.getLogger(__name__)


def _main() -> None:
    """Log discovered serial ports for local debugging."""
    LOGGER.info("Discovered ports: %s", findPorts())


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    _main()
