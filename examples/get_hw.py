"""Print the local node's hardware model.

To run: `python examples/get_hw.py`.
"""

import sys

import meshtastic.serial_interface


def main() -> None:
    """Connect to a serial radio and print the local hardware model."""
    if len(sys.argv) != 1:
        print(f"usage: {sys.argv[0]}", file=sys.stderr)
        print("Print the hardware model for the local node.", file=sys.stderr)
        raise SystemExit(3)

    with meshtastic.serial_interface.SerialInterface() as iface:
        my_info = iface.myInfo
        if my_info is None:
            print("Local node info is not available yet.", file=sys.stderr)
            raise SystemExit(1)

        if my_info.my_node_num == 0:
            print("Local node has not joined the mesh yet.", file=sys.stderr)
            raise SystemExit(1)

        nodes_by_num = iface.nodesByNum if isinstance(iface.nodesByNum, dict) else {}
        node = nodes_by_num.get(my_info.my_node_num)
        if not isinstance(node, dict):
            print("Local node not found in node database yet.", file=sys.stderr)
            raise SystemExit(1)

        user = node.get("user", {})
        if not isinstance(user, dict):
            user = {}
        print(user.get("hwModel", "unknown"))


if __name__ == "__main__":
    main()
