# reported by @ScriptBlock

import logging
import sys
from typing import Any

from pubsub import pub

import meshtastic

LOGGER = logging.getLogger(__name__)


def on_connection(
    interface: Any, _topic: Any = pub.AUTO_TOPIC
) -> None:  # called when we (re)connect to the radio
    LOGGER.info("%s", interface.myInfo)
    interface.close()


pub.subscribe(on_connection, "meshtastic.connection.established")
interface = meshtastic.TCPInterface(sys.argv[1])
