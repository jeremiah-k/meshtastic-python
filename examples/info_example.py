"""Simple program to demo how to use meshtastic library.
To run: python examples/info_example.py.
"""

import meshtastic
import meshtastic.serial_interface

with meshtastic.serial_interface.SerialInterface() as iface:
    # call showInfo() just to ensure values are populated
    # info = iface.showInfo()

    my_info = iface.myInfo
    if my_info is not None and iface.nodes:
        for n in iface.nodes.values():
            if n["num"] == my_info.my_node_num:
                print(n["user"]["hwModel"])
                break
