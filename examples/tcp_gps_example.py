"""Demonstration of how to look up a radio's location via its LAN connection.
Before running, connect your machine to the same WiFi network as the radio.
"""

import logging
import sys

import meshtastic.tcp_interface

RADIO_HOSTNAME = "meshtastic.local"  # Can also be an IP
logger = logging.getLogger(__name__)


def main() -> None:
    """Connect to the configured TCP radio and print local node position when available."""
    try:
        with meshtastic.tcp_interface.TCPInterface(RADIO_HOSTNAME) as iface:
            my_info = iface.myInfo
            if my_info is None:
                logger.error("myInfo is not available - radio may not yet have joined a mesh.")
                return

            if my_info.my_node_num <= 0:
                logger.error("Local node has not joined the mesh yet.")
                return

            nodes_by_num = (
                iface.nodesByNum if isinstance(iface.nodesByNum, dict) else {}
            )
            node = nodes_by_num.get(my_info.my_node_num)
            if not isinstance(node, dict):
                logger.error("Local node not found in node database yet.")
                return

            position = node.get("position")
            if position is None:
                logger.error("Node has no position data yet.")
                return

            print(position)
    except OSError as exc:
        logger.error("Could not connect to %s: %s", RADIO_HOSTNAME, exc)
        sys.exit(1)


if __name__ == "__main__":
    main()
