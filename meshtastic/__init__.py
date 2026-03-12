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

# ruff: noqa: F401

import copy
import logging
from importlib import import_module
from typing import Any, Callable, NamedTuple, TypeGuard, cast

from google.protobuf.json_format import MessageToJson

from meshtastic.node import Node
from meshtastic.util import (
    DeferredExecution,
    Timeout,
    catchAndIgnore,
    stripnl,
)

from . import util
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

pub = cast(Any, import_module("pubsub.pub"))

# Keep this module aligned with historical master behavior by intentionally not
# defining __all__. Public names remain available as module attributes.


def __getattr__(name: str) -> Any:
    """Provide lazy access to legacy module attributes.

    When the attribute "serial" is requested, import the third-party pyserial
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
        (e.g., the third-party pyserial module for "serial").

    Raises
    ------
    AttributeError
        If the requested attribute is not provided by this lazy loader.
    """
    # COMPAT_STABLE_SHIM: preserve historical `meshtastic.serial` module access.
    if name == "serial":
        # Keep historical `meshtastic.serial` access to the third-party
        # pyserial module as exposed on master.
        serial_module = import_module("serial")
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
"""Process-wide deferred publisher worker.

`DeferredExecution.queueWork()` is thread-safe (backed by Queue) and all
callbacks are serialized on the worker thread.
"""

logger = logging.getLogger(__name__)

REDACTED_TEXT = "<redacted>"
REDACTED_BYTES = b"<redacted>"


ResponseCallback = Callable[[dict[str, Any]], Any]
ProtobufFactory = Callable[[], Any]
OnReceive = Callable[[Any, dict[str, Any]], None]


class ResponseHandler(NamedTuple):
    """A pending response callback, waiting for a response to one of our messages."""

    # requestId: int - used only as a key
    #: a callable to call when a response is received
    callback: ResponseCallback
    #: Whether ACKs and NAKs should be passed to this handler
    ackPermitted: bool = False
    # FIXME, add timestamp and age out old requests


class KnownProtocol(NamedTuple):
    """Used to automatically decode known protocol payloads."""

    #: A descriptive name (e.g. "text", "user", "admin")
    name: str
    #: If set, will be called to parse as a protocol buffer
    protobufFactory: ProtobufFactory | None = None
    #: If set, invoked as onReceive(interface, packet)
    onReceive: OnReceive | None = None


def _packet_debug_summary(as_dict: dict[str, Any]) -> dict[str, Any]:
    """Return a sanitized packet summary for debug logging.

    The summary intentionally omits sensitive payload/body fields while retaining
    enough context for troubleshooting receive-handler flows.
    """
    decoded = as_dict.get("decoded")
    decoded_dict = decoded if isinstance(decoded, dict) else {}
    payload = decoded_dict.get("payload")
    payload_len = (
        len(payload)
        if isinstance(payload, (bytes, bytearray, memoryview, str))
        else None
    )
    decoded_keys = sorted(str(key) for key in decoded_dict.keys())
    return {
        "from": as_dict.get("from"),
        "to": as_dict.get("to"),
        "portnum": decoded_dict.get("portnum"),
        "decoded_keys": decoded_keys,
        "payload_len": payload_len,
    }


