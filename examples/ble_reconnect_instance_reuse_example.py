"""
Example demonstrating robust BLE client-side reconnection for a
long-running application.

This example shows the **instance reuse pattern** (preferred for efficiency):
- Create a single BLEInterface instance with `auto_reconnect=True` (the default)
- When disconnect occurs, call connect() again on the same instance
- This avoids thread teardown/recreation overhead for better performance

The instance reuse pattern is more efficient for long-running applications
since it maintains the receive thread and other internal state across reconnections.

For an alternative simpler but less efficient approach, see
ble_reconnect_instance_recreation_example.py which demonstrates the instance recreation pattern.
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
    Run a reconnection loop that reuses a single BLEInterface instance to maintain a long-lived connection to a Meshtastic device.
    
    This function parses a required BLE address from command-line arguments, subscribes to connection-status events, creates one BLEInterface with auto_reconnect enabled, and repeatedly attempts to connect using the same instance. It waits for a disconnection signal to trigger reconnection attempts, handles KeyboardInterrupt for graceful exit, logs BLE connection errors and unexpected exceptions, and ensures the BLE interface is closed on exit.
    """
    logging.basicConfig(level=logging.INFO)
    parser = argparse.ArgumentParser(
        description="Meshtastic BLE interface automatic reconnection example (instance reuse pattern)."
    )
    parser.add_argument("address", help="The BLE address of your Meshtastic device.")
    args = parser.parse_args()
    address = args.address

    # Subscribe to the connection change event
    pub.subscribe(on_connection_change, "meshtastic.connection.status")

    # Create a single BLEInterface instance that we'll reuse across reconnections
    logger.info("Creating BLE interface for %s...", address)
    iface = meshtastic.ble_interface.BLEInterface(
        address,
        noProto=True,  # Set to False in a real application
        auto_reconnect=True,  # Keep interface alive across disconnects
    )

    try:
        while True:
            try:
                disconnected_event.clear()
                
                # Attempt to connect (or reconnect) using the same interface instance
                logger.info("Attempting to connect to %s...", address)
                iface.connect()
                logger.info("Connection successful. Waiting for disconnection event...")
                
                # Wait until the on_connection_change callback signals a disconnect
                disconnected_event.wait()
                logger.info("Disconnected normally. Will attempt to reconnect...")

            except KeyboardInterrupt:
                logger.info("Exiting...")
                break
            except meshtastic.ble_interface.BLEInterface.BLEError:
                logger.exception("Connection failed")
            except Exception:
                logger.exception("An unexpected error occurred")

            logger.info("Retrying in %d seconds...", RETRY_DELAY_SECONDS)
            time.sleep(RETRY_DELAY_SECONDS)

    finally:
        # Clean up the interface when done
        if iface:
            logger.info("Closing BLE interface...")
            iface.close()


if __name__ == "__main__":
    main()
