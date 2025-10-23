"""
Example demonstrating BLE client-side reconnection using the instance recreation pattern.

Reach for this **instance recreation pattern** when simplicity matters more than raw efficiency:
- Create a new `BLEInterface` for each attempt, letting the context manager clean up threads deterministically.
- Rely on the `meshtastic.connection.status` pubsub signal to decide when to tear down and start the next connection.
- Adjust `RETRY_DELAY_SECONDS` (or the `--retry-delay` flag) alongside BLE backoff constants when targeting battery-powered nodes to avoid reconnect storms.

For better performance in long-running applications, see `ble_reconnect_instance_reuse_example.py`, which keeps one interface instance alive and lets its internal auto-reconnect loop handle disconnects.
"""

import argparse
import logging
import threading
import time

from pubsub import pub

import meshtastic
import meshtastic.ble_interface

# Retry delay in seconds when connection fails
RETRY_DELAY_SECONDS = 5

logger = logging.getLogger(__name__)

# A thread-safe flag to signal disconnection
disconnected_event = threading.Event()


def on_connection_change(interface, connected):
    """
    Handle BLE interface connection changes and notify the main loop when a disconnect occurs.

    Logs the interface's address (or repr) and connection state. When disconnected, sets the module-level `disconnected_event` to signal the main loop to attempt a reconnect.

    Parameters
    ----------
        interface: The BLE interface object whose connection status changed; its `address` attribute is used for logging if present.
        connected (bool): `True` if the interface is connected, `False` if it is disconnected.

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
    Run a reconnection loop that repeatedly creates a new BLEInterface for each connection attempt.

    Parses a required BLE address from command-line arguments, subscribes to connection-status events
    to detect disconnects, and uses a fresh BLEInterface instance (via a context manager) for each attempt.
    Exits cleanly on KeyboardInterrupt, logs BLE-related and unexpected errors, and retries after
    RETRY_DELAY_SECONDS when a connection ends or fails.
    """
    logging.basicConfig(level=logging.INFO)
    parser = argparse.ArgumentParser(
        description="Meshtastic BLE interface reconnection (instance recreation pattern)."
    )
    parser.add_argument("address", help="The BLE address of your Meshtastic device.")
    parser.add_argument(
        "--retry-delay",
        type=int,
        default=RETRY_DELAY_SECONDS,
        help=f"Seconds to wait before reconnect attempts (default: {RETRY_DELAY_SECONDS}).",
    )
    args = parser.parse_args()
    address = args.address
    delay = args.retry_delay

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
                    noProto=True,  # Set to False in a real application
                    auto_reconnect=False,
                ):
                    logger.info(
                        "Connection successful. Waiting for disconnection event..."
                    )
                    disconnected_event.wait()
                    logger.info("Disconnected normally.")
            except meshtastic.ble_interface.BLEInterface.BLEError:
                logger.exception("Connection failed")
            except Exception:
                logger.exception("An unexpected error occurred")

            logger.info("Retrying in %d seconds...", delay)
            time.sleep(delay)
    except KeyboardInterrupt:
        logger.info("Exiting...")
    finally:
        pub.unsubscribe(on_connection_change, "meshtastic.connection.status")


if __name__ == "__main__":
    main()
