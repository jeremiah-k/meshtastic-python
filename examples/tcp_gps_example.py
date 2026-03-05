"""Demonstration of how to look up a radio's location via its LAN connection.
Before running, connect your machine to the same WiFi network as the radio.
"""

import logging
import sys

import meshtastic.tcp_interface

RADIO_HOSTNAME = "meshtastic.local"  # Can also be an IP
EXIT_SUCCESS = 0
EXIT_FAILURE = 1
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def main() -> int:
    """Connect to the configured TCP radio and print local node position when available."""
    try:
        with meshtastic.tcp_interface.TCPInterface(RADIO_HOSTNAME) as iface:
            my_info = iface.myInfo
            if my_info is None:
                logger.error(
                    "myInfo is not available - radio may not yet have joined a mesh."
                )
                return EXIT_FAILURE

            if my_info.my_node_num <= 0:
                logger.error("Local node has not joined the mesh yet.")
                return EXIT_FAILURE

            nodes_by_num = (
                iface.nodesByNum if isinstance(iface.nodesByNum, dict) else {}
            )
            node = nodes_by_num.get(my_info.my_node_num)
            if not isinstance(node, dict):
                logger.error("Local node not found in node database yet.")
                return EXIT_FAILURE

            position = node.get("position")
            if position is None:
                logger.error("Node has no position data yet.")
                return EXIT_FAILURE

            print(position)
            return EXIT_SUCCESS
    except OSError:
        logger.exception("Could not connect to %s", RADIO_HOSTNAME)
        return EXIT_FAILURE


if __name__ == "__main__":
    sys.exit(main())