def _sanitize_last_received(as_dict: dict[str, Any]) -> dict[str, Any]:
    """Return a node-cache-safe packet copy for ``node['lastReceived']``.

    Keeps historical packet structure for compatibility while redacting only
    ``decoded.admin.raw.session_passkey`` when present.
    """
    sanitized = dict(as_dict)
    decoded = sanitized.get("decoded")
    if not isinstance(decoded, dict):
        return sanitized
    if "admin" not in decoded:
        return sanitized
    decoded_sanitized = dict(decoded)
    admin_payload = decoded.get("admin")
    if isinstance(admin_payload, dict):
        admin_sanitized = dict(admin_payload)
        raw_admin = admin_payload.get("raw")
        if isinstance(raw_admin, dict):
            raw_sanitized_dict = dict(raw_admin)
            if "session_passkey" in raw_sanitized_dict:
                raw_sanitized_dict["session_passkey"] = (
                    REDACTED_BYTES
                    if isinstance(
                        raw_sanitized_dict["session_passkey"],
                        (bytes, bytearray, memoryview),
                    )
                    else REDACTED_TEXT
                )
            admin_sanitized["raw"] = raw_sanitized_dict
        elif hasattr(raw_admin, "session_passkey"):
            raw_sanitized_obj: Any | None
            try:
                raw_sanitized_obj = copy.deepcopy(raw_admin)
            except Exception:  # noqa: BLE001 - preserve packet flow on odd payloads
                logger.debug(
                    "deepcopy failed during admin payload redaction; using sentinel payload",
                    exc_info=True,
                )
                raw_sanitized_obj = None
            if raw_sanitized_obj is None:
                admin_sanitized["raw"] = {"session_passkey": REDACTED_BYTES}
            else:
                try:
                    raw_sanitized_obj.session_passkey = REDACTED_BYTES
                except Exception:  # noqa: BLE001 - best effort redaction only
                    logger.debug(
                        "session_passkey assignment redaction failed; forcing fallback redaction",
                        exc_info=True,
                    )
                    redaction_applied = False
                    try:
                        delattr(raw_sanitized_obj, "session_passkey")
                        redaction_applied = True
                    except Exception:  # noqa: BLE001 - final fallback below
                        redaction_applied = False
                    admin_sanitized["raw"] = (
                        raw_sanitized_obj
                        if redaction_applied
                        else {"session_passkey": REDACTED_BYTES}
                    )
                else:
                    admin_sanitized["raw"] = raw_sanitized_obj
        decoded_sanitized["admin"] = admin_sanitized
    else:
        decoded_sanitized["admin"] = REDACTED_TEXT
    sanitized["decoded"] = decoded_sanitized
    return sanitized


def _is_valid_node_num(value: object) -> TypeGuard[int]:
    """Return True when value is an integer node number (excluding bool)."""
    return isinstance(value, int) and not isinstance(value, bool)


def _extract_sender_and_decoded(
    as_dict: dict[str, Any],
) -> tuple[int, dict[str, Any]] | None:
    """Return validated packet sender and decoded payload dictionary.

    Parameters
    ----------
    as_dict : dict[str, Any]
        Packet dictionary expected to contain an integer ``from`` and dict
        ``decoded`` payload.

    Returns
    -------
    tuple[int, dict[str, Any]] | None
        ``(sender, decoded)`` when both fields are present with expected types;
        otherwise ``None``.
    """
    sender = as_dict.get("from")
    decoded = as_dict.get("decoded")
    if not _is_valid_node_num(sender) or not isinstance(decoded, dict):
        return None
    return sender, decoded


def _on_text_receive(iface: Any, as_dict: dict[str, Any]) -> None:
    """Decode text payloads from a received packet and update per-node metadata.

    If the packet's decoded.payload contains valid UTF-8, store the decoded string in
    as_dict["decoded"]["text"]. If decoding fails, leave that field unset and log an error.
    Always invokes the interface's info-update path to refresh node metadata based on the packet.

    Parameters
    ----------
    iface : Any
        The interface instance that received the packet.
    as_dict : dict[str, Any]
        Packet dictionary expected to contain
        decoded.payload (bytes) where the text is stored.
    """
    # We don't throw if the utf8 is invalid in the text message.  Instead we just don't populate
    # the decoded.data.text and we log an error message.  This at least allows some delivery to
    # the app and the app can deal with the missing decoded representation.
    #
    # Usually btw this problem is caused by apps sending binary data but setting the payload type to
    # text.
    logger.debug("in _on_text_receive() %s", _packet_debug_summary(as_dict))
    try:
        as_bytes = as_dict["decoded"]["payload"]
        as_dict["decoded"]["text"] = as_bytes.decode("utf-8")
    except (UnicodeDecodeError, KeyError, AttributeError, TypeError):
        logger.exception("Malformatted utf8 in text message")
    _receive_info_update(iface, as_dict)


