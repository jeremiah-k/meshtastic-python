"""Simple program to demo how to use meshtastic library.
To run: python examples/pub_sub_example2.py.
"""

import sys
import time
from typing import Any

from pubsub import pub

import meshtastic
import meshtastic.tcp_interface
from meshtastic.mesh_interface import MeshInterface

# simple arg check
if len(sys.argv) < 2:
    print(f"usage: {sys.argv[0]} host")
    sys.exit(1)


def onReceive(packet: dict[str, Any], interface: MeshInterface) -> None:
    """Handle incoming mesh packets by printing them.

    Parameters
    ----------
    packet : dict[str, Any]
        The received packet.
    interface : MeshInterface
        The interface instance that received the packet.
    """
    _ = interface
    print(f"Received: {packet}")


def onConnection(interface: MeshInterface, topic: Any = pub.AUTO_TOPIC) -> None:
    """Handle connection events by sending a greeting text.

    Parameters
    ----------
    interface : MeshInterface
        The interface instance that connected.
    topic : Any, optional
        The pubsub topic that triggered this callback.
    """
    _ = topic
    # defaults to broadcast, specify a destination ID if you wish
    interface.sendText("hello mesh")


pub.subscribe(onReceive, "meshtastic.receive")
pub.subscribe(onConnection, "meshtastic.connection.established")
try:
    iface = meshtastic.tcp_interface.TCPInterface(hostname=sys.argv[1])
    while True:
        time.sleep(1000)
    iface.close()
except Exception as ex:
    print(f"Error: Could not connect to {sys.argv[1]} {ex}")
    sys.exit(1)
