"""Simple program to demo how to use meshtastic library.

To run: `python examples/pub_sub_example.py host`.
"""

import sys
import time

from pubsub import pub

import meshtastic.tcp_interface


def onConnection(interface, topic=pub.AUTO_TOPIC):  # pylint: disable=unused-argument
    """Handle (re)connection to the radio."""
    print(interface.myInfo)


def main() -> None:
    """Connect to a TCP radio and print local node info on connection."""
    if len(sys.argv) < 2:
        print(f"usage: {sys.argv[0]} host")
        raise SystemExit(1)

    pub.subscribe(onConnection, "meshtastic.connection.established")
    try:
        # Keep the process alive briefly so the callback can run.
        with meshtastic.tcp_interface.TCPInterface(sys.argv[1]):
            time.sleep(5)
    except OSError as exc:
        print(f"Error: Could not connect to {sys.argv[1]} ({exc})")
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
