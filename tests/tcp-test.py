# reported by @ScriptBlock

import sys

from pubsub import pub

import meshtastic


def on_connection(
    iface, topic=pub.AUTO_TOPIC
):  # called when we (re)connect to the radio
    print(iface.myInfo)
    iface.close()


pub.subscribe(on_connection, "meshtastic.connection.established")
interface = meshtastic.TCPInterface(sys.argv[1])
