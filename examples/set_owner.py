"""Simple program to demo how to use meshtastic library.
   To run: python examples/set_owner.py Bobby 333
"""

import sys

import meshtastic.serial_interface


def main() -> None:
    """Set local node owner long/short name over serial."""
    if len(sys.argv) < 2:
        print(f"usage: {sys.argv[0]} long_name [short_name]")
        raise SystemExit(3)

    long_name = sys.argv[1]
    short_name = sys.argv[2] if len(sys.argv) > 2 else None
    with meshtastic.serial_interface.SerialInterface() as iface:
        iface.localNode.setOwner(long_name, short_name)


if __name__ == "__main__":
    main()
