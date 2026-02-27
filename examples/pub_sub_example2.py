"""Simple program to demo how to use meshtastic library.
   To run: python examples/pub_sub_example2.py
"""

import sys
import time

from pubsub import pub

import meshtastic.tcp_interface


def onReceive(packet, interface):  # pylint: disable=unused-argument
    """called when a packet arrives"""
    print(f"Received: {packet}")


def onConnection(interface, topic=pub.AUTO_TOPIC):  # pylint: disable=unused-argument
    """called when we (re)connect to the radio"""
    # defaults to broadcast, specify a destination ID if you wish
    interface.sendText("hello mesh")


def main() -> None:
    """Connect to a TCP radio, print received packets, and send a greeting on connect."""
    if len(sys.argv) < 2:
        print(f"usage: {sys.argv[0]} host")
        raise SystemExit(1)

    pub.subscribe(onReceive, "meshtastic.receive")
    pub.subscribe(onConnection, "meshtastic.connection.established")
    try:
        with meshtastic.tcp_interface.TCPInterface(hostname=sys.argv[1]):
            while True:
                time.sleep(1)
    except KeyboardInterrupt:
        print("Exiting...")
    except OSError as exc:
        print(f"Error: Could not connect to {sys.argv[1]} ({exc})")
        raise SystemExit(1)


if __name__ == "__main__":
    main()
