"""Simple program to demo how to use meshtastic library.
To run: python examples/pub_sub_example.py.
"""

import sys

from pubsub import pub

import meshtastic
import meshtastic.tcp_interface

# simple arg check
if len(sys.argv) < 2:
    print(f"usage: {sys.argv[0]} host")
    sys.exit(1)


def onConnection(interface, topic=pub.AUTO_TOPIC):  # pylint: disable=unused-argument
    """Handle connection events by printing device information and closing.

    Parameters
    ----------
    interface : meshtastic.mesh_interface.MeshInterface
        The interface instance that connected.
    topic : Any, optional
        The pubsub topic that triggered this callback.
    """
    print(interface.myInfo)
    interface.close()


pub.subscribe(onConnection, "meshtastic.connection.established")

try:
    iface = meshtastic.tcp_interface.TCPInterface(sys.argv[1])
except Exception as ex:
    print(f"Error: Could not connect to {sys.argv[1]}: {ex}")
    sys.exit(1)
