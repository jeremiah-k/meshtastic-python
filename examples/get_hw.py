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
            if n.get("num") == my_info.my_node_num:
                hw_model = n.get("user", {}).get("hwModel", "unknown")
                print(hw_model)
                break
        else:
            print("Local node not found in node list.")
    else:
        print(
            "No node info available. Ensure the device is connected and has joined the mesh."
        )
