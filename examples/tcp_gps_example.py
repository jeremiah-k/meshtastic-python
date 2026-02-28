"""Demonstration of how to look up a radio's location via its LAN connection.
Before running, connect your machine to the same WiFi network as the radio.
"""

import meshtastic.tcp_interface

RADIO_HOSTNAME = "meshtastic.local"  # Can also be an IP


def main() -> None:
    """Connect to the configured TCP radio and print local node position when available."""
    try:
        with meshtastic.tcp_interface.TCPInterface(RADIO_HOSTNAME) as iface:
            my_info = iface.myInfo
            if my_info is None:
                print("myInfo is not available — radio may not yet have joined a mesh.")
                return

            if my_info.my_node_num < 0:
                print("Local node has not joined the mesh yet.")
                return

            node = (iface.nodesByNum or {}).get(my_info.my_node_num)
            if node is None:
                print("Local node not found in node database yet.")
                return

            position = node.get("position")
            if position is None:
                print("Node has no position data yet.")
                return

            print(position)
    except OSError as exc:
        print(f"Could not connect to {RADIO_HOSTNAME}: {exc}")
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
