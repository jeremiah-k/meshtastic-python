"""List known mesh nodes with their hardware model information.
To run: python examples/info_example.py.
"""

import meshtastic.serial_interface


def main() -> None:
    """Connect and print known nodes with their hardware model."""
    with meshtastic.serial_interface.SerialInterface() as iface:
        if not iface.nodes:
            print("No node info available yet.")
            return

        print("Known nodes:")
        for node in iface.nodes.values():
            user = node.get("user", {})
            if not isinstance(user, dict):
                user = {}
            print(
                f"{user.get('id', 'unknown')} ({user.get('longName', 'unknown')}): "
                f"{user.get('hwModel', 'unknown')}"
            )


if __name__ == "__main__":
    main()
