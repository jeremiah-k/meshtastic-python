"""
Example demonstrating a robust client-side reconnection loop for a
long-running application that uses the BLE interface.

The key is to instantiate the BLEInterface with `auto_reconnect=True` (the default).
This prevents the library from calling `close()` on the entire interface when a
disconnect occurs. Instead, it cleans up the underlying BLE client and notifies
listeners via the `onConnection` event with a `connected=False` payload.

The application can then listen for this event and attempt to create a new
BLEInterface instance to re-establish the connection, as shown in this example.
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
    Run a reconnection loop that maintains a Meshtastic BLEInterface for a given device address.
    
    Parses a required BLE address from command-line arguments, subscribes to connection-status events, and repeatedly attempts to open a BLEInterface with auto-reconnect enabled. The function waits for a disconnection signal from the connection-status callback, handles KeyboardInterrupt to exit, logs BLE-related and unexpected errors, and sleeps a configured delay before retrying.
    """
    logging.basicConfig(level=logging.INFO)
    parser = argparse.ArgumentParser(
        description="Meshtastic BLE interface automatic reconnection example."
    )
    parser.add_argument("address", help="The BLE address of your Meshtastic device.")
    args = parser.parse_args()
    address = args.address

    # Subscribe to the connection change event
    pub.subscribe(on_connection_change, "meshtastic.connection.status")

    while True:
        try:
            disconnected_event.clear()
            logger.info("Attempting to connect to %s...", address)
            # Set auto_reconnect=True to prevent the interface from closing on disconnect.
            # This allows us to handle the reconnection here.
            with meshtastic.ble_interface.BLEInterface(
                address,
                noProto=True,  # Set to False in a real application
                auto_reconnect=True,
            ):
                logger.info("Connection successful. Waiting for disconnection event...")
                # Wait until the on_connection_change callback signals a disconnect
                disconnected_event.wait()
                logger.info("Disconnected normally.")

        except KeyboardInterrupt:
            logger.info("Exiting...")
            break
        except meshtastic.ble_interface.BLEInterface.BLEError:
            logger.exception("Connection failed")
        except Exception:
            logger.exception("An unexpected error occurred")

        logger.info("Retrying in %d seconds...", RETRY_DELAY_SECONDS)
        time.sleep(RETRY_DELAY_SECONDS)


if __name__ == "__main__":
    main()
