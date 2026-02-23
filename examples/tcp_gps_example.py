"""Demonstration of how to look up a radio's location via its LAN connection.
Before running, connect your machine to the same WiFi network as the radio.
"""

import meshtastic
import meshtastic.tcp_interface

radio_hostname = "meshtastic.local"  # Can also be an IP
iface = meshtastic.tcp_interface.TCPInterface(radio_hostname)
my_info = iface.myInfo
if my_info is not None:
    my_node_num = my_info.my_node_num
    nodes_by_num = iface.nodesByNum
    if nodes_by_num is not None:
        node = nodes_by_num.get(my_node_num)
        if node is not None and "position" in node:
            print(node["position"])

iface.close()
