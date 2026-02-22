"""# A library for the Meshtastic Client API.

Primary interfaces: SerialInterface, TCPInterface, BLEInterface

Install with pip: "[pip3 install meshtastic](https://pypi.org/project/meshtastic/)"

Source code on [github](https://github.com/meshtastic/python)

notable properties of interface classes:

- `nodes` - The database of received nodes.  Includes always up-to-date location and username information for each
node in the mesh.  This is a read-only datastructure.
- `nodesByNum` - like "nodes" but keyed by nodeNum instead of nodeId. As such, includes "unknown" nodes which haven't seen a User packet yet
- `myInfo` & `metadata` - Contain read-only information about the local radio device (software version, hardware version, etc)
- `localNode` - Pointer to a node object for the local node

notable properties of nodes:

- `localConfig` - Current radio settings, can be written to the radio with the `writeConfig` method.
- `moduleConfig` - Current module settings, can be written to the radio with the `writeConfig` method.
- `channels` - The node's channels, keyed by index.

# Published PubSub topics

We use a [publish-subscribe](https://pypubsub.readthedocs.io/en/v4.0.3/) model to communicate asynchronous events.  Available
topics:

- `meshtastic.connection.established` - published once we've successfully connected to the radio and downloaded the node DB
- `meshtastic.connection.lost` - published once we've lost our link to the radio
- `meshtastic.receive.text(packet)` - delivers a received packet as a dictionary, if you only care about a particular
type of packet, you should subscribe to the full topic name.  If you want to see all packets, simply subscribe to "meshtastic.receive".
- `meshtastic.receive.position(packet)`
- `meshtastic.receive.user(packet)`
- `meshtastic.receive.data.portnum(packet)` (where portnum is an integer or well known PortNum enum)
- `meshtastic.node.updated(node = NodeInfo)` - published when a node in the DB changes (appears, location changed, username changed, etc...)
- `meshtastic.log.line(line)` - a raw unparsed log line from the radio
- `meshtastic.clientNotification(notification, interface) - a ClientNotification sent from the radio

We receive position, user, or data packets from the mesh.  You probably only care about `meshtastic.receive.data`.  The first argument for
that publish will be the packet.  Text or binary data packets (from `sendData` or `sendText`) will both arrive this way.  If you print packet
you'll see the fields in the dictionary.  `decoded.data.payload` will contain the raw bytes that were sent.  If the packet was sent with
`sendText`, `decoded.data.text` will **also** be populated with the decoded string.  For ASCII these two strings will be the same, but for
unicode scripts they can be different.

# Example Usage
```
import meshtastic
import meshtastic.serial_interface
from pubsub import pub

def onReceive(packet, interface): # called when a packet arrives
    print(f"Received: {packet}")

def onConnection(interface, topic=pub.AUTO_TOPIC): # called when we (re)connect to the radio
    # defaults to broadcast, specify a destination ID if you wish
    interface.sendText("hello mesh")

pub.subscribe(onReceive, "meshtastic.receive")
pub.subscribe(onConnection, "meshtastic.connection.established")
# By default will try to find a meshtastic device, otherwise provide a device path like /dev/ttyUSB0
interface = meshtastic.serial_interface.SerialInterface()

```
"""

import logging
from importlib import import_module
from typing import Any, Callable, NamedTuple

from google.protobuf.json_format import MessageToJson
from pubsub import pub  # type: ignore[import-untyped]

from meshtastic.node import Node
from meshtastic.util import (
    DeferredExecution,
    FixmeError,
    Timeout,
    catchAndIgnore,
    stripnl,
)

from . import (
    util,
)
from .protobuf import (
    admin_pb2,
    apponly_pb2,
    channel_pb2,
    config_pb2,
    mesh_pb2,
    mqtt_pb2,
    paxcount_pb2,
    portnums_pb2,
    powermon_pb2,
    remote_hardware_pb2,
    storeforward_pb2,
    telemetry_pb2,
)

