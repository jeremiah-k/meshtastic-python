"""Mesh Interface class."""

# pylint: disable=R0917,C0302

import collections
import json
import logging
import math
import random
import secrets
import sys
import threading
import time
import traceback
from datetime import datetime
from decimal import Decimal
from typing import IO, Any, Callable, Dict, List, Optional, Type, Union

import google.protobuf.json_format

try:
    import print_color  # type: ignore[import-untyped]
except ImportError:
    print_color = None

from pubsub import pub  # type: ignore[import-untyped]
from tabulate import tabulate

import meshtastic.node
from meshtastic import (
    BROADCAST_ADDR,
    BROADCAST_NUM,
    LOCAL_ADDR,
    NODELESS_WANT_CONFIG_ID,
    ResponseHandler,
    protocols,
    publishingThread,
)
from meshtastic.protobuf import mesh_pb2, portnums_pb2, telemetry_pb2
from meshtastic.util import (
    Acknowledgment,
    Timeout,
    convert_mac_addr,
    message_to_json,
    our_exit,
    remove_keys_from_dict,
    stripnl,
)

logger = logging.getLogger(__name__)


def _timeago(delta_secs: int) -> str:
    """
    Produce a short human-readable relative time string for a past interval.

    Parameters
    ----------
        delta_secs (int): Number of seconds elapsed in the past; zero or negative values are treated as "now".

    Returns
    -------
        str: A compact relative time string such as "now", "30 sec ago", "1 hour ago", or "2 days ago".

    """
    intervals = (
        ("year", 60 * 60 * 24 * 365),
        ("month", 60 * 60 * 24 * 30),
        ("day", 60 * 60 * 24),
        ("hour", 60 * 60),
        ("min", 60),
        ("sec", 1),
    )
    for name, interval_duration in intervals:
        if delta_secs < interval_duration:
            continue
        x = delta_secs // interval_duration
        plur = "s" if x > 1 else ""
        return f"{x} {name}{plur} ago"

    return "now"