def _on_position_receive(iface: Any, as_dict: dict[str, Any]) -> None:
    """Update the sender node's stored position when a received packet contains position data.

    If as_dict contains a "from" field and a decoded "position", the position is normalized
    using the interface's fixup routine and written to that node's "position" entry.

    Parameters
    ----------
    iface : Any
        Interface instance that provides position normalization and node lookup helpers.
    as_dict : dict[str, Any]
        Packet dictionary expected to contain
        "from" and "decoded"->"position".
    """
    logger.debug("in _on_position_receive() %s", _packet_debug_summary(as_dict))
    packet_guard = _extract_sender_and_decoded(as_dict)
    if packet_guard is None:
        return
    sender, decoded = packet_guard
    if "position" in decoded:
        _receive_info_update(iface, as_dict)
        p = decoded["position"]
        if not isinstance(p, dict):
            logger.debug(
                "Skipping position update from=%s: unexpected payload type %s",
                sender,
                type(p).__name__,
            )
            return
        if "error" in p:
            logger.debug(
                "Skipping position state update from=%s due to decode error payload",
                sender,
            )
            return
        logger.debug("position payload received from=%s", sender)
        p = iface._fixup_position(p)
        logger.debug("position payload normalized from=%s", sender)
        node = iface._get_or_create_by_num(sender)
        with iface._node_db_lock:
            node["position"] = p


def _on_node_info_receive(iface: Any, as_dict: dict[str, Any]) -> None:
    """Update the local node record from a received NodeInfo ("user") payload.

    When `as_dict` contains a decoded `"user"` entry and a `"from"` sender, stores
    the decoded user protobuf on the sender's node under `node["user"]`, ensures
    `iface.nodes` maps the user's `id` to that node, and refreshes per-node metadata
    via _receive_info_update(iface, as_dict).

    Parameters
    ----------
    iface : Any
        Interface instance managing the node database.
    as_dict : dict[str, Any]
        Received packet dictionary; expected to contain
        `"decoded" -> "user"` and `"from"`.
    """
    logger.debug("in _on_node_info_receive() %s", _packet_debug_summary(as_dict))
    packet_guard = _extract_sender_and_decoded(as_dict)
    if packet_guard is None:
        return
    sender, decoded = packet_guard
    if "user" in decoded:
        _receive_info_update(iface, as_dict)
        p = decoded["user"]
        if not isinstance(p, dict):
            logger.debug(
                "Skipping user update from=%s: unexpected payload type %s",
                sender,
                type(p).__name__,
            )
            return
        if "error" in p:
            logger.debug(
                "Skipping user state update from=%s due to decode error payload",
                sender,
            )
            return
        # decode user protobufs and update nodedb, provide decoded version as "position" in the published msg
        # update node DB as needed
        n = iface._get_or_create_by_num(sender)
        with iface._node_db_lock:
            n["user"] = p
            # We now have a node ID, make sure it is up-to-date in that table
            node_id = p.get("id") if isinstance(p, dict) else None
            nodes_by_id = iface.nodes
            if isinstance(node_id, str) and node_id and isinstance(nodes_by_id, dict):
                nodes_by_id[node_id] = n


def _on_telemetry_receive(iface: Any, as_dict: dict[str, Any]) -> None:
    """Update the appropriate telemetry section on the sender node when a telemetry packet is received.

    Merges metrics from the packet's `decoded.telemetry` into one of the node's telemetry
    sections: `deviceMetrics`, `environmentMetrics`, `airQualityMetrics`, `powerMetrics`,
    or `localStats`. If the packet lacks a `from` field or none of these sections are
    present, no change is made.

    Parameters
    ----------
    iface : Any
        Interface instance used to look up or create the target node.
    as_dict : dict[str, Any]
        Received packet dictionary; expected to include
        a `from` key and may include `decoded.telemetry`.
    """
    logger.debug("in _on_telemetry_receive() %s", _packet_debug_summary(as_dict))
    packet_guard = _extract_sender_and_decoded(as_dict)
    if packet_guard is None:
        return
    sender, decoded = packet_guard
    _receive_info_update(iface, as_dict)

    to_update = None
    telemetry = decoded.get("telemetry") or {}
    if not isinstance(telemetry, dict):
        logger.debug(
            "Skipping telemetry update from=%s: unexpected telemetry payload type %s",
            as_dict.get("from"),
            type(telemetry).__name__,
        )
        return
    if "deviceMetrics" in telemetry:
        to_update = "deviceMetrics"
    elif "environmentMetrics" in telemetry:
        to_update = "environmentMetrics"
    elif "airQualityMetrics" in telemetry:
        to_update = "airQualityMetrics"
    elif "powerMetrics" in telemetry:
        to_update = "powerMetrics"
    elif "localStats" in telemetry:
        to_update = "localStats"
    else:
        return

    update_obj = telemetry.get(to_update)
    if not isinstance(update_obj, dict):
        return
    node = iface._get_or_create_by_num(sender)
    with iface._node_db_lock:
        new_metrics = node.get(to_update, {})
        if not isinstance(new_metrics, dict):
            new_metrics = {}
        new_metrics.update(update_obj)
        logger.debug("updating %s metrics for %s to %s", to_update, sender, new_metrics)
        node[to_update] = new_metrics