# Keep exports sorted for lint stability and easier scanning.
# ruff: noqa: RUF022 -- sorted case-insensitively for readability.
__all__ = [
    "admin_pb2",
    "apponly_pb2",
    "BROADCAST_ADDR",
    "BROADCAST_NUM",
    "catchAndIgnore",
    "channel_pb2",
    "config_pb2",
    "DeferredExecution",
    "FixmeError",
    "KnownProtocol",
    "LOCAL_ADDR",
    "mesh_pb2",
    "MessageToJson",
    "mqtt_pb2",
    "Node",
    "NODELESS_WANT_CONFIG_ID",
    "OUR_APP_VERSION",
    "paxcount_pb2",
    "portnums_pb2",
    "powermon_pb2",
    "protocols",
    "pub",
    "remote_hardware_pb2",
    "ResponseHandler",
    "storeforward_pb2",
    "stripnl",
    "telemetry_pb2",
    "Timeout",
    "util",
]


def __getattr__(name: str) -> Any:
    """Provide lazy access to legacy module attributes.

    When the attribute "serial" is requested, import the internal serial_interface
    module, cache it on the module globals as "serial", and return it. For any
    other attribute, raise AttributeError.

    Parameters
    ----------
    name : str
        Attribute name being accessed.

    Returns
    -------
    Any
        The resolved module object for the requested legacy attribute
        (e.g., the internal serial_interface for "serial").

    Raises
    ------
    AttributeError
        If the requested attribute is not provided by this lazy loader.
    """
    if name == "serial":
        # Keep historical `meshtastic.serial` access, but map it to our
        # internal serial interface module (not the third-party pyserial module).
        serial_module = import_module(".serial_interface", __name__)
        # Cache in module namespace so subsequent accesses bypass __getattr__
        globals()["serial"] = serial_module
        return serial_module
    raise AttributeError(  # noqa: TRY003
        f"module {__name__!r} has no attribute {name!r}"
    )


# Note: To follow PEP224, comments should be after the module variable.

LOCAL_ADDR = "^local"
"""A special ID that means the local node"""

BROADCAST_NUM: int = 0xFFFFFFFF
"""if using 8 bit nodenums this will be shortened on the target"""

BROADCAST_ADDR = "^all"
"""A special ID that means broadcast"""

OUR_APP_VERSION: int = 20300
"""The numeric buildnumber (shared with android apps) specifying the
   level of device code we are guaranteed to understand

   format is Mmmss (where M is 1+the numeric major number. i.e. 20120 means 1.1.20
"""

NODELESS_WANT_CONFIG_ID = 69420
"""A special thing to pass for want_config_id that instructs nodes to skip sending nodeinfos other than its own."""

publishingThread = DeferredExecution("publishing")

logger = logging.getLogger(__name__)


class ResponseHandler(NamedTuple):
    """A pending response callback, waiting for a response to one of our messages."""

    # requestId: int - used only as a key
    #: a callable to call when a response is received
    callback: Callable
    #: Whether ACKs and NAKs should be passed to this handler
    ackPermitted: bool = False
    # FIXME, add timestamp and age out old requests


class KnownProtocol(NamedTuple):
    """Used to automatically decode known protocol payloads."""

    #: A descriptive name (e.g. "text", "user", "admin")
    name: str
    #: If set, will be called to parse as a protocol buffer
    protobufFactory: Callable | None = None
    #: If set, invoked as onReceive(interface, packet)
    onReceive: Callable | None = None


def _on_text_receive(iface: Any, asDict: dict[str, Any]) -> None:
    """Decode text payloads from a received packet and update per-node metadata.

    If the packet's decoded.payload contains valid UTF-8, store the decoded string in
    asDict["decoded"]["text"]. If decoding fails, leave that field unset and log an error.
    Always invokes the interface's info-update path to refresh node metadata based on the packet.

    Parameters
    ----------
    iface : Any
        The interface instance that received the packet.
    asDict : dict[str, Any]
        Packet dictionary expected to contain
        decoded.payload (bytes) where the text is stored.
    """
    # We don't throw if the utf8 is invalid in the text message.  Instead we just don't populate
    # the decoded.data.text and we log an error message.  This at least allows some delivery to
    # the app and the app can deal with the missing decoded representation.
    #
    # Usually btw this problem is caused by apps sending binary data but setting the payload type to
    # text.
    logger.debug("in _on_text_receive() asDict:%s", asDict)
    try:
        asBytes = asDict["decoded"]["payload"]
        asDict["decoded"]["text"] = asBytes.decode("utf-8")
    except (UnicodeDecodeError, KeyError, AttributeError):
        logger.exception("Malformatted utf8 in text message")
    _receive_info_update(iface, asDict)


