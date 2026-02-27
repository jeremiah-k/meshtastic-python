"""Simple program to demo how to use meshtastic library.

To run: `python examples/hello_world_serial.py`.
"""

import sys

import meshtastic.serial_interface


def main() -> None:
    """Send one text message over a serial-connected radio."""
    if len(sys.argv) < 2:
        print(f"usage: {sys.argv[0]} message")
        raise SystemExit(3)

    # By default this will auto-detect a Meshtastic device.
    with meshtastic.serial_interface.SerialInterface() as iface:
        iface.sendText(sys.argv[1])


if __name__ == "__main__":
    main()
