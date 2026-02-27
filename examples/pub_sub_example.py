"""Simple program to demo how to use meshtastic library.

To run: `python examples/pub_sub_example.py host`.
"""

import sys
import threading
from typing import Any

from pubsub import pub

import meshtastic.tcp_interface

_connected = threading.Event()


def onConnection(  # pylint: disable=unused-argument
    interface: Any, _topic: Any = pub.AUTO_TOPIC
) -> None:
    """Handle (re)connection to the radio."""
    print(interface.myInfo)
    _connected.set()


def main() -> None:
    """Connect to a TCP radio and print local node info on connection."""
    if len(sys.argv) < 2:
        print(f"usage: {sys.argv[0]} host")
        raise SystemExit(1)

    pub.subscribe(onConnection, "meshtastic.connection.established")
    _connected.clear()
    try:
        # Wait until the connection callback runs, then exit.
        with meshtastic.tcp_interface.TCPInterface(sys.argv[1]):
            if not _connected.wait(timeout=30):
                print("Error: Timed out waiting for connection callback")
                raise SystemExit(1)
    except OSError as exc:
        print(f"Error: Could not connect to {sys.argv[1]} ({exc})")
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
