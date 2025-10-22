"""
Example demonstrating BLE client-side reconnection using the instance recreation pattern.

This example shows the **instance recreation pattern** (simpler but less efficient):
- Create a new BLEInterface instance for each connection attempt
- Use context manager for automatic cleanup
- When disconnect occurs, create a fresh instance for reconnection

The instance recreation pattern is simpler to understand but has higher overhead
due to thread creation/destruction for each reconnection attempt.

For better performance in long-running applications, see ble_reconnect_instance_reuse_example.py
which demonstrates the more efficient instance reuse pattern.
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
    Handle a BLE interface's connection status change and notify the main loop on disconnect.
    
    If `connected` is False, sets the module-level `disconnected_event` to signal the main loop to retry the connection.
    
    Parameters:
        interface: The BLE interface object whose connection status changed.
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
    parser.add_argument("--retry-delay", type=int, default=RETRY_DELAY_SECONDS,
                        help="Seconds to wait before reconnect attempts (default: 5).")
    args = parser.parse_args()
    address = args.address
    delay = args.retry_delay

    # Subscribe to the connection change event
    pub.subscribe(on_connection_change, "meshtastic.connection.status")
    try:
        while True:
            try:
                disconnected_event.clear()
                logger.info("Attempting to connect to %s...", address)
                # Create new instance each time (simpler but less efficient)
                with meshtastic.ble_interface.BLEInterface(
                    address,
                    noProto=True,  # Set to False in a real application
                    auto_reconnect=False,
                ):
                    logger.info("Connection successful. Waiting for disconnection event...")
                    disconnected_event.wait()
                    logger.info("Disconnected normally.")

            except KeyboardInterrupt:
                logger.info("Exiting...")
                break
            except meshtastic.ble_interface.BLEInterface.BLEError:
                logger.exception("Connection failed")
            except Exception:
                logger.exception("An unexpected error occurred")

            logger.info("Retrying in %d seconds...", delay)
            time.sleep(delay)
    finally:
        pub.unsubscribe(on_connection_change, "meshtastic.connection.status")


if __name__ == "__main__":
    main()
