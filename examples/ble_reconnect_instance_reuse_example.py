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
import time

import meshtastic
import meshtastic.ble_interface

logger = logging.getLogger(__name__)


def main():
    """
    Run a reconnection loop that reuses a single BLEInterface instance to maintain a long-lived connection to a Meshtastic device.

    This function parses a required BLE address from command-line arguments and creates one BLEInterface with auto_reconnect enabled.
    The interface will handle reconnections automatically. This example then waits for a KeyboardInterrupt to gracefully exit.
    """
    logging.basicConfig(level=logging.INFO)
    parser = argparse.ArgumentParser(
        description="Meshtastic BLE interface automatic reconnection example (instance reuse pattern)."
    )
    parser.add_argument("address", help="The BLE address of your Meshtastic device.")
    args = parser.parse_args()
    address = args.address

    try:
        logger.info("Creating and connecting to BLE interface for %s...", address)
        with meshtastic.ble_interface.BLEInterface(
            address,
            noProto=True,  # Set to False to enable protobuf processing in production
        ) as _interface:
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


if __name__ == "__main__":
    main()