def _on_position_receive(iface: Any, asDict: dict[str, Any]) -> None:
    """Update the sender node's stored position when a received packet contains position data.

    If asDict contains a "from" field and a decoded "position", the position is normalized
    using the interface's fixup routine and written to that node's "position" entry.

    Parameters
    ----------
    iface : Any
        Interface instance that provides position normalization and node lookup helpers.
    asDict : dict[str, Any]
        Packet dictionary expected to contain
        "from" and "decoded"->"position".
    """
    logger.debug("in _on_position_receive() asDict:%s", asDict)
    if "decoded" in asDict:
        if "position" in asDict["decoded"] and "from" in asDict:
            p = asDict["decoded"]["position"]
            logger.debug("p:%s", p)
            p = iface._fixupPosition(p)
            logger.debug("after fixup p:%s", p)
            # update node DB as needed
            iface._getOrCreateByNum(asDict["from"])["position"] = p


def _on_node_info_receive(iface: Any, asDict: dict[str, Any]) -> None:
    """Update the local node record from a received NodeInfo ("user") payload.

    When `asDict` contains a decoded `"user"` entry and a `"from"` sender, stores
    the decoded user protobuf on the sender's node under `node["user"]`, ensures
    `iface.nodes` maps the user's `id` to that node, and refreshes per-node metadata
    via _receive_info_update(iface, asDict).

    Parameters
    ----------
    iface : Any
        Interface instance managing the node database.
    asDict : dict[str, Any]
        Received packet dictionary; expected to contain
        `"decoded" -> "user"` and `"from"`.
    """
    logger.debug("in _on_node_info_receive() asDict:%s", asDict)
    if "decoded" in asDict:
        if "user" in asDict["decoded"] and "from" in asDict:
            p = asDict["decoded"]["user"]
            # decode user protobufs and update nodedb, provide decoded version as "position" in the published msg
            # update node DB as needed
            n = iface._getOrCreateByNum(asDict["from"])
            n["user"] = p
            # We now have a node ID, make sure it is up-to-date in that table
            iface.nodes[p["id"]] = n
            _receive_info_update(iface, asDict)


def _on_telemetry_receive(iface: Any, asDict: dict[str, Any]) -> None:
    """Update the appropriate telemetry section on the sender node when a telemetry packet is received.

    Merges metrics from the packet's `decoded.telemetry` into one of the node's telemetry
    sections: `deviceMetrics`, `environmentMetrics`, `airQualityMetrics`, `powerMetrics`,
    or `localStats`. If the packet lacks a `from` field or none of these sections are
    present, no change is made.

    Parameters
    ----------
    iface : Any
        Interface instance used to look up or create the target node.
    asDict : dict[str, Any]
        Received packet dictionary; expected to include
        a `from` key and may include `decoded.telemetry`.
    """
    logger.debug("in _on_telemetry_receive() asDict:%s", asDict)
    if "from" not in asDict:
        return

    toUpdate = None

    telemetry = (asDict.get("decoded") or {}).get("telemetry") or {}
    if "deviceMetrics" in telemetry:
        toUpdate = "deviceMetrics"
    elif "environmentMetrics" in telemetry:
        toUpdate = "environmentMetrics"
    elif "airQualityMetrics" in telemetry:
        toUpdate = "airQualityMetrics"
    elif "powerMetrics" in telemetry:
        toUpdate = "powerMetrics"
    elif "localStats" in telemetry:
        toUpdate = "localStats"
    else:
        return

    updateObj = telemetry.get(toUpdate)
    if updateObj is None:
        return
    node = iface._getOrCreateByNum(asDict["from"])
    newMetrics = node.get(toUpdate, {})
    newMetrics.update(updateObj)
    logger.debug(
        "updating %s metrics for %s to %s", toUpdate, asDict["from"], newMetrics
    )
    node[toUpdate] = newMetrics


