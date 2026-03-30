# reported by @ScriptBlock

import logging
import sys

from pubsub import pub

import meshtastic
from meshtastic.mesh_interface import MeshInterface

LOGGER = logging.getLogger(__name__)

if len(sys.argv) < 2:
    LOGGER.error("Usage: %s <hostname>", sys.argv[0])
    sys.exit(1)


def _on_connection(interface: MeshInterface, _topic: object = pub.AUTO_TOPIC) -> None:
    LOGGER.info("%s", interface.myInfo)
    interface.close()


pub.subscribe(_on_connection, "meshtastic.connection.established")
interface = meshtastic.TCPInterface(sys.argv[1])
