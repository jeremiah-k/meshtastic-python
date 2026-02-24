"""Simple program to demo how to use meshtastic library.
To run: python examples/get_hw.py.
"""

import sys

import meshtastic
import meshtastic.serial_interface

# simple arg check
if len(sys.argv) != 1:
    print(f"usage: {sys.argv[0]}")
    print("Print the hardware model for the local node.")
    sys.exit(3)

with meshtastic.serial_interface.SerialInterface() as iface:
    my_info = iface.myInfo
    if my_info is not None and iface.nodes:
        for n in iface.nodes.values():
            if n["num"] == my_info.my_node_num:
                print(n["user"]["hwModel"])
                break
    else:
        print(
            "No node info available. Ensure the device is connected and has joined the mesh."
        )