def _receive_info_update(iface: Any, asDict: dict[str, Any]) -> None:
    """Update per-node metadata fields based on information present in a received packet dictionary.

    Parameters
    ----------
    iface : Any
        The interface instance whose node store will be updated.
    asDict : dict[str, Any]
        Parsed packet dictionary; if it contains a "from"
        key, the node identified by that value will have these fields set:
        - lastReceived: the full packet dictionary
        - lastHeard: value of `rxTime` from the packet (or None)
        - snr: value of `rxSnr` from the packet (or None)
        - hopLimit: value of `hopLimit` from the packet (or None)
    """
    if "from" in asDict:
        node = iface._getOrCreateByNum(asDict["from"])
        node["lastReceived"] = asDict
        node["lastHeard"] = asDict.get("rxTime")
        node["snr"] = asDict.get("rxSnr")
        node["hopLimit"] = asDict.get("hopLimit")


def _on_admin_receive(iface: Any, asDict: dict[str, Any]) -> None:
    """Store the admin session passkey from an admin packet on the sending node.

    If the expected fields are present in `asDict`, sets the sender node's
    "adminSessionPassKey" to the extracted `session_passkey`.

    Parameters
    ----------
    iface : Any
        The interface instance managing the node database.
    asDict : dict[str, Any]
        Received packet dictionary; expected to contain
        `decoded.admin.raw.session_passkey` and a `from` sender field.
    """
    logger.debug("in _on_admin_receive() asDict:%s", asDict)
    if "from" not in asDict:
        logger.debug(
            "Dropping admin packet because 'from' field is missing: %s", asDict
        )
        return

    try:
        adminMessage = asDict["decoded"]["admin"]["raw"]
        iface._getOrCreateByNum(asDict["from"])[
            "adminSessionPassKey"
        ] = adminMessage.session_passkey
    except (KeyError, AttributeError):
        # Expected fields not present - this is normal for non-admin packets
        logger.debug(
            "Admin session passkey not extracted from packet (expected for non-admin packets): %s",
            asDict,
            exc_info=True,
        )


"""Well known message payloads can register decoders for automatic protobuf parsing"""
protocols = {
    portnums_pb2.PortNum.TEXT_MESSAGE_APP: KnownProtocol(
        "text", onReceive=_on_text_receive
    ),
    portnums_pb2.PortNum.RANGE_TEST_APP: KnownProtocol(
        "rangetest", onReceive=_on_text_receive
    ),
    portnums_pb2.PortNum.DETECTION_SENSOR_APP: KnownProtocol(
        "detectionsensor", onReceive=_on_text_receive
    ),
    portnums_pb2.PortNum.POSITION_APP: KnownProtocol(
        "position", mesh_pb2.Position, _on_position_receive
    ),
    portnums_pb2.PortNum.NODEINFO_APP: KnownProtocol(
        "user", mesh_pb2.User, _on_node_info_receive
    ),
    portnums_pb2.PortNum.ADMIN_APP: KnownProtocol(
        "admin", admin_pb2.AdminMessage, _on_admin_receive
    ),
    portnums_pb2.PortNum.ROUTING_APP: KnownProtocol("routing", mesh_pb2.Routing),
    portnums_pb2.PortNum.TELEMETRY_APP: KnownProtocol(
        "telemetry", telemetry_pb2.Telemetry, _on_telemetry_receive
    ),
    portnums_pb2.PortNum.REMOTE_HARDWARE_APP: KnownProtocol(
        "remotehw", remote_hardware_pb2.HardwareMessage
    ),
    portnums_pb2.PortNum.SIMULATOR_APP: KnownProtocol("simulator", mesh_pb2.Compressed),
    portnums_pb2.PortNum.TRACEROUTE_APP: KnownProtocol(
        "traceroute", mesh_pb2.RouteDiscovery
    ),
    portnums_pb2.PortNum.POWERSTRESS_APP: KnownProtocol(
        "powerstress", powermon_pb2.PowerStressMessage
    ),
    portnums_pb2.PortNum.WAYPOINT_APP: KnownProtocol("waypoint", mesh_pb2.Waypoint),
    portnums_pb2.PortNum.PAXCOUNTER_APP: KnownProtocol(
        "paxcounter", paxcount_pb2.Paxcount
    ),
    portnums_pb2.PortNum.STORE_FORWARD_APP: KnownProtocol(
        "storeforward", storeforward_pb2.StoreAndForward
    ),
    portnums_pb2.PortNum.NEIGHBORINFO_APP: KnownProtocol(
        "neighborinfo", mesh_pb2.NeighborInfo
    ),
    portnums_pb2.PortNum.MAP_REPORT_APP: KnownProtocol("mapreport", mqtt_pb2.MapReport),
}
