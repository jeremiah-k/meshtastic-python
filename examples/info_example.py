"""List known mesh nodes with their hardware model information.
To run: python examples/info_example.py.
"""

import meshtastic.serial_interface

with meshtastic.serial_interface.SerialInterface() as iface:
    if not iface.nodes:
        print(
            "No node info available. Ensure the device is connected and has joined the mesh."
        )
    else:
        print("Known nodes:")
        for node in iface.nodes.values():
            user = node.get("user")
            if not isinstance(user, dict):
                user = {}
            node_id = user.get("id", "unknown")
            long_name = user.get("longName", "unknown")
            hw_model = user.get("hwModel", "unknown")
            print(f"{node_id} ({long_name}): {hw_model}")
