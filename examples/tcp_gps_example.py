"""Demonstration of how to look up a radio's location via its LAN connection.
Before running, connect your machine to the same WiFi network as the radio.
"""

import meshtastic
import meshtastic.tcp_interface

RADIO_HOSTNAME = "meshtastic.local"  # Can also be an IP

try:
    with meshtastic.tcp_interface.TCPInterface(RADIO_HOSTNAME) as iface:
        my_info = iface.myInfo
        if my_info is not None:
            my_node_num = my_info.my_node_num
            nodes_by_num = iface.nodesByNum
            if nodes_by_num is not None:
                node = nodes_by_num.get(my_node_num)
                if node is not None and "position" in node:
                    print(node["position"])
                elif node is None:
                    print(f"Node {my_node_num} not found in nodesByNum.")
                else:
                    print("Node has no position data yet.")
            else:
                print("nodesByNum is not available.")
        else:
            print("myInfo is not available — radio may not yet have joined a mesh.")
except OSError as e:
    print(f"Could not connect to {RADIO_HOSTNAME}: {e}")
