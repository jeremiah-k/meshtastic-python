"""
Example demonstrating BLE client-side reconnection using the instance recreation pattern.

Reach for this **instance recreation pattern** when simplicity matters more than raw efficiency:
- Create a new `BLEInterface` for each attempt, letting the context manager clean up threads deterministically.
- Rely on the `meshtastic.connection.status` pubsub signal to decide when to tear down and start the next connection.
- Adjust `RETRY_DELAY_SECONDS` (or the `--retry-delay` flag) alongside BLE backoff constants when targeting battery-powered
  nodes to avoid reconnect storms.

For better performance in long-running applications, see `ble_reconnect_instance_reuse_example.py`, which keeps one interface
instance alive and lets its internal auto-reconnect loop handle disconnects.
"""

import argparse
import logging
import threading
import time

from pubsub import pub  # type: ignore[import-untyped]  # pylint: disable=E0401

import meshtastic
import meshtastic.ble_interface

# Retry delay in seconds when connection fails
RETRY_DELAY_SECONDS = 5

logger = logging.getLogger(__name__)

# A thread-safe flag to signal disconnection
disconnected_event = threading.Event()


def on_connection_change(interface, connected):
    """
    Notify the reconnection loop when a BLE interface connects or disconnects.

    Logs a label for the interface (prefers its `address` attribute when present) and sets the module-level
    `disconnected_event` when `connected` is False to signal the main retry loop.

    Parameters
    ----------
        interface: BLE interface object whose `address` attribute may be used for logging.
        connected (bool): `True` when the interface is connected, `False` when disconnected.

    """
    iface_label = getattr(interface, "address", repr(interface))
    logger.info(
        "Connection changed for %s: %s",
        iface_label,
        "Connected" if connected else "Disconnected",
    )
    if not connected:
        # Signal the main loop that we've been disconnected
        disconnected_event.set()


def main():
    """
    Run a reconnection loop that repeatedly creates a new BLEInterface instance and manages retry attempts.

    Parses command-line arguments: a required BLE device address, an optional --retry-delay, and an optional --log-level.
    Subscribes to the meshtastic.connection.status pubsub topic to detect disconnects, recreates a BLEInterface for each attempt,
    waits for a disconnection event, retries after the configured delay, and exits cleanly on KeyboardInterrupt while always
    unsubscribing from the pubsub topic on shutdown.
    """
    parser = argparse.ArgumentParser(
        description="Meshtastic BLE interface reconnection (instance recreation pattern)."
    )
    parser.add_argument("address", help="The BLE address of your Meshtastic device.")
    parser.add_argument(
        "--retry-delay",
        type=float,
        default=RETRY_DELAY_SECONDS,
        help=f"Seconds to wait before reconnect attempts (default: {RETRY_DELAY_SECONDS}).",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG"],
        help="Logging level (default: INFO).",
    )
    args = parser.parse_args()
    if args.retry_delay <= 0:
        parser.error("--retry-delay must be > 0")
    address = args.address
    delay = args.retry_delay
    logging.basicConfig(level=getattr(logging, args.log_level, logging.INFO))

    # Subscribe to the connection change event
    pub.subscribe(on_connection_change, "meshtastic.connection.status")
    try:
        while True:
            disconnected_event.clear()
            logger.info("Attempting to connect to %s...", address)
            try:
                # Create new instance each time (simpler but less efficient)
                with meshtastic.ble_interface.BLEInterface(
                    address,
                    noProto=True,  # For example simplicity only; use False (default) in real applications for full protocol processing
                    auto_reconnect=False,
                ):
                    logger.info(
                        "Connection successful. Waiting for disconnection event..."
                    )
                    # Wait indefinitely for disconnect signal from pubsub
                    disconnected_event.wait()
                    logger.info("Disconnected.")
            except meshtastic.ble_interface.BLEInterface.BLEError:
                logger.exception("Connection failed")
            except Exception:
                logger.exception("An unexpected error occurred")

            logger.info("Retrying in %s seconds...", delay)
            time.sleep(delay)
    except KeyboardInterrupt:
        logger.info("Exiting...")
    finally:
        pub.unsubscribe(on_connection_change, "meshtastic.connection.status")


if __name__ == "__main__":
    main()
