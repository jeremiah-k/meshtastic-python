"""
Example demonstrating robust BLE client-side reconnection for a
long-running application.

This example shows the **instance reuse pattern** (preferred for efficiency):
- Create a single BLEInterface instance with `auto_reconnect=True` (the default)
- The interface handles all reconnections automatically when disconnects occur
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

    Parameters
    ----------
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

    This function parses a required BLE address from command-line arguments, subscribes to connection-status events,
    and creates one BLEInterface with auto_reconnect enabled. The interface will handle reconnections automatically.
    This example then waits for a KeyboardInterrupt to gracefully exit, while logging connection status changes.
    """
    logging.basicConfig(level=logging.INFO)
    parser = argparse.ArgumentParser(
        description="Meshtastic BLE interface automatic reconnection example (instance reuse pattern)."
    )
    parser.add_argument("address", help="The BLE address of your Meshtastic device.")
    args = parser.parse_args()
    address = args.address

    # Subscribe to the connection change event to be notified of status changes
    pub.subscribe(on_connection_change, "meshtastic.connection.status")

    iface = None
    try:
        # Create a single BLEInterface instance that we'll reuse across reconnections.
        # auto_reconnect=True (the default) means the interface will handle all reconnections automatically.
        logger.info("Creating and connecting to BLE interface for %s...", address)
        iface = meshtastic.ble_interface.BLEInterface(
            address,
            noProto=True,  # Set to False in a real application
        )
        logger.info(
            "Connection successful. The interface will now auto-reconnect on disconnect."
        )

        # In a real app, you could now start using the interface to send/receive data.
        # For this example, we'll just sleep until the user presses Ctrl+C.
        while True:
            time.sleep(1)

    except KeyboardInterrupt:
        logger.info("Exiting...")
    except meshtastic.ble_interface.BLEInterface.BLEError:
        logger.exception("Connection failed")
    except Exception:
        logger.exception("An unexpected error occurred")

    finally:
        # Clean up the interface when done
        if iface:
            logger.info("Closing BLE interface...")
            iface.close()


if __name__ == "__main__":
    main()
