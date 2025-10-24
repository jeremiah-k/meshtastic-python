"""
Example demonstrating robust BLE client-side reconnection for a long-running application.

Use this **instance reuse pattern** when you want the most efficient reconnection strategy:
- Keep one `BLEInterface` alive with `auto_reconnect=True` (default) so the same receive thread survives disconnects.
- React to reconnection state changes via the built-in `meshtastic.connection.status` pubsub messages if the application needs UI or logging hooks.
- Tune `AUTO_RECONNECT_INITIAL_DELAY`, `AUTO_RECONNECT_MAX_DELAY`, and `AUTO_RECONNECT_BACKOFF` in `meshtastic.ble_interface`
  to balance battery-powered nodes versus desktop development rigs.

For an alternative, simpler-but-slower approach, see `ble_reconnect_instance_recreation_example.py`, which recreates the
interface each time instead of reusing it.
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
        ) as interface:
            logger.info(
                "Connection successful. The interface will now auto-reconnect on disconnect."
            )

            # In a real app, you could now start using the interface to send/receive data.
            # For example: interface.sendText("Hello mesh!")
            # For this example, we'll just sleep until the user presses Ctrl+C.
            # Demonstrate that we have access to the interface (educational purpose)
            _ = interface  # Mark as intentionally used for educational clarity
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