def _receive_info_update(iface: Any, as_dict: dict[str, Any]) -> None:
    """Update per-node metadata fields based on information present in a received packet dictionary.

    Parameters
    ----------
    iface : Any
        The interface instance whose node store will be updated.
    as_dict : dict[str, Any]
        Parsed packet dictionary; if it contains an integer "from" key, the
        node identified by that value will have these fields set:
        - lastReceived: packet dictionary copy with admin session passkey redacted
        - lastHeard: value of `rxTime` from the packet (or None)
        - snr: value of `rxSnr` from the packet (or None)
        - hopLimit: value of `hopLimit` from the packet (or None)
    """
    sender = as_dict.get("from")
    if not _is_valid_node_num(sender):
        if sender is not None:
            logger.debug(
                "Skipping receive info update due to non-integer sender type: %s",
                type(sender).__name__,
            )
        return
    node = iface._get_or_create_by_num(sender)
    sanitized_last_received = _sanitize_last_received(as_dict)
    with iface._node_db_lock:
        node["lastReceived"] = sanitized_last_received
        node["lastHeard"] = as_dict.get("rxTime")
        node["snr"] = as_dict.get("rxSnr")
        node["hopLimit"] = as_dict.get("hopLimit")


def _on_admin_receive(iface: Any, as_dict: dict[str, Any]) -> None:
    """Store the admin session passkey from an admin packet on the sending node.

    If the expected fields are present in `as_dict`, sets the sender node's
    "adminSessionPassKey" to the extracted `session_passkey`.

    Parameters
    ----------
    iface : Any
        The interface instance managing the node database.
    as_dict : dict[str, Any]
        Received packet dictionary; expected to contain
        `decoded.admin.raw.session_passkey` and a `from` sender field.
    """
    logger.debug("in _on_admin_receive() from=%s", as_dict.get("from"))
    packet_guard = _extract_sender_and_decoded(as_dict)
    if packet_guard is None:
        sender = as_dict.get("from")
        decoded = as_dict.get("decoded")
        if sender is None:
            logger.debug("Dropping admin packet because 'from' field is missing")
        elif not _is_valid_node_num(sender):
            logger.debug("Admin packet has invalid 'from' field type: %r", type(sender))
        elif not isinstance(decoded, dict):
            logger.debug("Admin packet missing decoded dict from=%s", sender)
        return

    sender, decoded = packet_guard
    _receive_info_update(iface, as_dict)

    admin_payload = decoded.get("admin")
    if not isinstance(admin_payload, dict):
        logger.debug("Admin packet missing admin payload dict from=%s", sender)
        return

    raw_admin = admin_payload.get("raw")
    session_passkey = getattr(raw_admin, "session_passkey", None)
    if session_passkey is None and isinstance(raw_admin, dict):
        session_passkey = raw_admin.get("session_passkey")

    if session_passkey is None:
        logger.debug(
            "Admin session passkey not extracted from admin packet from=%s",
            sender,
        )
        return

    node = iface._get_or_create_by_num(sender)
    with iface._node_db_lock:
        node["adminSessionPassKey"] = session_passkey


# Well known message payloads can register decoders for automatic protobuf parsing.
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