class MeshInterface:  # pylint: disable=R0902
    """Interface class for meshtastic devices.

    Properties:

    isConnected
    nodes
    debugOut
    """

    class MeshInterfaceError(Exception):
        """An exception class for general mesh interface errors."""

        def __init__(self, message: str) -> None:
            """
            Initialize the MeshInterfaceError with a human-readable message.

            Parameters
            ----------
                message (str): The error message to store on the exception and pass to the base Exception.

            """
            self.message = message
            super().__init__(self.message)

    def __init__(
        self,
        debugOut: Optional[Union[IO[str], Callable[[str], Any]]] = None,
        noProto: bool = False,
        noNodes: bool = False,
        timeout: int = 300,
    ) -> None:
        """
        Initialize the MeshInterface and configure runtime options.

        Parameters
        ----------
            debugOut (Optional[file-like or callable]): Destination for
                human-readable log lines; if provided the interface will
                publish device logs to this output.
            noProto (bool): If True, disable running the meshtastic protocol layer over the link (operate as a dumb serial client).
            noNodes (bool): If True, instruct the device not to send its node database on startup; only other configuration will be requested.
            timeout (int): Default timeout in seconds for operations that wait for replies.

        """
        self.debugOut = debugOut
        self.nodes: Optional[Dict[str, Dict]] = None  # FIXME
        self.isConnected: threading.Event = threading.Event()
        self.noProto: bool = noProto
        self.localNode: meshtastic.node.Node = meshtastic.node.Node(
            self, -1, timeout=timeout
        )  # We fixup nodenum later
        self.myInfo: Optional[mesh_pb2.MyNodeInfo] = (
            None  # We don't have device info yet
        )
        self.metadata: Optional[mesh_pb2.DeviceMetadata] = (
            None  # We don't have device metadata yet
        )
        self.responseHandlers: Dict[int, ResponseHandler] = (
            {}
        )  # A map from request ID to the handler
        self.failure: Optional[BaseException] = (
            None  # If we've encountered a fatal exception it will be kept here
        )
        self._timeout: Timeout = Timeout(maxSecs=timeout)
        self._acknowledgment: Acknowledgment = Acknowledgment()
        self.heartbeatTimer: Optional[threading.Timer] = None
        self._heartbeat_lock = threading.RLock()
        self._closing = False
        random.seed()  # FIXME, we should not clobber the random seedval here, instead tell user they must call it
        self.currentPacketId: int = random.randint(0, 0xFFFFFFFF)
        self.nodesByNum: Optional[Dict[int, Dict]] = None
        self.noNodes: bool = noNodes
        self.configId: Optional[int] = NODELESS_WANT_CONFIG_ID if noNodes else None
        self.gotResponse: bool = False  # used in gpio read
        self.mask: Optional[int] = None  # used in gpio read and gpio watch
        self.queueStatus: Optional[mesh_pb2.QueueStatus] = None
        self.queue: collections.OrderedDict = collections.OrderedDict()
        self._localChannels: List[Any] = []

        # We could have just not passed in debugOut to MeshInterface, and instead told consumers to subscribe to
        # the meshtastic.log.line publish instead.  Alas though changing that now would be a breaking API change
        # for any external consumers of the library.
        if debugOut:
            pub.subscribe(MeshInterface._printLogLine, "meshtastic.log.line")

    def close(self) -> None:
        """
        Shut down the interface, cancel any pending heartbeat, mark the interface as closing, and send a disconnect to the radio.
        """
        with self._heartbeat_lock:
            self._closing = True
            timer = self.heartbeatTimer
            self.heartbeatTimer = None
        if timer:
            timer.cancel()

        self._sendDisconnect()

    def __enter__(self) -> "MeshInterface":
        return self

    def __exit__(
        self,
        exc_type: Optional[Type[BaseException]],
        exc_value: Optional[BaseException],
        trace: Optional[Any],
    ) -> None:
        if exc_type is not None and exc_value is not None:
            logger.error(
                f"An exception of type {exc_type} with value {exc_value} has occurred"
            )
        if trace is not None:
            logger.error(f"Traceback:\n{''.join(traceback.format_tb(trace))}")
        self.close()

    @staticmethod
    def _printLogLine(line, interface):
        """Print a line of log output."""
        if print_color is not None and interface.debugOut == sys.stdout:
            # this isn't quite correct (could cause false positives), but currently our formatting differs between different log representations
            if "DEBUG" in line:
                print_color.print(line, color="cyan", end=None)  # type: ignore[call-arg]
            elif "INFO" in line:
                print_color.print(line, color="white", end=None)  # type: ignore[call-arg]
            elif "WARN" in line:
                print_color.print(line, color="yellow", end=None)  # type: ignore[call-arg]
            elif "ERR" in line:
                print_color.print(line, color="red", end=None)  # type: ignore[call-arg]
            else:
                print_color.print(line, end=None)  # type: ignore[call-arg]
        else:
            interface.debugOut.write(line + "\n")

    def _handleLogLine(self, line: str) -> None:
        """Handle a line of log output from the device."""

        # Devices should _not_ be including a newline at the end of each log-line str (especially when
        # encapsulated as a LogRecord).  But to cope with old device loads, we check for that and fix it here:
        if line.endswith("\n"):
            line = line[:-1]

        pub.sendMessage("meshtastic.log.line", line=line, interface=self)

    def _handleLogRecord(self, record: mesh_pb2.LogRecord) -> None:
        """
        Process a protobuf LogRecord by extracting its message text and handling it as a device log line.

        Parameters
        ----------
            record (mesh_pb2.LogRecord): Protobuf log record containing the `message` field to be processed.

        """
        # For now we just try to format the line as if it had come in over the serial port
        self._handleLogLine(record.message)

    def showInfo(self, file: Optional[IO[str]] = None) -> str:
        """
        Produce and print a human-readable summary of this mesh interface, including owner, local node info, metadata, and the set of known nodes.

        The summary formats node entries as JSON, removes internal keys
        (`raw`, `decoded`, `payload`) from node records, and normalizes stored
        MAC addresses into human-readable form.

        Parameters
        ----------
            file: A file-like object to which the summary is printed (defaults to sys.stdout).

        Returns
        -------
            summary (str): The full summary text that was printed.

        """
        owner = f"Owner: {self.getLongName()} ({self.getShortName()})"
        myinfo = ""
        if self.myInfo:
            myinfo = f"\nMy info: {message_to_json(self.myInfo)}"
        metadata = ""
        if self.metadata:
            metadata = f"\nMetadata: {message_to_json(self.metadata)}"
        mesh = "\n\nNodes in mesh: "
        nodes = {}
        if self.nodes:
            for n in self.nodes.values():
                # when the TBeam is first booted, it sometimes shows the raw data
                # so, we will just remove any raw keys
                keys_to_remove = ("raw", "decoded", "payload")
                n2 = remove_keys_from_dict(keys_to_remove, n)

                # if we have 'macaddr', re-format it
                if "macaddr" in n2["user"]:
                    val = n2["user"]["macaddr"]
                    # decode the base64 value
                    addr = convert_mac_addr(val)
                    n2["user"]["macaddr"] = addr

                # use id as dictionary key for correct json format in list of nodes
                nodeid = n2["user"]["id"]
                nodes[nodeid] = n2
        infos = owner + myinfo + metadata + mesh + json.dumps(nodes, indent=2)
        if file is None:
            file = sys.stdout
        print(infos, file=file)
        return infos

    def showNodes(
        self, includeSelf: bool = True, showFields: Optional[List[str]] = None
    ) -> str:  # pylint: disable=W0613
        """
        Produce a formatted table summarizing known mesh nodes.

        Parameters
        ----------
            includeSelf (bool): If False, omit the local node from the output.
            showFields (Optional[List[str]]): Ordered list of node fields to
                include (dotted paths for nested fields). If omitted or empty,
                a sensible default set of fields is used; the row-number column
                "N" is always included.

        Returns
        -------
            table (str): The rendered table string (also printed to stdout) containing one row per node and columns mapped to human-readable headings.

        """

        def get_human_readable(name: str) -> str:
            """
            Map an internal dotted field path to a human-readable column label.

            Parameters
            ----------
                name (str): Dotted field path or key to convert (e.g., "user.longName", "position.latitude").

            Returns
            -------
                str: A human-readable label for the given field when known (e.g., "User", "Latitude"); otherwise returns the original `name`.

            """
            name_map = {
                "user.longName": "User",
                "user.id": "ID",
                "user.shortName": "AKA",
                "user.hwModel": "Hardware",
                "user.publicKey": "Pubkey",
                "user.role": "Role",
                "position.latitude": "Latitude",
                "position.longitude": "Longitude",
                "position.altitude": "Altitude",
                "deviceMetrics.batteryLevel": "Battery",
                "deviceMetrics.channelUtilization": "Channel util.",
                "deviceMetrics.airUtilTx": "Tx air util.",
                "snr": "SNR",
                "hopsAway": "Hops",
                "channel": "Channel",
                "lastHeard": "LastHeard",
                "since": "Since",
                "isFavorite": "Fav",
            }

            if name in name_map:
                return name_map[name]  # Default to a formatted guess
            else:
                return name

        def formatFloat(value, precision=2, unit="") -> Optional[str]:
            """
            Format a numeric value into a fixed-precision string with an optional unit suffix.

            Parameters
            ----------
                value (float | int | None): The numeric value to format. Falsy inputs (e.g., None or 0) produce no output.
                precision (int): Number of digits after the decimal point. Defaults to 2.
                unit (str): Suffix to append to the formatted number (e.g., "V", "m"). Defaults to empty string.

            Returns
            -------
                str | None: `None` if `value` is falsy (for example `None` or `0`), otherwise the formatted string (e.g., "3.14V").

            """
            return f"{value:.{precision}f}{unit}" if value else None

        def getLH(ts) -> Optional[str]:
            """
            Format a Unix timestamp into a human-readable 'YYYY-MM-DD HH:MM:SS' string.

            Parameters
            ----------
                ts (float|int|None): Seconds since the Unix epoch. If falsy or None, no time is available.

            Returns
            -------
                last_heard (Optional[str]): Formatted timestamp string in `YYYY-MM-DD HH:MM:SS` form, or `None` if `ts` is falsy.

            """
            return (
                datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S") if ts else None
            )

        def getTimeAgo(ts) -> Optional[str]:
            """
            Produce a short human-readable relative time string for a Unix epoch timestamp.

            Parameters
            ----------
                ts (float|int|None): Unix timestamp in seconds since the epoch; if `None` no computation is performed.

            Returns
            -------
                Optional[str]: A concise relative time string like "now",
                    "5 sec ago", "2 min ago", etc., or `None` if `ts` is
                    `None` or represents a time in the future.

            """
            if ts is None:
                return None
            delta = datetime.now() - datetime.fromtimestamp(ts)
            delta_secs = int(delta.total_seconds())
            if delta_secs < 0:
                return None  # not handling a timestamp from the future
            return _timeago(delta_secs)

        def getNestedValue(node_dict: Dict[str, Any], key_path: str) -> Any:
            """
            Retrieve a value from a nested dict using a dotted key path.

            Parameters
            ----------
                node_dict (Dict[str, Any]): Dictionary to traverse.
                key_path (str): Dotted path (e.g., "a.b.c"); must contain at least one dot.

            Returns
            -------
                Any: The value at the nested path, or `None` if `key_path`
                    does not contain a dot, any intermediate key is missing,
                    or an intermediate value is not a dict.

            """
            if "." not in key_path:
                logger.debug("getNestedValue was called without a nested path.")
                return None
            keys = key_path.split(".")
            value: Optional[Union[str, dict]] = node_dict
            for key in keys:
                if isinstance(value, dict):
                    value = value.get(key)
                else:
                    return None
            return value

        if showFields is None or len(showFields) == 0:
            # The default set of fields to show (e.g., the status quo)
            showFields = [
                "N",
                "user.longName",
                "user.id",
                "user.shortName",
                "user.hwModel",
                "user.publicKey",
                "user.role",
                "position.latitude",
                "position.longitude",
                "position.altitude",
                "deviceMetrics.batteryLevel",
                "deviceMetrics.channelUtilization",
                "deviceMetrics.airUtilTx",
                "snr",
                "hopsAway",
                "channel",
                "isFavorite",
                "lastHeard",
                "since",
            ]
        else:
            # Always at least include the row number.
            showFields.insert(0, "N")

        rows: List[Dict[str, Any]] = []
        if self.nodesByNum:
            logger.debug(f"self.nodes:{self.nodes}")
            for node in self.nodesByNum.values():
                if not includeSelf and node["num"] == self.localNode.nodeNum:
                    continue

                presumptive_id = f"!{node['num']:08x}"

                # This allows the user to specify fields that wouldn't otherwise be included.
                fields = {}
                for field in showFields:
                    if "." in field:
                        raw_value = getNestedValue(node, field)
                    else:
                        # The "since" column is synthesized, it's not retrieved from the device. Get the
                        # lastHeard value here, and then we'll format it properly below.
                        if field == "since":
                            raw_value = node.get("lastHeard")
                        else:
                            raw_value = node.get(field)

                    formatted_value: Optional[str] = ""

                    # Some of these need special formatting or processing.
                    if field == "channel":
                        if raw_value is None:
                            formatted_value = "0"
                    elif field == "deviceMetrics.channelUtilization":
                        formatted_value = formatFloat(raw_value, 2, "%")
                    elif field == "deviceMetrics.airUtilTx":
                        formatted_value = formatFloat(raw_value, 2, "%")
                    elif field == "deviceMetrics.batteryLevel":
                        if raw_value in (0, 101):
                            formatted_value = "Powered"
                        else:
                            formatted_value = formatFloat(raw_value, 0, "%")
                    elif field == "isFavorite":
                        formatted_value = "*" if raw_value else ""
                    elif field == "lastHeard":
                        formatted_value = getLH(raw_value)
                    elif field == "position.latitude":
                        formatted_value = formatFloat(raw_value, 4, "°")
                    elif field == "position.longitude":
                        formatted_value = formatFloat(raw_value, 4, "°")
                    elif field == "position.altitude":
                        formatted_value = formatFloat(raw_value, 0, "m")
                    elif field == "since":
                        formatted_value = getTimeAgo(raw_value) or "N/A"
                    elif field == "snr":
                        formatted_value = formatFloat(raw_value, 0, " dB")
                    elif field == "user.shortName":
                        formatted_value = (
                            raw_value
                            if raw_value is not None
                            else f"Meshtastic {presumptive_id[-4:]}"
                        )
                    elif field == "user.id":
                        formatted_value = (
                            raw_value if raw_value is not None else presumptive_id
                        )
                    else:
                        formatted_value = raw_value  # No special formatting

                    fields[field] = formatted_value

                # Filter out any field in the data set that was not specified.
                filteredData = {
                    get_human_readable(k): v
                    for k, v in fields.items()
                    if k in showFields
                }
                rows.append(filteredData)

        rows.sort(key=lambda r: r.get("LastHeard") or "0000", reverse=True)
        for i, row in enumerate(rows):
            row["N"] = i + 1

        table = tabulate(rows, headers="keys", missingval="N/A", tablefmt="fancy_grid")
        print(table)
        return table

    def getNode(
        self,
        nodeId: str,
        requestChannels: bool = True,
        requestChannelAttempts: int = 3,
        timeout: int = 300,
    ) -> meshtastic.node.Node:
        """
        Obtain a Node object representing the device identified by nodeId.

        If nodeId is the local or broadcast address, returns the
        already-initialized local node. For remote nodes, this constructs a
        Node and, when requestChannels is True, requests channel information
        and retries up to requestChannelAttempts until channel data is received
        or the operation times out. On repeated failures to retrieve channel
        info, the function will terminate the process via our_exit.

        Parameters
        ----------
            nodeId (str): Node identifier (hex/node-id string, or LOCAL_ADDR/BROADCAST_ADDR).
            requestChannels (bool): If True, request channel settings from the remote node.
            requestChannelAttempts (int): Number of attempts to retrieve channel info before giving up.
            timeout (int): Timeout (in seconds) passed to the Node constructor and used while waiting for responses.

        Returns
        -------
            meshtastic.node.Node: The Node object corresponding to nodeId.

        """
        if nodeId in (LOCAL_ADDR, BROADCAST_ADDR):
            return self.localNode
        else:
            n = meshtastic.node.Node(self, nodeId, timeout=timeout)
            # Only request device settings and channel info when necessary
            if requestChannels:
                logger.debug("About to requestChannels")
                n.requestChannels()
                retries_left = requestChannelAttempts
                last_index: int = 0
                while retries_left > 0:
                    retries_left -= 1
                    if not n.waitForConfig():
                        new_index: int = (
                            len(n.partialChannels) if n.partialChannels else 0
                        )
                        # each time we get a new channel, reset the counter
                        if new_index != last_index:
                            retries_left = requestChannelAttempts - 1
                        if retries_left <= 0:
                            our_exit("Error: Timed out waiting for channels, giving up")
                        print("Timed out trying to retrieve channel info, retrying")
                        n.requestChannels(startingIndex=new_index)
                        last_index = new_index
                    else:
                        break
            return n

    def sendText(
        self,
        text: str,
        destinationId: Union[int, str] = BROADCAST_ADDR,
        wantAck: bool = False,
        wantResponse: bool = False,
        onResponse: Optional[Callable[[dict], Any]] = None,
        channelIndex: int = 0,
        portNum: portnums_pb2.PortNum.ValueType = portnums_pb2.PortNum.TEXT_MESSAGE_APP,
        replyId: Optional[int] = None,
    ) -> mesh_pb2.MeshPacket:
        """Send a utf8 string to some other node, if the node has a display it
           will also be shown on the device.

        Arguments:
            text {string} -- The text to send

        Keyword Arguments:
            destinationId {nodeId or nodeNum} -- where to send this
                                                 message (default: {BROADCAST_ADDR})
            wantAck -- True if you want the message sent in a reliable manner
                       (with retries and ack/nak provided for delivery)
            wantResponse -- True if you want the service on the other side to
                            send an application layer response
            portNum -- the application portnum (similar to IP port numbers)
                       of the destination, see portnums.proto for a list
            replyId -- the ID of the message that this packet is a response to

        Returns the sent packet. The id field will be populated in this packet
        and can be used to track future message acks/naks.

        """

        return self.sendData(
            text.encode("utf-8"),
            destinationId,
            portNum=portNum,
            wantAck=wantAck,
            wantResponse=wantResponse,
            onResponse=onResponse,
            channelIndex=channelIndex,
            replyId=replyId,
        )

    def sendAlert(
        self,
        text: str,
        destinationId: Union[int, str] = BROADCAST_ADDR,
        onResponse: Optional[Callable[[dict], Any]] = None,
        channelIndex: int = 0,
    ) -> mesh_pb2.MeshPacket:
        """
        Send a high-priority alert text to a node, which may trigger special notifications on clients.

        Parameters
        ----------
            text (str): Alert text to send.
            destinationId (int | str): Node ID or node number to receive the alert (defaults to broadcast).
            onResponse (Optional[Callable[[dict], Any]]): Optional callback invoked if a response is received for this message.
            channelIndex (int): Channel index to use when sending.

        Returns
        -------
            mesh_pb2.MeshPacket: The sent mesh packet with its `id` populated.

        """

        return self.sendData(
            text.encode("utf-8"),
            destinationId,
            portNum=portnums_pb2.PortNum.ALERT_APP,
            wantAck=False,
            wantResponse=False,
            onResponse=onResponse,
            channelIndex=channelIndex,
            priority=mesh_pb2.MeshPacket.Priority.ALERT,
        )

    def sendMqttClientProxyMessage(self, topic: str, data: bytes) -> None:
        """
        Send an MQTT Client Proxy message to the radio.

        Parameters
        ----------
                topic (str): MQTT topic string to forward to the radio.
                data (bytes): MQTT message payload to forward.

        """
        prox = mesh_pb2.MqttClientProxyMessage()
        prox.topic = topic
        prox.data = data
        toRadio = mesh_pb2.ToRadio()
        toRadio.mqttClientProxyMessage.CopyFrom(prox)
        self._sendToRadio(toRadio)

    def sendData(  # pylint: disable=R0913
        self,
        data: Any,
        destinationId: Union[int, str] = BROADCAST_ADDR,
        portNum: portnums_pb2.PortNum.ValueType = portnums_pb2.PortNum.PRIVATE_APP,
        wantAck: bool = False,
        wantResponse: bool = False,
        onResponse: Optional[Callable[[dict], Any]] = None,
        onResponseAckPermitted: bool = False,
        channelIndex: int = 0,
        hopLimit: Optional[int] = None,
        pkiEncrypted: Optional[bool] = False,
        publicKey: Optional[bytes] = None,
        priority: mesh_pb2.MeshPacket.Priority.ValueType = mesh_pb2.MeshPacket.Priority.RELIABLE,
        replyId: Optional[int] = None,
    ) -> mesh_pb2.MeshPacket:
        """
        Send a data packet to a mesh node.

        Parameters
        ----------
                data (bytes or protobuf): Payload to send; protobuf objects will be serialized.
                destinationId (int|str): Destination node identifier (node ID string, numeric node number, BROADCAST_ADDR, or LOCAL_ADDR).
                portNum (int): Application port number for the payload.
                wantAck (bool): Request reliable delivery with retries and ack/nak.
                wantResponse (bool): Request an application-layer response from the recipient.
                onResponse (callable[[dict], Any] | None): Callback invoked when a response or NAK/ACK (if permitted) for this packet is received.
                onResponseAckPermitted (bool): If True, invoke onResponse for regular ACKs as well as data responses and NAKs.
                channelIndex (int): Channel index to send on.
                hopLimit (int | None): Maximum hop count for the packet; None uses the default.
                pkiEncrypted (bool | None): If True, attempt PKI encryption for the payload.
                publicKey (bytes | None): Public key to use for PKI encryption when pkiEncrypted is True.
                priority (int | None): Packet priority enum value to assign to the packet.
                replyId (int | None): If provided, marks this packet as a reply to the given message ID.

        Returns
        -------
                mesh_pb2.MeshPacket: The sent packet with its `id` field populated.

        Raises
        ------
                MeshInterface.MeshInterfaceError: If the payload exceeds the maximum allowed size or a packet id cannot be generated.

        """

        if getattr(data, "SerializeToString", None):
            logger.debug(f"Serializing protobuf as data: {stripnl(data)}")
            data = data.SerializeToString()

        logger.debug(f"len(data): {len(data)}")
        logger.debug(
            f"mesh_pb2.Constants.DATA_PAYLOAD_LEN: {mesh_pb2.Constants.DATA_PAYLOAD_LEN}"
        )
        if len(data) > mesh_pb2.Constants.DATA_PAYLOAD_LEN:
            raise MeshInterface.MeshInterfaceError("Data payload too big")

        if (
            portNum == portnums_pb2.PortNum.UNKNOWN_APP
        ):  # we are now more strict wrt port numbers
            our_exit("Warning: A non-zero port number must be specified")

        meshPacket = mesh_pb2.MeshPacket()
        meshPacket.channel = channelIndex
        meshPacket.decoded.payload = data
        meshPacket.decoded.portnum = portNum
        meshPacket.decoded.want_response = wantResponse
        meshPacket.id = self._generatePacketId()
        if replyId is not None:
            meshPacket.decoded.reply_id = replyId
        if priority is not None:
            meshPacket.priority = priority

        if onResponse is not None:
            logger.debug(f"Setting a response handler for requestId {meshPacket.id}")
            self._addResponseHandler(
                meshPacket.id, onResponse, ackPermitted=onResponseAckPermitted
            )
        p = self._sendPacket(
            meshPacket,
            destinationId,
            wantAck=wantAck,
            hopLimit=hopLimit,
            pkiEncrypted=pkiEncrypted,
            publicKey=publicKey,
        )
        return p

    def sendPosition(
        self,
        latitude: float = 0.0,
        longitude: float = 0.0,
        altitude: int = 0,
        destinationId: Union[int, str] = BROADCAST_ADDR,
        wantAck: bool = False,
        wantResponse: bool = False,
        channelIndex: int = 0,
    ) -> mesh_pb2.MeshPacket:
        """
        Send a position packet to a node (defaults to broadcast) and optionally wait for a response.

        The device will use this packet to update its notion of local position.

        Parameters
        ----------
            latitude (float): Latitude in degrees. If 0.0 the latitude field is omitted.
            longitude (float): Longitude in degrees. If 0.0 the longitude field is omitted.
            altitude (int): Altitude in meters. If 0 the altitude field is omitted.
            destinationId (int | str): Destination address or node ID; defaults to broadcast.
            wantAck (bool): Request an acknowledgment from the recipient.
            wantResponse (bool): If True, block until a position response is received.
            channelIndex (int): Channel index to send the packet on.

        Returns
        -------
            mesh_pb2.MeshPacket: The sent packet with its `id` populated.

        """
        p = mesh_pb2.Position()
        if latitude != 0.0:
            p.latitude_i = int(latitude / 1e-7)
            logger.debug(f"p.latitude_i:{p.latitude_i}")

        if longitude != 0.0:
            p.longitude_i = int(longitude / 1e-7)
            logger.debug(f"p.longitude_i:{p.longitude_i}")

        if altitude != 0:
            p.altitude = int(altitude)
            logger.debug(f"p.altitude:{p.altitude}")

        if wantResponse:
            onResponse = self.onResponsePosition
        else:
            onResponse = None

        d = self.sendData(
            p,
            destinationId,
            portNum=portnums_pb2.PortNum.POSITION_APP,
            wantAck=wantAck,
            wantResponse=wantResponse,
            onResponse=onResponse,
            channelIndex=channelIndex,
        )
        if wantResponse:
            self.waitForPosition()
        return d

    def onResponsePosition(self, p):
        """
        Handle an incoming position response payload.

        Parses the Position protobuf from the decoded packet payload, marks the
        interface's position acknowledgment as received, and prints a concise
        human-readable summary including latitude/longitude (in degrees),
        altitude (meters) when present, and precision information. If the
        packet is a routing response indicating "NO_RESPONSE", terminates with
        a message about the required firmware version.

        Parameters
        ----------
            p (dict): A decoded packet dictionary containing at minimum the
                keys `"decoded"`, `"decoded"]["portnum"`, and
                `"decoded"]["payload"`; for routing responses it must include
                `"decoded"]["routing"]["errorReason"`.

        """
        if p["decoded"]["portnum"] == "POSITION_APP":
            self._acknowledgment.receivedPosition = True
            position = mesh_pb2.Position()
            position.ParseFromString(p["decoded"]["payload"])

            ret = "Position received: "
            if position.latitude_i != 0 and position.longitude_i != 0:
                ret += (
                    f"({position.latitude_i * 10**-7}, {position.longitude_i * 10**-7})"
                )
            else:
                ret += "(unknown)"
            if position.altitude != 0:
                ret += f" {position.altitude}m"

            if position.precision_bits not in [0, 32]:
                ret += f" precision:{position.precision_bits}"
            elif position.precision_bits == 32:
                ret += " full precision"
            elif position.precision_bits == 0:
                ret += " position disabled"

            print(ret)

        elif p["decoded"]["portnum"] == "ROUTING_APP":
            if p["decoded"]["routing"]["errorReason"] == "NO_RESPONSE":
                our_exit(
                    "No response from node. At least firmware 2.1.22 is required on the destination node."
                )

    def sendTraceRoute(
        self, dest: Union[int, str], hopLimit: int, channelIndex: int = 0
    ) -> None:
        """
        Initiate a traceroute toward a destination node.

        Sends a RouteDiscovery packet to the given destination and waits for
        trace route responses; the wait timeout is extended based on the local
        node count but capped by hopLimit.

        Parameters
        ----------
            dest (int | str): Destination node (numeric node number, node ID string, or broadcast/local constants).
            hopLimit (int): Maximum number of hops to probe for the trace route.
            channelIndex (int): Channel index to use for transmission.

        Raises
        ------
            MeshInterfaceError: If waiting for the trace route response times out or the operation fails.

        """
        r = mesh_pb2.RouteDiscovery()
        self.sendData(
            r,
            destinationId=dest,
            portNum=portnums_pb2.PortNum.TRACEROUTE_APP,
            wantResponse=True,
            onResponse=self.onResponseTraceRoute,
            channelIndex=channelIndex,
            hopLimit=hopLimit,
        )
        # extend timeout based on number of nodes, limit by configured hopLimit
        waitFactor = min(len(self.nodes) - 1 if self.nodes else 0, hopLimit)
        self.waitForTraceRoute(waitFactor)

    def onResponseTraceRoute(self, p: dict):
        """
        Handle a route-trace response and display human-readable forward and return paths.

        Parses a RouteDiscovery payload from p["decoded"]["payload"], prints a formatted
        route from the response destination back to the origin and, when present,
        prints the traced route back to the requester. Marks the interface acknowledgment
        flag for trace-route reception.

        Parameters
        ----------
            p (dict): Received mesh packet dictionary. Expected keys:
                - "decoded": a dict containing a "payload" field with the RouteDiscovery
                  protobuf serialized bytes.
                - "from": numeric node ID of the packet sender.
                - "to": numeric node ID of the packet destination.
                - "hopStart" (optional): present when a return route is included.

        """
        UNK_SNR = -128  # Value representing unknown SNR

        routeDiscovery = mesh_pb2.RouteDiscovery()
        routeDiscovery.ParseFromString(p["decoded"]["payload"])
        asDict = google.protobuf.json_format.MessageToDict(routeDiscovery)

        def _node_label(node_num: int) -> str:
            """
            Return a human-readable label for a node number.

            Parameters
            ----------
                node_num (int): Numeric node identifier.

            Returns
            -------
                str: The node's ID string if known, otherwise the node number formatted as an 8-digit hexadecimal string.

            """
            return self._nodeNumToId(node_num, False) or f"{node_num:08x}"

        def _format_snr(snr_value: Optional[int]) -> str:
            """
            Format an SNR value encoded in quarter-dB units into a human-readable string.

            Parameters
            ----------
                snr_value (Optional[int]): SNR expressed as an integer number of quarter dB units,
                    or `None`/`UNK_SNR` to indicate an unknown value.

            Returns
            -------
                Formatted SNR in dB as a string (for example, "7.5"), or "?" when the SNR is unknown.

            """
            return (
                str(snr_value / 4)
                if snr_value is not None and snr_value != UNK_SNR
                else "?"
            )

        def _append_hop(route_text: str, node_num: int, snr_text: str) -> str:
            """
            Append a hop description to an existing route string.

            Parameters
            ----------
                route_text (str): Current route representation.
                node_num (int): Numeric node identifier to be converted to a label.
                snr_text (str): Signal-to-noise ratio text to display (unit omitted).

            Returns
            -------
                str: The route string with " --> <node_label> (<snr_text>dB)" appended.

            """
            return f"{route_text} --> {_node_label(node_num)} ({snr_text}dB)"

        route_towards = asDict.get("route", [])
        snr_towards = asDict.get("snrTowards", [])
        snr_towards_valid = len(snr_towards) == len(route_towards) + 1

        print("Route traced towards destination:")
        routeStr = _node_label(p["to"])  # Start with destination of response
        for idx, nodeNum in enumerate(route_towards):  # Add intermediate hops
            hop_snr = _format_snr(snr_towards[idx]) if snr_towards_valid else "?"
            routeStr = _append_hop(routeStr, nodeNum, hop_snr)
        final_towards_snr = _format_snr(snr_towards[-1]) if snr_towards_valid else "?"
        routeStr = _append_hop(routeStr, p["from"], final_towards_snr)

        print(routeStr)  # Print the route towards destination

        # Only if hopStart is set and there is an SNR entry (for the origin) it's valid, even though route might be empty (direct connection)
        route_back = asDict.get("routeBack", [])
        snr_back = asDict.get("snrBack", [])
        backValid = "hopStart" in p and len(snr_back) == len(route_back) + 1
        if backValid:
            print("Route traced back to us:")
            routeStr = _node_label(p["from"])  # Start with origin of response
            for idx, nodeNum in enumerate(route_back):  # Add intermediate hops
                routeStr = _append_hop(routeStr, nodeNum, _format_snr(snr_back[idx]))
            routeStr = _append_hop(routeStr, p["to"], _format_snr(snr_back[-1]))
            print(routeStr)  # Print the route back to us

        self._acknowledgment.receivedTraceRoute = True

    def sendTelemetry(
        self,
        destinationId: Union[int, str] = BROADCAST_ADDR,
        wantResponse: bool = False,
        channelIndex: int = 0,
        telemetryType: str = "device_metrics",
    ) -> None:
        """
        Send a telemetry request or announcement and optionally wait for a reply.

        Parameters
        ----------
                destinationId (int | str): Node ID or broadcast address to send the telemetry to.
                wantResponse (bool): If true, register a telemetry response handler and block until a response is received.
                channelIndex (int): Channel index to use for the outgoing packet.
                telemetryType (str): Type of telemetry payload to send. Supported values:
                        "environment_metrics", "air_quality_metrics", "power_metrics", "local_stats",
                        and "device_metrics" (the default). When "device_metrics" is chosen, the method
                        populates the payload from the local node's cached device metrics when available.

        """
        r = telemetry_pb2.Telemetry()

        if telemetryType == "environment_metrics":
            r.environment_metrics.CopyFrom(telemetry_pb2.EnvironmentMetrics())
        elif telemetryType == "air_quality_metrics":
            r.air_quality_metrics.CopyFrom(telemetry_pb2.AirQualityMetrics())
        elif telemetryType == "power_metrics":
            r.power_metrics.CopyFrom(telemetry_pb2.PowerMetrics())
        elif telemetryType == "local_stats":
            r.local_stats.CopyFrom(telemetry_pb2.LocalStats())
        else:  # fall through to device metrics
            if self.nodesByNum is not None:
                node = self.nodesByNum.get(self.localNode.nodeNum)
                if node is not None:
                    metrics = node.get("deviceMetrics")
                    if metrics:
                        batteryLevel = metrics.get("batteryLevel")
                        if batteryLevel is not None:
                            r.device_metrics.battery_level = batteryLevel
                        voltage = metrics.get("voltage")
                        if voltage is not None:
                            r.device_metrics.voltage = voltage
                        channel_utilization = metrics.get("channelUtilization")
                        if channel_utilization is not None:
                            r.device_metrics.channel_utilization = channel_utilization
                        air_util_tx = metrics.get("airUtilTx")
                        if air_util_tx is not None:
                            r.device_metrics.air_util_tx = air_util_tx
                        uptime_seconds = metrics.get("uptimeSeconds")
                        if uptime_seconds is not None:
                            r.device_metrics.uptime_seconds = uptime_seconds

        if wantResponse:
            onResponse = self.onResponseTelemetry
        else:
            onResponse = None

        self.sendData(
            r,
            destinationId=destinationId,
            portNum=portnums_pb2.PortNum.TELEMETRY_APP,
            wantResponse=wantResponse,
            onResponse=onResponse,
            channelIndex=channelIndex,
        )
        if wantResponse:
            self.waitForTelemetry()

    def onResponseTelemetry(self, p: dict):
        """
        Handle an incoming telemetry response packet.

        Parses the packet's Telemetry protobuf, marks telemetry as received, and prints human-readable telemetry fields.
        If the packet contains a routing error indicating `NO_RESPONSE`, exits with a firmware-requirement message.

        Parameters
        ----------
            p (dict): Decoded packet dictionary as produced by _handlePacketFromRadio, expected to contain
                "decoded" with keys "portnum" and either "payload" (for TELEMETRY_APP) or "routing" (for ROUTING_APP).

        """
        if p["decoded"]["portnum"] == "TELEMETRY_APP":
            self._acknowledgment.receivedTelemetry = True
            telemetry = telemetry_pb2.Telemetry()
            telemetry.ParseFromString(p["decoded"]["payload"])
            print("Telemetry received:")
            # Check if the telemetry message has the device_metrics field
            # This is the original code that was the default for --request-telemetry and is kept for compatibility
            if telemetry.HasField("device_metrics"):
                if telemetry.device_metrics.battery_level is not None:
                    print(
                        f"Battery level: {telemetry.device_metrics.battery_level:.2f}%"
                    )
                if telemetry.device_metrics.voltage is not None:
                    print(f"Voltage: {telemetry.device_metrics.voltage:.2f} V")
                if telemetry.device_metrics.channel_utilization is not None:
                    print(
                        f"Total channel utilization: {telemetry.device_metrics.channel_utilization:.2f}%"
                    )
                if telemetry.device_metrics.air_util_tx is not None:
                    print(
                        f"Transmit air utilization: {telemetry.device_metrics.air_util_tx:.2f}%"
                    )
                if telemetry.device_metrics.uptime_seconds is not None:
                    print(f"Uptime: {telemetry.device_metrics.uptime_seconds} s")
            else:
                # this is the new code if --request-telemetry <type> is used.
                telemetry_dict = google.protobuf.json_format.MessageToDict(telemetry)
                for key, value in telemetry_dict.items():
                    if (
                        key != "time"
                    ):  # protobuf includes a time field that we don't print for device_metrics.
                        print(f"{key}:")
                        for sub_key, sub_value in value.items():
                            print(f"  {sub_key}: {sub_value}")

        elif p["decoded"]["portnum"] == "ROUTING_APP":
            if p["decoded"]["routing"]["errorReason"] == "NO_RESPONSE":
                our_exit(
                    "No response from node. At least firmware 2.1.22 is required on the destination node."
                )

    def onResponseWaypoint(self, p: dict):
        """
        Handle an incoming waypoint response packet.

        If the packet is a WAYPOINT_APP response, mark the waypoint
        acknowledgment as received, parse the Waypoint protobuf from
        decoded['payload'], and print a human-readable representation. If the
        packet is a ROUTING_APP response with routing['errorReason'] ==
        "NO_RESPONSE", exit with a message indicating a minimum firmware
        requirement.

        Parameters
        ----------
            p (dict): Packet dictionary with a 'decoded' mapping that contains at least:
                - 'portnum' (str): Port name such as "WAYPOINT_APP" or "ROUTING_APP".
                - 'payload' (bytes): Serialized Waypoint protobuf (when portnum is WAYPOINT_APP).
                - 'routing' (dict): Routing info including 'errorReason' (when portnum is ROUTING_APP).

        """
        if p["decoded"]["portnum"] == "WAYPOINT_APP":
            self._acknowledgment.receivedWaypoint = True
            w = mesh_pb2.Waypoint()
            w.ParseFromString(p["decoded"]["payload"])
            print(f"Waypoint received: {w}")
        elif p["decoded"]["portnum"] == "ROUTING_APP":
            if p["decoded"]["routing"]["errorReason"] == "NO_RESPONSE":
                our_exit(
                    "No response from node. At least firmware 2.1.22 is required on the destination node."
                )

    def sendWaypoint(  # pylint: disable=R0913
        self,
        name: str,
        description: str,
        icon: Union[int, str],
        expire: int,
        waypoint_id: Optional[int] = None,
        latitude: float = 0.0,
        longitude: float = 0.0,
        destinationId: Union[int, str] = BROADCAST_ADDR,
        wantAck: bool = True,
        wantResponse: bool = False,
        channelIndex: int = 0,
    ) -> mesh_pb2.MeshPacket:
        """
        Send a waypoint to a node (usually broadcast) and return the sent MeshPacket.

        Parameters
        ----------
                waypoint_id (Optional[int]): If provided, use this waypoint id; otherwise a random id will be generated.
                wantResponse (bool): If True, block until a waypoint response is received.
                latitude (float): Latitude in decimal degrees.
                longitude (float): Longitude in decimal degrees.

        Returns
        -------
                The sent MeshPacket with its `id` populated for tracking acknowledgments.

        """
        w = mesh_pb2.Waypoint()
        w.name = name
        w.description = description
        w.icon = int(icon)
        w.expire = expire
        if waypoint_id is None:
            # Generate a waypoint's id, NOT a packet ID.
            # same algorithm as https://github.com/meshtastic/js/blob/715e35d2374276a43ffa93c628e3710875d43907/src/meshDevice.ts#L791
            seed = secrets.randbits(32)
            w.id = math.floor(seed * math.pow(2, -32) * 1e9)
            logger.debug(f"w.id:{w.id}")
        else:
            w.id = waypoint_id
        if latitude != 0.0:
            w.latitude_i = int(latitude * 1e7)
            logger.debug(f"w.latitude_i:{w.latitude_i}")
        if longitude != 0.0:
            w.longitude_i = int(longitude * 1e7)
            logger.debug(f"w.longitude_i:{w.longitude_i}")

        if wantResponse:
            onResponse = self.onResponseWaypoint
        else:
            onResponse = None

        d = self.sendData(
            w,
            destinationId,
            portNum=portnums_pb2.PortNum.WAYPOINT_APP,
            wantAck=wantAck,
            wantResponse=wantResponse,
            onResponse=onResponse,
            channelIndex=channelIndex,
        )
        if wantResponse:
            self.waitForWaypoint()
        return d

    def deleteWaypoint(
        self,
        waypoint_id: int,
        destinationId: Union[int, str] = BROADCAST_ADDR,
        wantAck: bool = True,
        wantResponse: bool = False,
        channelIndex: int = 0,
    ) -> mesh_pb2.MeshPacket:
        """
        Send a deletion for a waypoint identified by `waypoint_id` to a destination node.

        Parameters
        ----------
            waypoint_id (int): The waypoint's identifier to delete (must be the waypoint's id, not a packet id).
            destinationId (int | str): Destination node identifier or address (defaults to broadcast).
            wantResponse (bool): If True, wait for and process a waypoint response before returning.
            wantAck (bool): If True, request an acknowledgement for the sent packet.
            channelIndex (int): Channel index to send the packet on.

        Returns
        -------
            mesh_pb2.MeshPacket: The MeshPacket that was sent; its `id` field will be populated and can be used to track acks/naks.

        """
        p = mesh_pb2.Waypoint()
        p.id = waypoint_id
        p.expire = 0

        if wantResponse:
            onResponse = self.onResponseWaypoint
        else:
            onResponse = None

        d = self.sendData(
            p,
            destinationId,
            portNum=portnums_pb2.PortNum.WAYPOINT_APP,
            wantAck=wantAck,
            wantResponse=wantResponse,
            onResponse=onResponse,
            channelIndex=channelIndex,
        )
        if wantResponse:
            self.waitForWaypoint()
        return d

    def _addResponseHandler(
        self,
        requestId: int,
        callback: Callable[[dict], Any],
        ackPermitted: bool = False,
    ):
        self.responseHandlers[requestId] = ResponseHandler(
            callback=callback, ackPermitted=ackPermitted
        )

    def _sendPacket(
        self,
        meshPacket: mesh_pb2.MeshPacket,
        destinationId: Union[int, str] = BROADCAST_ADDR,
        wantAck: bool = False,
        hopLimit: Optional[int] = None,
        pkiEncrypted: Optional[bool] = False,
        publicKey: Optional[bytes] = None,
    ):
        """
        Send a MeshPacket to a specific node or broadcast.

        Parameters
        ----------
            meshPacket (mesh_pb2.MeshPacket): The packet to send; fields such as payload and port should be set by the caller.
            destinationId (int|str): Destination identifier — can be a node
                number (int), a node ID string (hex-style), or the constants
                BROADCAST_ADDR or LOCAL_ADDR. Determines the packet's `to`
                field.
            wantAck (bool): If true, request an acknowledgement from the recipient; sets the packet's `want_ack`.
            hopLimit (int | None): Maximum hop count for the packet. If omitted, the local node's LoRa `hop_limit` is used.
            pkiEncrypted (bool | None): If true, mark the packet as PKI-encrypted.
            publicKey (bytes | None): Optional public key to include on the packet.

        Returns
        -------
            mesh_pb2.MeshPacket: The same MeshPacket instance that was sent,
                with transport-related fields populated (for example: `to`,
                `want_ack`, `hop_limit`, `id`, and encryption/public key
                fields).

        """

        # We allow users to talk to the local node before we've completed the full connection flow...
        if self.myInfo is not None and destinationId != self.myInfo.my_node_num:
            self._waitConnected()

        toRadio = mesh_pb2.ToRadio()

        nodeNum: int = 0
        if destinationId is None:
            our_exit("Warning: destinationId must not be None")
        elif isinstance(destinationId, int):
            nodeNum = destinationId
        elif destinationId == BROADCAST_ADDR:
            nodeNum = BROADCAST_NUM
        elif destinationId == LOCAL_ADDR:
            if self.myInfo:
                nodeNum = self.myInfo.my_node_num
            else:
                our_exit("Warning: No myInfo found.")
        # A simple hex style nodeid - we can parse this without needing the DB
        elif isinstance(destinationId, str) and len(destinationId) >= 8:
            # assuming some form of node id string such as !1234578 or 0x12345678
            # always grab the last 8 items of the hexadecimal id str and parse to integer
            nodeNum = int(destinationId[-8:], 16)
        else:
            if self.nodes:
                node = self.nodes.get(destinationId)
                if node is None:
                    our_exit(f"Warning: NodeId {destinationId} not found in DB")
                else:
                    nodeNum = node["num"]
            else:
                logger.warning("Warning: There were no self.nodes.")

        meshPacket.to = nodeNum
        meshPacket.want_ack = wantAck

        if hopLimit is not None:
            meshPacket.hop_limit = hopLimit
        else:
            loraConfig = self.localNode.localConfig.lora
            meshPacket.hop_limit = loraConfig.hop_limit

        if pkiEncrypted:
            meshPacket.pki_encrypted = True

        if publicKey is not None:
            meshPacket.public_key = publicKey

        # if the user hasn't set an ID for this packet (likely and recommended),
        # we should pick a new unique ID so the message can be tracked.
        if meshPacket.id == 0:
            meshPacket.id = self._generatePacketId()

        toRadio.packet.CopyFrom(meshPacket)
        if self.noProto:
            logger.warning(
                "Not sending packet because protocol use is disabled by noProto"
            )
        else:
            logger.debug(f"Sending packet: {stripnl(meshPacket)}")
            self._sendToRadio(toRadio)
        return meshPacket

    def waitForConfig(self):
        """
        Block until the radio configuration and the local node's configuration are available.

        Raises:
            MeshInterface.MeshInterfaceError: If the configuration is not received before the interface timeout.

        """
        success = (
            self._timeout.waitForSet(self, attrs=("myInfo", "nodes"))
            and self.localNode.waitForConfig()
        )
        if not success:
            raise MeshInterface.MeshInterfaceError(
                "Timed out waiting for interface config"
            )

    def waitForAckNak(self):
        """
        Block until an acknowledgement (ACK) or negative acknowledgement (NAK) is received.

        Raises:
            MeshInterface.MeshInterfaceError: If waiting times out before an ACK/NAK is received.

        """
        success = self._timeout.waitForAckNak(self._acknowledgment)
        if not success:
            raise MeshInterface.MeshInterfaceError(
                "Timed out waiting for an acknowledgment"
            )

    def waitForTraceRoute(self, waitFactor):
        """
        Wait for trace route completion using the configured timeout.

        Blocks until a trace route response is acknowledged or the configured timeout multiplied by waitFactor elapses.

        Parameters
        ----------
            waitFactor (float): Multiplier applied to the base trace-route timeout to extend the wait period.

        Raises
        ------
            MeshInterface.MeshInterfaceError: If the wait times out before a traceroute response is received.

        """
        success = self._timeout.waitForTraceRoute(waitFactor, self._acknowledgment)
        if not success:
            raise MeshInterface.MeshInterfaceError("Timed out waiting for traceroute")

    def waitForTelemetry(self):
        """
        Block until a telemetry response is received or the configured timeout elapses.

        Raises:
            MeshInterface.MeshInterfaceError: If waiting for telemetry times out.

        """
        success = self._timeout.waitForTelemetry(self._acknowledgment)
        if not success:
            raise MeshInterface.MeshInterfaceError("Timed out waiting for telemetry")

    def waitForPosition(self):
        """
        Block until a position acknowledgment is received.

        Raises:
            MeshInterface.MeshInterfaceError: If waiting for the position times out.

        """
        success = self._timeout.waitForPosition(self._acknowledgment)
        if not success:
            raise MeshInterface.MeshInterfaceError("Timed out waiting for position")

    def waitForWaypoint(self):
        """
        Block until a waypoint acknowledgment is received.

        Waits for the internal waypoint acknowledgment event to be set; raises MeshInterface.MeshInterfaceError if the wait times out.
        """
        success = self._timeout.waitForWaypoint(self._acknowledgment)
        if not success:
            raise MeshInterface.MeshInterfaceError("Timed out waiting for waypoint")

    def getMyNodeInfo(self) -> Optional[Dict]:
        """
        Return the stored node info for the local node.

        Returns:
            dict: The local node's info dictionary from nodesByNum, or `None`
                if myInfo or nodesByNum is unavailable or the local node entry
                is not present.

        """
        if self.myInfo is None or self.nodesByNum is None:
            return None
        logger.debug(f"self.nodesByNum:{self.nodesByNum}")
        return self.nodesByNum.get(self.myInfo.my_node_num)

    def getMyUser(self) -> Optional[Dict[str, Any]]:
        """
        Retrieve the user dictionary for the local node.

        If local node information is available, returns the value of its "user" field.

        Returns:
            dict: The local node's user data, or `None` if no local node info or user field is present.

        """
        nodeInfo = self.getMyNodeInfo()
        if nodeInfo is not None:
            return nodeInfo.get("user")
        return None

    def getLongName(self) -> Optional[str]:
        """
        Return the configured long name for the local user.

        Returns:
            The long name string for the local user, or None if no user or long name is set.

        """
        user = self.getMyUser()
        if user is not None:
            return user.get("longName", None)
        return None

    def getShortName(self) -> Optional[str]:
        """
        Return the local node user's short name.

        Returns:
            str: The user's `shortName` if present, `None` otherwise.

        """
        user = self.getMyUser()
        if user is not None:
            return user.get("shortName", None)
        return None

    def getPublicKey(self) -> Optional[bytes]:
        """
        Return the local node's public key if one is set.

        Returns:
            public_key (bytes | None): The local node's public key bytes if present, `None` otherwise.

        """
        user = self.getMyUser()
        if user is not None:
            return user.get("publicKey", None)
        return None

    def getCannedMessage(self) -> Optional[str]:
        """
        Retrieve the canned (predefined) message configured for the local node.

        Returns:
            str: The canned message text, or `None` if there is no local node or no canned message configured.

        """
        node = self.localNode
        if node is not None:
            return node.get_canned_message()
        return None

    def getRingtone(self) -> Optional[str]:
        """
        Retrieve the ringtone for the local node.

        Returns:
            str or None: The local node's ringtone name or identifier, or `None` if no local node exists or the ringtone is unavailable.

        """
        node = self.localNode
        if node is not None:
            return node.get_ringtone()
        return None

    def _waitConnected(self, timeout=30.0):
        """Block until the initial node db download is complete, or timeout
        and raise an exception."""
        if not self.noProto:
            if not self.isConnected.wait(timeout):  # timeout after x seconds
                raise MeshInterface.MeshInterfaceError(
                    "Timed out waiting for connection completion"
                )

        # If we failed while connecting, raise the connection to the client
        if self.failure is not None:
            raise self.failure

    def _generatePacketId(self) -> int:
        """
        Produce a new unique packet identifier for outgoing mesh packets.

        The returned value encodes a 10-bit rolling counter in the low 10 bits
        and random bits in the upper bits to reduce collision risk across
        restarts.

        Returns:
            int: A new packet id where the lower 10 bits are a monotonic counter and the remaining bits are randomized.

        Raises:
            MeshInterface.MeshInterfaceError: If the interface is not connected and cannot generate a packet id.

        """
        if self.currentPacketId is None:
            raise MeshInterface.MeshInterfaceError(
                "Not connected yet, can not generate packet"
            )
        else:
            nextPacketId = (self.currentPacketId + 1) & 0xFFFFFFFF
            nextPacketId = (
                nextPacketId & 0x3FF
            )  # == (0xFFFFFFFF >> 22), masks upper 22 bits
            randomPart = (
                random.randint(0, 0x3FFFFF) << 10
            ) & 0xFFFFFFFF  # generate number with 10 zeros at end
            self.currentPacketId = nextPacketId | randomPart  # combine
            return self.currentPacketId

    def _disconnected(self):
        """
        Mark the interface as disconnected and publish a meshtastic.connection.lost event.

        This clears the internal connected flag and schedules a published
        notification (via the publishing thread) so external listeners are
        informed that the interface has lost its connection.
        """
        self.isConnected.clear()
        publishingThread.queueWork(
            lambda: pub.sendMessage("meshtastic.connection.lost", interface=self)
        )

    def sendHeartbeat(self):
        """
        Send a heartbeat message to the radio to indicate the interface is alive.
        """
        p = mesh_pb2.ToRadio()
        p.heartbeat.CopyFrom(mesh_pb2.Heartbeat())
        self._sendToRadio(p)

    def _startHeartbeat(self):
        """
        Schedule and run periodic heartbeat transmissions to the radio device.

        This starts a recurring, daemonized timer that calls sendHeartbeat() at
        a fixed interval (300 seconds) and reschedules itself. The scheduler
        runs the first heartbeat immediately and thereafter every interval. It
        is safe during shutdown: the method respects the _closing flag and
        _heartbeat_lock to avoid scheduling or sending heartbeats after
        shutdown has begun.
        """

        def callback():
            """
            Schedule recurring heartbeat timers and dispatch a heartbeat, while avoiding scheduling or sending during shutdown.

            This callback creates and registers a daemon timer that re-invokes
            itself after a fixed interval, stores the timer on
            self.heartbeatTimer, and then calls self.sendHeartbeat() to emit a
            heartbeat. The function observes self._closing and acquires
            self._heartbeat_lock to ensure it does not schedule, store, or send
            heartbeats once shutdown has begun.
            """
            with self._heartbeat_lock:
                # If shutdown started, don't schedule more heartbeat timers.
                if self._closing:
                    return
                self.heartbeatTimer = None
            interval = 300
            logger.debug(f"Sending heartbeat, interval {interval} seconds")
            timer = threading.Timer(interval, callback)
            # Heartbeat maintenance should never prevent process shutdown.
            timer.daemon = True
            with self._heartbeat_lock:
                if self._closing:
                    return
                self.heartbeatTimer = timer
            timer.start()
            # Final guard: check _closing one more time before sending heartbeat
            # to avoid sending after shutdown has started
            with self._heartbeat_lock:
                if self._closing:
                    timer.cancel()
                    return
            self.sendHeartbeat()

        callback()  # run our periodic callback now, it will make another timer if necessary

    def _connected(self):
        """
        Mark the interface as connected, start the heartbeat timer, and publish a connection-established event.

        If the interface is shutting down, do nothing. Otherwise set the
        internal connected event, start periodic heartbeats, and enqueue a
        "meshtastic.connection.established" publication.
        """
        with self._heartbeat_lock:
            if self._closing:
                logger.debug("Skipping _connected(): interface is closing")
                return
        # (because I'm lazy) _connected might be called when remote Node
        # objects complete their config reads, don't generate redundant isConnected
        # for the local interface
        if not self.isConnected.is_set():
            self.isConnected.set()
            self._startHeartbeat()
            publishingThread.queueWork(
                lambda: pub.sendMessage(
                    "meshtastic.connection.established", interface=self
                )
            )

    def _startConfig(self):
        """
        Initialize internal node/config state and request the radio's configuration.

        Resets local state used during configuration (myInfo, nodes, nodesByNum, and local channel list),
        allocates a new non-conflicting configId when appropriate, and sends a ToRadio message
        requesting configuration using the chosen configId.
        """
        self.myInfo = None
        self.nodes = {}  # nodes keyed by ID
        self.nodesByNum = {}  # nodes keyed by nodenum
        self._localChannels = (
            []
        )  # empty until we start getting channels pushed from the device (during config)

        startConfig = mesh_pb2.ToRadio()
        if self.configId is None or not self.noNodes:
            self.configId = random.randint(0, 0xFFFFFFFF)
            if self.configId == NODELESS_WANT_CONFIG_ID:
                self.configId = self.configId + 1
        startConfig.want_config_id = self.configId
        self._sendToRadio(startConfig)

    def _sendDisconnect(self):
        """
        Notify the radio device that this interface is disconnecting.
        """
        m = mesh_pb2.ToRadio()
        m.disconnect = True
        self._sendToRadio(m)

    def _queueHasFreeSpace(self) -> bool:
        # We never got queueStatus, maybe the firmware is old
        if self.queueStatus is None:
            return True
        return self.queueStatus.free > 0

    def _queueClaim(self) -> None:
        """
        Decrement the cached transmit-queue free-slot counter when a packet is claimed.

        Does nothing if queue status information is not available.
        """
        if self.queueStatus is None:
            return
        self.queueStatus.free -= 1

    def _sendToRadio(self, toRadio: mesh_pb2.ToRadio) -> None:
        """
        Send a ToRadio protobuf to the radio device, queueing MeshPacket payloads and sending non-packet messages immediately.

        If `toRadio` contains a `packet` (a MeshPacket) it is placed on the
        internal transmit queue and will be sent when TX queue space is
        available; otherwise it is sent immediately. This call may block while
        waiting for free TX queue space, will claim queue slots for outgoing
        packets, and will requeue packets that were not acknowledged. If
        protocol usage is disabled via `noProto`, the message is not sent.

        Parameters
        ----------
            toRadio (mesh_pb2.ToRadio): The protobuf message to transmit; if it
                has a `packet` field the contained MeshPacket will be queued
                for transmission.

        """
        if self.noProto:
            logger.warning(
                "Not sending packet because protocol use is disabled by noProto"
            )
        else:
            # logger.debug(f"Sending toRadio: {stripnl(toRadio)}")

            if not toRadio.HasField("packet"):
                # not a meshpacket -- send immediately, give queue a chance,
                # this makes heartbeat trigger queue
                self._sendToRadioImpl(toRadio)
            else:
                # meshpacket -- queue
                self.queue[toRadio.packet.id] = toRadio

            resentQueue = collections.OrderedDict()

            while self.queue:
                # logger.warn("queue: " + " ".join(f'{k:08x}' for k in self.queue))
                while not self._queueHasFreeSpace():
                    logger.debug("Waiting for free space in TX Queue")
                    time.sleep(0.5)
                try:
                    toResend = self.queue.popitem(last=False)
                except KeyError:
                    break
                packetId, packet = toResend
                # logger.warn(f"packet: {packetId:08x} {packet}")
                resentQueue[packetId] = packet
                if packet is False:
                    continue
                self._queueClaim()
                if packet != toRadio:
                    logger.debug(f"Resending packet ID {packetId:08x} {packet}")
                self._sendToRadioImpl(packet)

            # logger.warn("resentQueue: " + " ".join(f'{k:08x}' for k in resentQueue))
            for packetId, packet in resentQueue.items():
                if (
                    self.queue.pop(packetId, False) is False
                ):  # Packet got acked under us
                    logger.debug(f"packet {packetId:08x} got acked under us")
                    continue
                if packet:
                    self.queue[packetId] = packet
            # logger.warn("queue + resentQueue: " + " ".join(f'{k:08x}' for k in self.queue))

    def _sendToRadioImpl(self, toRadio: mesh_pb2.ToRadio) -> None:
        """
        Transmit the given ToRadio protobuf to the radio device; intended to be implemented by subclasses.

        Parameters
        ----------
            toRadio (mesh_pb2.ToRadio): The protobuf message describing the action or packet to send to the radio.

        Description:
            This method is the transport hook used by MeshInterface to deliver
            ToRadio protobufs to the underlying hardware or driver. Subclasses
            must override this method to perform the actual transmission.

        """
        logger.error(f"Subclass must provide toradio: {toRadio}")

    def _handleConfigComplete(self) -> None:
        """
        Finalize initial configuration by applying collected local channels and marking the interface as connected.

        Sets the local node's channels from the internally collected
        _localChannels and invokes _connected() to signal that configuration is
        complete and normal packet handling may begin.
        """
        # This is no longer necessary because the current protocol statemachine has already proactively sent us the locally visible channels
        # self.localNode.requestChannels()
        self.localNode.setChannels(self._localChannels)

        # the following should only be called after we have settings and channels
        self._connected()  # Tell everyone else we are ready to go

    def _handleQueueStatusFromRadio(self, queueStatus) -> None:
        self.queueStatus = queueStatus
        logger.debug(
            f"TX QUEUE free {queueStatus.free} of {queueStatus.maxlen}, res = {queueStatus.res}, id = {queueStatus.mesh_packet_id:08x} "
        )

        if queueStatus.res:
            return

        # logger.warn("queue: " + " ".join(f'{k:08x}' for k in self.queue))
        justQueued = self.queue.pop(queueStatus.mesh_packet_id, None)

        if justQueued is None and queueStatus.mesh_packet_id != 0:
            self.queue[queueStatus.mesh_packet_id] = False
            logger.debug(
                f"Reply for unexpected packet ID {queueStatus.mesh_packet_id:08x}"
            )
        # logger.warn("queue: " + " ".join(f'{k:08x}' for k in self.queue))

    def _handleFromRadio(self, fromRadioBytes):
        """
        Process raw FromRadio protobuf bytes, update internal state, and publish corresponding events.

        Parses the provided FromRadio protobuf payload and applies its contents to the interface:
        - updates myInfo and metadata and sets local node number;
        - creates/updates node entries and publishes meshtastic.node.updated;
        - handles config completion, channel records, packet receipts, log
          records, queue status, client notifications, MQTT proxy messages, and
          XMODEM packets by invoking the appropriate handlers and publishing
          events;
        - on reboot, marks the interface disconnected and restarts configuration download;
        - copies config and moduleConfig subfields into the local node's configuration objects.

        Parameters
        ----------
            fromRadioBytes (bytes): Raw protobuf bytes received from the radio representing a mesh_pb2.FromRadio message.

        Raises
        ------
            Exception: If parsing the protobuf fails or if downstream handlers raise; parse errors are logged before being re-raised.

        """
        fromRadio = mesh_pb2.FromRadio()
        logger.debug(
            f"in mesh_interface.py _handleFromRadio() fromRadioBytes: {fromRadioBytes}"
        )
        try:
            fromRadio.ParseFromString(fromRadioBytes)
        except Exception:
            logger.exception("Error while parsing FromRadio bytes:%s", fromRadioBytes)
            raise
        asDict = google.protobuf.json_format.MessageToDict(fromRadio)
        logger.debug(f"Received from radio: {fromRadio}")
        if fromRadio.HasField("my_info"):
            self.myInfo = fromRadio.my_info
            self.localNode.nodeNum = self.myInfo.my_node_num
            logger.debug(f"Received myinfo: {stripnl(fromRadio.my_info)}")

        elif fromRadio.HasField("metadata"):
            self.metadata = fromRadio.metadata
            logger.debug(f"Received device metadata: {stripnl(fromRadio.metadata)}")

        elif fromRadio.HasField("node_info"):
            logger.debug(f"Received nodeinfo: {asDict['nodeInfo']}")

            node = self._getOrCreateByNum(asDict["nodeInfo"]["num"])
            node.update(asDict["nodeInfo"])
            try:
                newpos = self._fixupPosition(node["position"])
                node["position"] = newpos
            except KeyError:
                logger.debug("Node has no position key")

            # no longer necessary since we're mutating directly in nodesByNum via _getOrCreateByNum
            # self.nodesByNum[node["num"]] = node
            if "user" in node:  # Some nodes might not have user/ids assigned yet
                if "id" in node["user"]:
                    if self.nodes is not None:
                        self.nodes[node["user"]["id"]] = node
            publishingThread.queueWork(
                lambda: pub.sendMessage(
                    "meshtastic.node.updated", node=node, interface=self
                )
            )
        elif fromRadio.config_complete_id == self.configId:
            # we ignore the config_complete_id, it is unneeded for our
            # stream API fromRadio.config_complete_id
            logger.debug(f"Config complete ID {self.configId}")
            self._handleConfigComplete()
        elif fromRadio.HasField("channel"):
            self._handleChannel(fromRadio.channel)
        elif fromRadio.HasField("packet"):
            self._handlePacketFromRadio(fromRadio.packet)
        elif fromRadio.HasField("log_record"):
            self._handleLogRecord(fromRadio.log_record)
        elif fromRadio.HasField("queueStatus"):
            self._handleQueueStatusFromRadio(fromRadio.queueStatus)
        elif fromRadio.HasField("clientNotification"):
            publishingThread.queueWork(
                lambda: pub.sendMessage(
                    "meshtastic.clientNotification",
                    notification=fromRadio.clientNotification,
                    interface=self,
                )
            )

        elif fromRadio.HasField("mqttClientProxyMessage"):
            publishingThread.queueWork(
                lambda: pub.sendMessage(
                    "meshtastic.mqttclientproxymessage",
                    proxymessage=fromRadio.mqttClientProxyMessage,
                    interface=self,
                )
            )

        elif fromRadio.HasField("xmodemPacket"):
            publishingThread.queueWork(
                lambda: pub.sendMessage(
                    "meshtastic.xmodempacket",
                    packet=fromRadio.xmodemPacket,
                    interface=self,
                )
            )

        elif fromRadio.HasField("rebooted") and fromRadio.rebooted:
            # Tell clients the device went away.  Careful not to call the overridden
            # subclass version that closes the serial port
            MeshInterface._disconnected(self)

            self._startConfig()  # redownload the node db etc...

        elif fromRadio.HasField("config") or fromRadio.HasField("moduleConfig"):
            if fromRadio.config.HasField("device"):
                self.localNode.localConfig.device.CopyFrom(fromRadio.config.device)
            elif fromRadio.config.HasField("position"):
                self.localNode.localConfig.position.CopyFrom(fromRadio.config.position)
            elif fromRadio.config.HasField("power"):
                self.localNode.localConfig.power.CopyFrom(fromRadio.config.power)
            elif fromRadio.config.HasField("network"):
                self.localNode.localConfig.network.CopyFrom(fromRadio.config.network)
            elif fromRadio.config.HasField("display"):
                self.localNode.localConfig.display.CopyFrom(fromRadio.config.display)
            elif fromRadio.config.HasField("lora"):
                self.localNode.localConfig.lora.CopyFrom(fromRadio.config.lora)
            elif fromRadio.config.HasField("bluetooth"):
                self.localNode.localConfig.bluetooth.CopyFrom(
                    fromRadio.config.bluetooth
                )
            elif fromRadio.config.HasField("security"):
                self.localNode.localConfig.security.CopyFrom(fromRadio.config.security)
            elif fromRadio.moduleConfig.HasField("mqtt"):
                self.localNode.moduleConfig.mqtt.CopyFrom(fromRadio.moduleConfig.mqtt)
            elif fromRadio.moduleConfig.HasField("serial"):
                self.localNode.moduleConfig.serial.CopyFrom(
                    fromRadio.moduleConfig.serial
                )
            elif fromRadio.moduleConfig.HasField("external_notification"):
                self.localNode.moduleConfig.external_notification.CopyFrom(
                    fromRadio.moduleConfig.external_notification
                )
            elif fromRadio.moduleConfig.HasField("store_forward"):
                self.localNode.moduleConfig.store_forward.CopyFrom(
                    fromRadio.moduleConfig.store_forward
                )
            elif fromRadio.moduleConfig.HasField("range_test"):
                self.localNode.moduleConfig.range_test.CopyFrom(
                    fromRadio.moduleConfig.range_test
                )
            elif fromRadio.moduleConfig.HasField("telemetry"):
                self.localNode.moduleConfig.telemetry.CopyFrom(
                    fromRadio.moduleConfig.telemetry
                )
            elif fromRadio.moduleConfig.HasField("canned_message"):
                self.localNode.moduleConfig.canned_message.CopyFrom(
                    fromRadio.moduleConfig.canned_message
                )
            elif fromRadio.moduleConfig.HasField("audio"):
                self.localNode.moduleConfig.audio.CopyFrom(fromRadio.moduleConfig.audio)
            elif fromRadio.moduleConfig.HasField("remote_hardware"):
                self.localNode.moduleConfig.remote_hardware.CopyFrom(
                    fromRadio.moduleConfig.remote_hardware
                )
            elif fromRadio.moduleConfig.HasField("neighbor_info"):
                self.localNode.moduleConfig.neighbor_info.CopyFrom(
                    fromRadio.moduleConfig.neighbor_info
                )
            elif fromRadio.moduleConfig.HasField("detection_sensor"):
                self.localNode.moduleConfig.detection_sensor.CopyFrom(
                    fromRadio.moduleConfig.detection_sensor
                )
            elif fromRadio.moduleConfig.HasField("ambient_lighting"):
                self.localNode.moduleConfig.ambient_lighting.CopyFrom(
                    fromRadio.moduleConfig.ambient_lighting
                )
            elif fromRadio.moduleConfig.HasField("paxcounter"):
                self.localNode.moduleConfig.paxcounter.CopyFrom(
                    fromRadio.moduleConfig.paxcounter
                )

        else:
            logger.debug("Unexpected FromRadio payload")

    def _fixupPosition(self, position: Dict) -> Dict:
        """
        Convert integer coordinate fields to float latitude/longitude values.

        If the input dict contains 'latitudeI' or 'longitudeI', sets 'latitude' and/or
        'longitude' to those values multiplied by 1e-7 and converted to float, then
        returns the same dict.

        Parameters
        ----------
            position (Dict): A position dictionary that may contain integer fields
                'latitudeI' and 'longitudeI'.

        Returns
        -------
            Dict: The same position dictionary with 'latitude' and/or 'longitude' set
            to float values when corresponding integer fields were present.

        """
        if "latitudeI" in position:
            position["latitude"] = float(position["latitudeI"] * Decimal("1e-7"))
        if "longitudeI" in position:
            position["longitude"] = float(position["longitudeI"] * Decimal("1e-7"))
        return position

    def _nodeNumToId(self, num: int, isDest: bool = True) -> Optional[str]:
        """
        Return the node ID corresponding to a mesh node number.

        If num is the broadcast number, returns the broadcast address when
        isDest is True or the string "Unknown" when isDest is False. Otherwise
        looks up the node in internal state.

        Parameters
        ----------
            isDest (bool): When True treat broadcast as a destination (return broadcast address); when False treat broadcast as an unknown source.

        Returns
        -------
            str: The node ID for the given node number, the broadcast address, "Unknown", or `None` if the node number is not found.

        """
        if num == BROADCAST_NUM:
            if isDest:
                return BROADCAST_ADDR
            else:
                return "Unknown"

        try:
            return self.nodesByNum[num]["user"]["id"]  # type: ignore[index]
        except (KeyError, TypeError):
            logger.debug(f"Node {num} not found for fromId")
            return None

    def _getOrCreateByNum(self, nodeNum):
        """
        Return a node entry for a given node number, creating a minimal placeholder if none exists.

        Parameters
        ----------
            nodeNum (int): Numeric node identifier.

        Returns
        -------
            dict: Node info dictionary stored in self.nodesByNum for the given nodeNum.

        Raises
        ------
            MeshInterface.MeshInterfaceError: If nodeNum is the broadcast node number.

        """
        if nodeNum == BROADCAST_NUM:
            raise MeshInterface.MeshInterfaceError(
                "Can not create/find nodenum by the broadcast num"
            )

        if self.nodesByNum is None:
            raise MeshInterface.MeshInterfaceError("Node database not initialized")

        if nodeNum in self.nodesByNum:
            return self.nodesByNum[nodeNum]
        else:
            presumptive_id = f"!{nodeNum:08x}"
            n = {
                "num": nodeNum,
                "user": {
                    "id": presumptive_id,
                    "longName": f"Meshtastic {presumptive_id[-4:]}",
                    "shortName": f"{presumptive_id[-4:]}",
                    "hwModel": "UNSET",
                },
            }  # Create a minimal node db entry
            self.nodesByNum[nodeNum] = n
            return n

    def _handleChannel(self, channel):
        """
        Append a received channel descriptor to the interface's local channel list during initial configuration.

        Parameters
        ----------
            channel: Channel descriptor (protobuf message or dict) received from
                the radio that describes a local channel; appended to
                self._localChannels.

        """
        self._localChannels.append(channel)

    def _handlePacketFromRadio(self, meshPacket, hack=False):
        """
        Process an incoming MeshPacket from the radio and publish an appropriate meshtastic.receive event.

        Converts the protobuf MeshPacket to a dictionary, attaches the original
        protobuf as "raw", ensures "from" and "to" keys exist (defaulting to 0
        if missing), and populates "fromId"/"toId" using
        node-number-to-ID mapping. If the packet contains a decoded payload,
        preserves the raw payload bytes, exposes or sets a port number, and,
        when a known protocol handler exists, decodes the protocol payload into
        decoded.<handler.name> and attaches the protocol protobuf as
        decoded.<handler.name>["raw"]. If a protocol handler defines an
        onReceive callback it will be invoked with the packet dict. If the
        decoded payload contains a requestId, any registered response handler
        will be invoked (respecting ACK filtering and handler
        ackPermitted/onAckNak rules). Finally, publishes the packet on a topic
        like "meshtastic.receive", "meshtastic.receive.data.<portnum>", or
        "meshtastic.receive.<protocol>" via the pub/sub system.

        Parameters
        ----------
            meshPacket: protobuf MeshPacket instance received from the radio.
            hack (bool): When True, skip the usual check that requires a "from" field so unit tests can construct packets lacking that field.

        Returns
        -------
            None

        """
        asDict = google.protobuf.json_format.MessageToDict(meshPacket)

        # We normally decompose the payload into a dictionary so that the client
        # doesn't need to understand protobufs.  But advanced clients might
        # want the raw protobuf, so we provide it in "raw"
        asDict["raw"] = meshPacket

        # from might be missing if the nodenum was zero.
        if not hack and "from" not in asDict:
            asDict["from"] = 0
            logger.error(
                f"Device returned a packet we sent, ignoring: {stripnl(asDict)}"
            )
            print(
                f"Error: Device returned a packet we sent, ignoring: {stripnl(asDict)}"
            )
            return
        if "to" not in asDict:
            asDict["to"] = 0

        # /add fromId and toId fields based on the node ID
        try:
            asDict["fromId"] = self._nodeNumToId(asDict["from"], False)
        except Exception as ex:
            logger.warning(f"Not populating fromId {ex}")
        try:
            asDict["toId"] = self._nodeNumToId(asDict["to"])
        except Exception as ex:
            logger.warning(f"Not populating toId {ex}")

        # We could provide our objects as DotMaps - which work with . notation or as dictionaries
        # asObj = DotMap(asDict)
        topic = "meshtastic.receive"  # Generic unknown packet type

        decoded = None
        portnum = portnums_pb2.PortNum.Name(portnums_pb2.PortNum.UNKNOWN_APP)
        if "decoded" in asDict:
            decoded = asDict["decoded"]
            # The default MessageToDict converts byte arrays into base64 strings.
            # We don't want that - it messes up data payload.  So slam in the correct
            # byte array.
            decoded["payload"] = meshPacket.decoded.payload

            # UNKNOWN_APP is the default protobuf portnum value, and therefore if not
            # set it will not be populated at all to make API usage easier, set
            # it to prevent confusion
            if "portnum" not in decoded:
                decoded["portnum"] = portnum
                logger.warning(f"portnum was not in decoded. Setting to:{portnum}")
            else:
                portnum = decoded["portnum"]

            topic = f"meshtastic.receive.data.{portnum}"

            # decode position protobufs and update nodedb, provide decoded version
            # as "position" in the published msg move the following into a 'decoders'
            # API that clients could register?
            portNumInt = meshPacket.decoded.portnum  # we want portnum as an int
            handler = protocols.get(portNumInt)
            # The decoded protobuf as a dictionary (if we understand this message)
            p = None
            if handler is not None:
                topic = f"meshtastic.receive.{handler.name}"

                # Convert to protobuf if possible
                if handler.protobufFactory is not None:
                    pb = handler.protobufFactory()
                    pb.ParseFromString(meshPacket.decoded.payload)
                    p = google.protobuf.json_format.MessageToDict(pb)
                    asDict["decoded"][handler.name] = p
                    # Also provide the protobuf raw
                    asDict["decoded"][handler.name]["raw"] = pb

                # Call specialized onReceive if necessary
                if handler.onReceive is not None:
                    handler.onReceive(self, asDict)

            # Is this message in response to a request, if so, look for a handler
            requestId = decoded.get("requestId")
            if requestId is not None:
                logger.debug(f"Got a response for requestId {requestId}")
                # We ignore ACK packets unless the callback is named `onAckNak`
                # or the handler is set as ackPermitted, but send NAKs and
                # other, data-containing responses to the handlers
                routing = decoded.get("routing")
                isAck = routing is not None and (
                    "errorReason" not in routing or routing["errorReason"] == "NONE"
                )
                # we keep the responseHandler in dict until we actually call it
                handler = self.responseHandlers.get(requestId, None)
                if handler is not None:
                    if (
                        (not isAck)
                        or handler.callback.__name__ == "onAckNak"
                        or handler.ackPermitted
                    ):
                        handler = self.responseHandlers.pop(requestId, None)
                        if handler is not None:
                            logger.debug(
                                f"Calling response handler for requestId {requestId}"
                            )
                            handler.callback(asDict)

        logger.debug(f"Publishing {topic}: packet={stripnl(asDict)} ")
        publishingThread.queueWork(
            lambda: pub.sendMessage(topic, packet=asDict, interface=self)
        )
