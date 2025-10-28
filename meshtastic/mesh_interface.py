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
from typing import Any, Callable, Dict, List, Optional, Union

import google.protobuf.json_format
from google.protobuf.message import DecodeError  # type: ignore[import-untyped]

try:
    import print_color  # type: ignore[import-untyped]
except ImportError:
    print_color = None

from pubsub import pub
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
    Format a past time interval given in seconds into a short human-readable string.

    Parameters
    ----------
        delta_secs (int): Number of seconds in the past. Zero or negative values are treated as "now".

    Returns
    -------
        str: `"now"` for zero or negative inputs; otherwise a string like `"30 sec ago"`, `"1 hour ago"`, or `"2 days ago"` using the largest whole unit that fits.

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

        def __init__(self, message):
            """
            Initialize the exception with a human-readable message.

            Parameters
            ----------
                message (str): Error message stored on the instance and passed to the base Exception.

            """
            self.message = message
            super().__init__(self.message)

    def __init__(
        self,
        debugOut=None,
        noProto: bool = False,
        noNodes: bool = False,
        timeout: int = 300,
    ) -> None:
        """
        Initialize the MeshInterface and prepare internal state for communicating with a Meshtastic device.

        Parameters:
            debugOut (optional): A stream-like object or callable that will receive formatted log lines; when provided the interface forwards normalized log lines to this handler.
            noProto (bool): When True, disable protocol-level processing of transport messages.
            noNodes (bool): When True, request a node-less configuration from the device at startup.
            timeout (int): Default timeout in seconds used for waiting on replies, configuration, and related operations.
        """
        self.debugOut = debugOut
        self.nodes: Dict[str, Dict] = (
            {}
        )  # Initialize as empty dict to prevent AttributeError
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
        self.failure = (
            None  # If we've encountered a fatal exception it will be kept here
        )
        self._timeout: Timeout = Timeout(maxSecs=timeout)
        self._acknowledgment: Acknowledgment = Acknowledgment()
        self.heartbeatTimer: Optional[threading.Timer] = None
        random.seed()  # FIXME, we should not clobber the random seedval here, instead tell user they must call it
        self.currentPacketId: int = random.randint(0, 0xFFFFFFFF)
        self.nodesByNum: Dict[int, Dict] = (
            {}
        )  # Initialize as empty dict to prevent AttributeError
        self.noNodes: bool = noNodes
        self.configId: Optional[int] = NODELESS_WANT_CONFIG_ID if noNodes else None
        self.gotResponse: bool = False  # used in gpio read
        self.mask: Optional[int] = None  # used in gpio read and gpio watch
        self.queueStatus: Optional[mesh_pb2.QueueStatus] = None
        self.queue: collections.OrderedDict = collections.OrderedDict()
        self._localChannels = None

        # We could have just not passed in debugOut to MeshInterface, and instead told consumers to subscribe to
        # the meshtastic.log.line publish instead.  Alas though changing that now would be a breaking API change
        # for any external consumers of the library.
        if debugOut:
            pub.subscribe(MeshInterface._printLogLine, "meshtastic.log.line")

    def close(self):
        """
        Shut down the mesh interface and terminate the radio connection.

        If a heartbeat timer is active, cancel it and send a disconnect message to the radio device.
        """
        if self.heartbeatTimer:
            self.heartbeatTimer.cancel()

        self._sendDisconnect()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, trace):
        if exc_type is not None and exc_value is not None:
            logger.error(
                f"An exception of type {exc_type} with value {exc_value} has occurred"
            )
        if trace is not None:
            logger.error(f"Traceback:\n{''.join(traceback.format_tb(trace))}")
        self.close()

    @staticmethod
    def _printLogLine(line, interface):
        """
        Write a single log line to the interface's debug output, using colorized output when possible.

        Parameters:
            line (str): Log line text to emit (without a trailing newline).
            interface: Object with a `debugOut` attribute (typically a file-like stream); if `interface.debugOut` is `sys.stdout` and a `print_color` printer is available, the line is printed with a color selected by severity substrings ("DEBUG", "INFO", "WARN", "ERR"); otherwise the line is written to `interface.debugOut` with a terminating newline.
        """
        if print_color is not None and interface.debugOut == sys.stdout:
            # this isn't quite correct (could cause false positives), but currently our formatting differs between different log representations
            if "DEBUG" in line:
                print_color.print(line, color="cyan", end=None)
            elif "INFO" in line:
                print_color.print(line, color="white", end=None)
            elif "WARN" in line:
                print_color.print(line, color="yellow", end=None)
            elif "ERR" in line:
                print_color.print(line, color="red", end=None)
            else:
                print_color.print(line, end=None)
        else:
            interface.debugOut.write(line + "\n")

    def _handleLogLine(self, line: str) -> None:
        """
        Publish a device log line to the internal pubsub after normalizing a trailing newline.

        If the line ends with '\n', the newline is removed before publishing on the "meshtastic.log.line" topic.

        Parameters:
            line (str): Log text received from the device; a trailing newline will be stripped.
        """

        # Devices should _not_ be including a newline at the end of each log-line str (especially when
        # encapsulated as a LogRecord).  But to cope with old device loads, we check for that and fix it here:
        if line.endswith("\n"):
            line = line[:-1]

        pub.sendMessage("meshtastic.log.line", line=line, interface=self)

    def _handleLogRecord(self, record: mesh_pb2.LogRecord) -> None:
        """
        Publish the embedded log message from a LogRecord protobuf.

        Parameters
        ----------
                record (mesh_pb2.LogRecord): Protobuf containing the log `message` to forward to the normal log-line handler.

        """
        # For now we just try to format the line as if it had come in over the serial port
        self._handleLogLine(record.message)

    def showInfo(self, file=sys.stdout) -> str:  # pylint: disable=W0613
        """
        Produce a human-readable summary of the interface state and return it as a string.

        The summary includes the owner's long and short names, the local node info when available,
        device metadata when available, and a JSON-formatted map of known nodes (node ID -> node data)
        with normalized fields (for example, MAC addresses converted to human-readable form).

        Parameters
        ----------
            file: A file-like object to which the summary is printed (defaults to sys.stdout).

        Returns
        -------
            infos (str): The formatted summary string that was printed to `file`.

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
        print(infos, file=file)
        return infos

    def showNodes(
        self, includeSelf: bool = True, showFields: Optional[List[str]] = None
    ) -> str:  # pylint: disable=W0613
        """
        Render a human-readable table of known mesh nodes.

        Renders a tabular view (one row per known node) using either a sensible default column set or the ordered
        dot-path fields provided in showFields. The table is printed to stdout and also returned.

        Parameters
        ----------
            includeSelf (bool): If False, omit the local node from the table.
            showFields (List[str] | None): Ordered list of dot-separated field paths to include as columns
                (for example, "user.longName"). If None or empty, a default set of common fields is used;
                the row number ("N") is always included.

        Returns
        -------
            str: The rendered table as a string.
        """

        def get_human_readable(name):
            """
            Map a dot-path field name to a concise, human-friendly column header.

            Parameters
            ----------
                name (str): Dot-separated field path (e.g., "user.longName", "position.latitude").

            Returns
            -------
                str: A short human-friendly label for the field if recognized; otherwise the original `name`.

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
            }

            if name in name_map:
                return name_map.get(name)  # Default to a formatted guess
            else:
                return name

        def formatFloat(value, precision=2, unit="") -> Optional[str]:
            """
            Format a number with fixed decimal precision and an optional unit suffix.

            If `value` is `None`, returns `None`.

            Parameters
            ----------
                value (float | int | None): Number to format.
                precision (int): Decimal places to include.
                unit (str): Suffix appended directly after the number (e.g., "m").

            Returns
            -------
                str or None: Formatted numeric string with `unit` appended (e.g., "12.34m"), or `None` when `value` is `None`.

            """
            return f"{value:.{precision}f}{unit}" if value is not None else None

        def getLH(ts) -> Optional[str]:
            """
            Format a UNIX timestamp into a local datetime string "YYYY-MM-DD HH:MM:SS".

            Parameters
            ----------
                ts (int | float | None): Seconds since the epoch. If `ts` is falsy (None or 0) the function returns `None`.

            Returns
            -------
                Optional[str]: Local-time formatted timestamp "YYYY-MM-DD HH:MM:SS" when `ts` is truthy, `None` otherwise.

            """
            return (
                datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S") if ts else None
            )

        def getTimeAgo(ts) -> Optional[str]:
            """
            Return a concise human-readable description of how long ago a POSIX timestamp occurred.

            Parameters
            ----------
                ts (float|int|None): POSIX timestamp in seconds since the epoch; if None, no timestamp is available.

            Returns
            -------
                str or None: A short interval like "now", "30 sec ago", or "1 hour ago", or None if `ts` is None or represents a time in the future.

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
            Retrieve the value at a dot-separated key path from a nested dictionary.

            Traverses nested dictionaries following the keys in `key_path` (for example "user.location.lat"). If every step exists and is a dictionary, returns the final value; returns `None` if any key is missing or an intermediate value is not a dictionary.

            Parameters
            ----------
                node_dict (dict): Dictionary to search.
                key_path (str): Dot-separated sequence of keys defining the nested path.

            Returns
            -------
                The value found at the specified path, or `None` if the path cannot be resolved.

            """
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

                # Filter out any field in data set that was not specified.
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
        Get the MeshInterface Node object for the specified node identifier.

        Parameters
        ----------
            nodeId (str): Node identifier (for example a node ID string). If equal to LOCAL_ADDR or BROADCAST_ADDR, the localNode placeholder is returned.
            requestChannels (bool): If True, request the remote device's channel and configuration data after creating the Node.
            requestChannelAttempts (int): Number of retry attempts to retrieve channel information before giving up.
            timeout (int): Seconds to wait when creating the Node and while waiting for its configuration.

        Returns
        -------
            meshtastic.node.Node: The Node corresponding to nodeId (or localNode for LOCAL_ADDR/BROADCAST_ADDR).

        Notes
        -----
            If channel retrieval repeatedly times out the function may terminate the process via our_exit.

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
    ):
        """
        Send a UTF-8 text message to a node, optionally requesting link-layer acknowledgement or an application-level response.

        Parameters
        ----------
            text (str): The UTF-8 string to send.
            destinationId (int | str): Target node number, node ID string, or BROADCAST_ADDR.
            wantAck (bool): If true, request link-layer acknowledgement and automatic retries.
            wantResponse (bool): If true, request an application-level response from the recipient.
            onResponse (Callable[[dict], Any] | None): Optional callback invoked with the decoded response message when received.
            channelIndex (int): Channel index to send on.
            portNum (int): Application port number to use for the payload.
            replyId (int | None): If provided, marks this message as a response to the given message ID.

        Returns
        -------
            mesh_pb2.MeshPacket: The sent mesh packet; its `id` will be populated and can be used to track acknowledgements or responses.
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
    ):
        """
        Send a high-priority alert text to a node which may trigger special notifications on some clients.

        Parameters
        ----------
            text (str): The alert message to send.
            destinationId (int | str, optional): Destination node number or node ID string. Defaults to BROADCAST_ADDR.
            onResponse (callable, optional): Callback invoked with the response packet dictionary when a response is received.
            channelIndex (int, optional): Channel index to use for sending. Defaults to 0.

        Returns
        -------
            mesh_pb2.MeshPacket: The sent mesh packet with its `id` populated for tracking.
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

    def sendMqttClientProxyMessage(self, topic: str, data: bytes):
        """
        Send an MQTT client-proxy message through the radio.

        Parameters
        ----------
                topic (str): MQTT topic string from the broker.
                data (bytes): Raw MQTT message payload to forward to the radio.

        """
        prox = mesh_pb2.MqttClientProxyMessage()
        prox.topic = topic
        prox.data = data
        toRadio = mesh_pb2.ToRadio()
        toRadio.mqttClientProxyMessage.CopyFrom(prox)
        self._sendToRadio(toRadio)

    def sendData(
        self,
        data,
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
        priority: mesh_pb2.MeshPacket.Priority.ValueType = (
            mesh_pb2.MeshPacket.Priority.RELIABLE
        ),
        replyId: Optional[int] = None,
    ):  # pylint: disable=R0913
        """
        Send an application payload to a mesh node and enqueue it for transmission.

        Serializes protobuf messages to bytes when needed, enforces the maximum payload size, optionally registers a response callback for the packet's request id, and queues the prepared MeshPacket for transmission.

        Parameters:
            data: Bytes or a protobuf message to send; protobufs will be serialized to bytes.
            destinationId (int | str): Target node number, node id string, or BROADCAST_ADDR.
            portNum (int): Application port number the recipient should use to interpret the payload.
            wantAck (bool): Request link-layer acknowledgement/retries when True.
            wantResponse (bool): Request an application-layer response when True.
            onResponse (Callable[[dict], Any] | None): Callback invoked when a response or NAK is received (and optionally for plain ACKs).
            onResponseAckPermitted (bool): When True, allow `onResponse` to be invoked for plain ACKs as well as responses/NAKs.
            channelIndex (int): Channel index to assign to the packet.
            hopLimit (int | None): Maximum hops the packet may traverse; uses the default if None.
            pkiEncrypted (bool | None): When True, attempt PKI encryption for the packet.
            publicKey (bytes | None): Public key to use for PKI encryption when applicable.
            priority (mesh_pb2.MeshPacket.Priority.ValueType | None): Packet priority.
            replyId (int | None): If set, marks this packet as a response to the given message id.

        Returns:
            mesh_pb2.MeshPacket: The prepared MeshPacket with its `id` populated.

        Raises:
            MeshInterface.MeshInterfaceError: If the serialized payload exceeds mesh_pb2.Constants.DATA_PAYLOAD_LEN.
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
    ):
        """
        Send a Position packet advertising the device's location to a destination.

        Latitude and longitude values equal to 0.0 are omitted; non-zero lat/lon are encoded as 1e-7 fixed-point integers (stored in latitude_i/longitude_i). An altitude of 0 is omitted. If wantResponse is True, the call will wait for a position response before returning.

        Parameters
        ----------
            latitude (float): Latitude in decimal degrees.
            longitude (float): Longitude in decimal degrees.
            altitude (int): Altitude in meters.
            destinationId (int | str): Destination node identifier or address (defaults to broadcast).
            wantAck (bool): Request an acknowledgement for the sent packet.
            wantResponse (bool): Wait for and handle a position response before returning.
            channelIndex (int): Channel index to send the packet on.

        Returns
        -------
            mesh_pb2.MeshPacket: The sent mesh packet with its `id` populated for tracking ACK/NAK.

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
        Process a received position or routing response and present a human-readable position.

        When the packet's decoded port is POSITION_APP, parse the Position protobuf from
        p["decoded"]["payload"], mark the interface acknowledgment as having received a
        position, and print a human-readable summary including latitude/longitude (if
        provided), altitude in meters (if provided), and precision information
        ("full precision", "position disabled", or "precision:<bits>"). When the packet's
        decoded port is ROUTING_APP and routing.errorReason equals "NO_RESPONSE", terminate
        the program via our_exit with a message indicating the destination node's firmware
        is too old.

        Parameters
        ----------
            p (dict): Packet dictionary containing a "decoded" mapping with at least
                a "portnum" key. For POSITION_APP, the "decoded" mapping must include a
                binary "payload" containing a serialized mesh_pb2.Position protobuf.

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
    ):
        """
        Initiates a traceroute to the specified destination and waits for the route discovery results.

        Sends a RouteDiscovery request to the destination and blocks until traceroute responses are received or the wait period derived from hopLimit expires.

        Parameters
        ----------
            dest (int | str): Destination node number or node ID string.
            hopLimit (int): Maximum number of hops to probe; also used as the upper bound for the response wait period.
            channelIndex (int, optional): Channel index to use for the request. Defaults to 0.

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
        Format and print human-readable forward and reverse traceroute results from a RouteDiscovery payload.

        Parses a RouteDiscovery protobuf from p['decoded']['payload'] and prints a formatted route from the responder toward the requested destination including per-hop SNR values in dB (SNR values in the proto are divided by 4 to produce dB). If the response contains a valid reverse route (indicated by presence of 'hopStart' in p and matching 'snrBack' entries) prints the route traced back to the requester. Unknown SNR entries are rendered as "?". Marks the traceroute as received by setting self._acknowledgment.receivedTraceRoute = True.

        Parameters
        ----------
            p (dict): Packet dictionary produced by _handlePacketFromRadio. Expected keys:
                - decoded (dict): contains a 'payload' bytes field with the serialized RouteDiscovery proto.
                - to (int): Numeric node id used as the destination in printed output.
                - from (int): Numeric node id used as the origin in printed output.
                - hopStart (optional): presence indicates reverse-route validity when combined with 'snrBack' in the decoded payload.

        """
        UNK_SNR = -128  # Value representing unknown SNR

        routeDiscovery = mesh_pb2.RouteDiscovery()
        routeDiscovery.ParseFromString(p["decoded"]["payload"])
        asDict = google.protobuf.json_format.MessageToDict(routeDiscovery)

        print("Route traced towards destination:")
        routeStr = (
            self._nodeNumToId(p["to"], False) or f"{p['to']:08x}"
        )  # Start with destination of response

        # SNR list should have one more entry than the route, as the final destination adds its SNR also
        lenTowards = 0 if "route" not in asDict else len(asDict["route"])
        snrTowardsValid = (
            "snrTowards" in asDict and len(asDict["snrTowards"]) == lenTowards + 1
        )
        if lenTowards > 0:  # Loop through hops in route and add SNR if available
            for idx, nodeNum in enumerate(asDict["route"]):
                routeStr += (
                    " --> "
                    + (self._nodeNumToId(nodeNum, False) or f"{nodeNum:08x}")
                    + " ("
                    + (
                        str(asDict["snrTowards"][idx] / 4)
                        if snrTowardsValid and asDict["snrTowards"][idx] != UNK_SNR
                        else "?"
                    )
                    + "dB)"
                )

        # End with origin of response
        routeStr += (
            " --> "
            + (self._nodeNumToId(p["from"], False) or f"{p['from']:08x}")
            + " ("
            + (
                str(asDict["snrTowards"][-1] / 4)
                if snrTowardsValid and asDict["snrTowards"][-1] != UNK_SNR
                else "?"
            )
            + "dB)"
        )

        print(routeStr)  # Print the route towards destination

        # Only if hopStart is set and there is an SNR entry (for the origin) it's valid, even though route might be empty (direct connection)
        lenBack = 0 if "routeBack" not in asDict else len(asDict["routeBack"])
        backValid = (
            "hopStart" in p
            and "snrBack" in asDict
            and len(asDict["snrBack"]) == lenBack + 1
        )
        if backValid:
            print("Route traced back to us:")
            routeStr = (
                self._nodeNumToId(p["from"], False) or f"{p['from']:08x}"
            )  # Start with origin of response

            if lenBack > 0:  # Loop through hops in routeBack and add SNR if available
                for idx, nodeNum in enumerate(asDict["routeBack"]):
                    routeStr += (
                        " --> "
                        + (self._nodeNumToId(nodeNum, False) or f"{nodeNum:08x}")
                        + " ("
                        + (
                            str(asDict["snrBack"][idx] / 4)
                            if asDict["snrBack"][idx] != UNK_SNR
                            else "?"
                        )
                        + "dB)"
                    )

            # End with destination of response (us)
            routeStr += (
                " --> "
                + (self._nodeNumToId(p["to"], False) or f"{p['to']:08x}")
                + " ("
                + (
                    str(asDict["snrBack"][-1] / 4)
                    if asDict["snrBack"][-1] != UNK_SNR
                    else "?"
                )
                + "dB)"
            )

            print(routeStr)  # Print the route back to us

        self._acknowledgment.receivedTraceRoute = True

    def sendTelemetry(
        self,
        destinationId: Union[int, str] = BROADCAST_ADDR,
        wantResponse: bool = False,
        channelIndex: int = 0,
        telemetryType: str = "device_metrics",
    ):
        """
        Send a Telemetry protobuf to a node or to broadcast.

        Populates the requested telemetry section (defaults to `device_metrics` copied from the local node when available) and transmits it on the specified channel. If `wantResponse` is True, registers a response handler and waits for a telemetry response.

        Parameters:
            destinationId (int | str): Target node number, node identifier, or `BROADCAST_ADDR` to broadcast.
            wantResponse (bool): If True, request and wait for a telemetry response.
            channelIndex (int): Channel index to send the telemetry on.
            telemetryType (str): Telemetry section to populate and send. Valid values are
                "device_metrics" (default), "environment_metrics", "air_quality_metrics",
                "power_metrics", and "local_stats".
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
        Handle an incoming telemetry or routing response and print human-readable telemetry metrics.

        Parses a Telemetry protobuf from p["decoded"]["payload"], sets self._acknowledgment.receivedTelemetry = True, and prints the fields in `device_metrics` (battery_level, voltage, channel_utilization, air_util_tx, uptime_seconds) when present. If `device_metrics` is absent, converts the Telemetry message to a dict and prints each section and its subfields except the `time` field. If the packet is a routing response and its `errorReason` equals `"NO_RESPONSE"`, calls our_exit with a firmware upgrade message.

        Parameters
        ----------
            p (dict): Decoded packet dictionary. Expected keys:
                "decoded" (dict) containing:
                    "portnum" (str): either "TELEMETRY_APP" or "ROUTING_APP".
                    For telemetry: "payload" (bytes) with the serialized Telemetry protobuf.
                    For routing: "routing" (dict) with "errorReason" (str).

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
        Handle a waypoint response or routing error received in a packet.

        Parses and prints a Waypoint when the packet's decoded port is `WAYPOINT_APP` and marks that a waypoint response was received. If the packet's decoded port is `ROUTING_APP` and its routing errorReason equals `"NO_RESPONSE"`, exits with an explanatory error message about required firmware.

        Parameters
        ----------
            p (dict): Packet dictionary containing a `decoded` mapping with keys used by this handler:
                - `portnum` (str): Port identifier such as `"WAYPOINT_APP"` or `"ROUTING_APP"`.
                - `payload` (bytes): Serialized Waypoint protobuf when `portnum` is `"WAYPOINT_APP"`.
                - `routing` (dict, optional): Routing information which may include `errorReason`.

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

    def sendWaypoint(
        self,
        name,
        description,
        expire: int,
        waypoint_id: Optional[int] = None,
        latitude: float = 0.0,
        longitude: float = 0.0,
        destinationId: Union[int, str] = BROADCAST_ADDR,
        wantAck: bool = True,
        wantResponse: bool = False,
        channelIndex: int = 0,
    ):  # pylint: disable=R0913
        """
        Send a waypoint message to a node or broadcast and return the sent mesh packet.

        Parameters
        ----------
            name (str): Waypoint name.
            description (str): Waypoint description.
            expire (int): Expiration value for the waypoint; use 0 to request deletion.
            waypoint_id (Optional[int]): Waypoint identifier; if None an id is generated.
            latitude (float): Latitude in decimal degrees; ignored when 0.0. Stored as fixed-point internal representation.
            longitude (float): Longitude in decimal degrees; ignored when 0.0. Stored as fixed-point internal representation.
            destinationId (int | str): Destination node id or address (defaults to broadcast).
            wantAck (bool): Whether an acknowledgement should be requested for the packet.
            wantResponse (bool): Whether to request and wait for an application-level response.
            channelIndex (int): Channel index to send the waypoint on.

        Returns
        -------
            mesh_pb2.MeshPacket: The MeshPacket that was sent, with its packet id populated.

        """
        w = mesh_pb2.Waypoint()
        w.name = name
        w.description = description
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
    ):
        """
        Request deletion of a waypoint by sending a Waypoint protobuf with its `expire` set to 0.

        Parameters:
            waypoint_id (int): The waypoint's identifier (the waypoint `id`, not a packet creation id).
            destinationId (int | str): Destination node identifier or broadcast address.
            wantAck (bool): Request link-layer acknowledgement for the transmitted packet.
            wantResponse (bool): If True, wait for the application-level waypoint response before returning.
            channelIndex (int): Channel index to use for sending.

        Returns:
            mesh_pb2.MeshPacket: The transmitted mesh packet with its `id` populated for tracking acknowledgments.
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
        Prepare and send a MeshPacket to a resolved destination node or broadcast.

        Resolves destinationId (numeric node number, node ID string, BROADCAST_ADDR, or LOCAL_ADDR) to a numeric node number, populates transmission fields on the provided MeshPacket (to, want_ack, hop_limit, pki_encrypted, public_key, and id if unset), and enqueues the packet for radio transmission unless protocol handling is disabled.

        Parameters:
            meshPacket (mesh_pb2.MeshPacket): MeshPacket protobuf to populate and send.
            destinationId (int | str): Numeric node number, node ID string (last 8 hex chars parsed), BROADCAST_ADDR, or LOCAL_ADDR.
            wantAck (bool): Request an acknowledgment when True.
            hopLimit (int | None): Maximum hop count; when None, uses the local LoRa configuration's hop_limit.
            pkiEncrypted (bool | None): When True, marks the packet as PKI-encrypted.
            publicKey (bytes | None): Optional public key bytes to attach to the packet.

        Returns:
            mesh_pb2.MeshPacket: The same MeshPacket instance after transmission fields have been populated (including a generated `id` if it was unset).
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
        Block until the interface's configuration (myInfo and nodes) and the local node's configuration are available.

        Raises:
            MeshInterface.MeshInterfaceError: if the wait times out before configuration completes.

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
        Block until an ACK or NAK is received for the last transmitted packet.

        Raises:
            MeshInterface.MeshInterfaceError: If waiting times out before an acknowledgment is received.

        """
        success = self._timeout.waitForAckNak(self._acknowledgment)
        if not success:
            raise MeshInterface.MeshInterfaceError(
                "Timed out waiting for an acknowledgment"
            )

    def waitForTraceRoute(self, waitFactor):
        """
        Block until a traceroute response is received or the computed timeout (derived from waitFactor) elapses.

        Parameters
        ----------
            waitFactor (float): Multiplier applied to the traceroute wait policy used to compute the total wait time.

        Raises
        ------
            MeshInterface.MeshInterfaceError: If no traceroute response is received before the computed timeout.

        """
        success = self._timeout.waitForTraceRoute(waitFactor, self._acknowledgment)
        if not success:
            raise MeshInterface.MeshInterfaceError("Timed out waiting for traceroute")

    def waitForTelemetry(self):
        """
        Waits until a telemetry response is received or the configured timeout elapses.

        Raises:
            MeshInterface.MeshInterfaceError: If waiting for telemetry times out.

        """
        success = self._timeout.waitForTelemetry(self._acknowledgment)
        if not success:
            raise MeshInterface.MeshInterfaceError("Timed out waiting for telemetry")

    def waitForPosition(self):
        """
        Block until a position response is received and acknowledged.

        Raises:
            MeshInterface.MeshInterfaceError: If waiting for a position response times out.
        """
        success = self._timeout.waitForPosition(self._acknowledgment)
        if not success:
            raise MeshInterface.MeshInterfaceError("Timed out waiting for position")

    def waitForWaypoint(self):
        """
        Waits until a waypoint response is received for this interface.

        Raises:
            MeshInterface.MeshInterfaceError: If the wait times out while waiting for the waypoint.

        """
        success = self._timeout.waitForWaypoint(self._acknowledgment)
        if not success:
            raise MeshInterface.MeshInterfaceError("Timed out waiting for waypoint")

    def getMyNodeInfo(self) -> Optional[Dict]:
        """
        Retrieve the node dictionary for the interface's current node number.

        Returns:
            dict: The node dictionary from self.nodesByNum for the current node.
            None: If myInfo or nodesByNum is missing, or no entry exists for the current node number.

        """
        if self.myInfo is None or self.nodesByNum is None:
            return None
        logger.debug(f"self.nodesByNum:{self.nodesByNum}")
        return self.nodesByNum.get(self.myInfo.my_node_num)

    def getMyUser(self):
        """
        Get the local node's user information.

        Returns:
            The local node's `user` mapping, or `None` if not available.

        """
        nodeInfo = self.getMyNodeInfo()
        if nodeInfo is not None:
            return nodeInfo.get("user")
        return None

    def getLongName(self):
        """
        Get the configured long name for the local user.

        Returns:
            The local user's `longName` string if available, `None` otherwise.
        """
        user = self.getMyUser()
        if user is not None:
            return user.get("longName", None)
        return None

    def getShortName(self):
        """
        Return the local node's short display name.

        Returns:
            short_name (str): The local user's `shortName` if present, otherwise `None`.
        """
        user = self.getMyUser()
        if user is not None:
            return user.get("shortName", None)
        return None

    def getPublicKey(self):
        """
        Get the local node's configured public key.

        Returns:
            The local user's `publicKey` value if present, `None` otherwise.

        """
        user = self.getMyUser()
        if user is not None:
            return user.get("publicKey", None)
        return None

    def getCannedMessage(self):
        """
        Get the local node's canned message if available.

        Returns:
            str or None: The canned message for the local node, or `None` if there is no local node or no canned message.

        """
        node = self.localNode
        if node is not None:
            return node.get_canned_message()
        return None

    def getRingtone(self):
        """
        Retrieve the configured ringtone for the local node.

        Returns:
            str: The ringtone identifier for the local node, or `None` if no local node or ringtone is unavailable.

        """
        node = self.localNode
        if node is not None:
            return node.get_ringtone()
        return None

    def _waitConnected(self, timeout=30.0):
        """
        Block until the interface reports it is connected or a timeout occurs.

        If `noProto` is True this returns immediately. If a fatal connection failure was recorded while waiting, that exception is re-raised.

        Parameters
        ----------
            timeout (float): Maximum time in seconds to wait for connection completion.

        Raises
        ------
            MeshInterface.MeshInterfaceError: If waiting times out.
            Exception: Re-raises the exception stored in `self.failure` if a fatal failure occurred during connection.

        """
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
        Generate the next packet identifier and store it on the interface.

        The new identifier preserves a monotonic lower 10-bit counter and uses randomized upper bits to reduce collision risk; the result is written to `self.currentPacketId`.

        Returns:
            int: The new packet identifier.

        Raises:
            MeshInterface.MeshInterfaceError: If `self.currentPacketId` is None (no base ID available).
        """
        if self.currentPacketId is None:
            raise MeshInterface.MeshInterfaceError(
                "Not connected yet, can not generate packet"
            )
        else:
            nextPacketId = (self.currentPacketId + 1) & 0xFFFFFFFF
            nextPacketId = (
                nextPacketId & 0x3FF
            )  # keep lower 10 bits for monotonic portion
            randomPart = (
                random.randint(0, 0x3FFFFF) << 10
            ) & 0xFFFFFFFF  # 22 random bits shifted left by 10
            self.currentPacketId = nextPacketId | randomPart  # combine
            return self.currentPacketId

    def _disconnected(self):
        """
        Mark the interface as disconnected and notify subscribers.

        Clears the internal connected flag and asynchronously publishes two events:
        `meshtastic.connection.lost` and `meshtastic.connection.status` with
        `connected=False`.
        """
        self.isConnected.clear()
        publishingThread.queueWork(
            lambda: pub.sendMessage("meshtastic.connection.lost", interface=self)
        )
        publishingThread.queueWork(
            lambda: pub.sendMessage(
                "meshtastic.connection.status", interface=self, connected=False
            )
        )

    def sendHeartbeat(self):
        """
        Send a heartbeat message to the radio to maintain and verify the connection.

        Enqueues a ToRadio heartbeat message for transmission to the connected radio device.
        """
        p = mesh_pb2.ToRadio()
        p.heartbeat.CopyFrom(mesh_pb2.Heartbeat())
        self._sendToRadio(p)

    def _startHeartbeat(self):
        """
        Start periodic heartbeats: send one immediately and schedule recurring heartbeats every 300 seconds.

        Stores the active threading.Timer on self.heartbeatTimer; canceling or replacing that Timer stops future heartbeats.
        """

        def callback():
            """
            Schedule the next heartbeat callback and send an immediate heartbeat.

            Sets a timer to invoke this callback again after 300 seconds and then transmits a heartbeat via sendHeartbeat().
            """
            self.heartbeatTimer = None
            interval = 300
            logger.debug(f"Sending heartbeat, interval {interval} seconds")
            self.heartbeatTimer = threading.Timer(interval, callback)
            self.heartbeatTimer.start()
            self.sendHeartbeat()

        callback()  # run our periodic callback now, it will make another timer if necessary

    def _connected(self):
        """
        Mark the interface as connected and start periodic heartbeats.

        If the interface was not already marked connected, set the internal connected flag, start the heartbeat timer, and publish
        pubsub messages "meshtastic.connection.established" and "meshtastic.connection.status" (with connected=True).
        """
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
            publishingThread.queueWork(
                lambda: pub.sendMessage(
                    "meshtastic.connection.status", interface=self, connected=True
                )
            )

    def _startConfig(self):
        """
        Reset cached node and configuration state and request a new configuration ID from the radio.

        Clears in-memory caches for myInfo, nodes, nodesByNum, and the local channel list. Ensures self.configId is set to a 32-bit configuration identifier (generating a new one when none exists or when node fetching is enabled) and avoids the reserved NODELESS_WANT_CONFIG_ID. Sends a ToRadio message with want_config_id to initiate the device configuration sequence.
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
        Request orderly shutdown of the radio transport session.

        Sends a ToRadio disconnect request to the radio transport to initiate connection teardown.
        """
        m = mesh_pb2.ToRadio()
        m.disconnect = True
        self._sendToRadio(m)

    def _queueHasFreeSpace(self) -> bool:
        # We never got queueStatus, maybe the firmware is old
        """
        Indicates whether the radio transmit queue likely has available space.

        Returns:
            True if the queue status is unknown or the reported free slot count is greater than zero, False otherwise.
        """
        if self.queueStatus is None:
            return True
        return self.queueStatus.free > 0

    def _queueClaim(self) -> None:
        """
        Decrement the available queue slot count when queue status is present.

        If `self.queueStatus` is None this method does nothing. Otherwise it reduces
        `self.queueStatus.free` by one to claim a queue slot.
        """
        if self.queueStatus is None:
            return
        self.queueStatus.free -= 1

    def _sendToRadio(self, toRadio: mesh_pb2.ToRadio) -> None:
        """
        Send a ToRadio message to the radio transport and manage the interface transmit queue.

        If `toRadio` contains a mesh packet, the packet is enqueued and this method attempts to drain the transmit queue by sending queued packets when transmit slots are available; this operation may block while waiting for free TX space and will re‑queue packets that remain unacknowledged. If `toRadio` contains no mesh packet, it is sent immediately. If protocol handling is disabled on the interface (`self.noProto`), the message is not transmitted.

        Parameters:
            toRadio (mesh_pb2.ToRadio): The ToRadio protobuf to send; may contain a mesh packet in the `packet` field.
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
        Send a prepared ToRadio protobuf to the radio transport; subclasses must override to perform the actual transmission.

        Base implementation logs an error and does not perform any transport. Subclasses should override this method to send the provided ToRadio message to the connected radio hardware.

        Parameters
        ----------
            toRadio (mesh_pb2.ToRadio): The protobuf message prepared for transmission to the radio.

        """
        logger.error(f"Subclass must provide toradio: {toRadio}")

    def _handleConfigComplete(self) -> None:
        """
        Finalize local configuration by applying cached local channels to the local node and mark the interface as connected so normal mesh processing can begin.
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
        Process a raw FromRadio protobuf message and update interface state and event streams.

        Parses the provided FromRadio bytes, updates internal state (myInfo, metadata, node records, local configuration, and queue status),
        and enqueues pubsub notifications for node updates, client notifications, MQTT proxy messages, xmodem packets, log records,
        and other radio events. Malformed or unparseable protobuf payloads are discarded without raising.

        Parameters
        ----------
            fromRadioBytes (bytes | bytearray): Raw FromRadio protobuf payload received from the radio. bytearray inputs are accepted and
                converted to bytes before parsing.

        """
        fromRadio = mesh_pb2.FromRadio()
        # Ensure we have bytes, not bytearray (BLE interface can return bytearray)
        if isinstance(fromRadioBytes, bytearray):
            fromRadioBytes = bytes(fromRadioBytes)
        logger.debug(
            f"in mesh_interface.py _handleFromRadio() fromRadioBytes: {fromRadioBytes}"
        )
        try:
            fromRadio.ParseFromString(fromRadioBytes)
        except DecodeError:
            # Handle protobuf parsing errors gracefully - discard corrupted packet
            logger.warning(
                "Failed to parse FromRadio packet, discarding: %r", fromRadioBytes
            )
            return
        except Exception:
            logger.exception("Error while parsing FromRadio bytes: %r", fromRadioBytes)
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
            except:
                logger.debug("Node without position")

            # no longer necessary since we're mutating directly in nodesByNum via _getOrCreateByNum
            # self.nodesByNum[node["num"]] = node
            if "user" in node:  # Some nodes might not have user/ids assigned yet
                if "id" in node["user"]:
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
        Populate floating-point latitude and longitude fields from fixed-point integer fields in the given mapping.

        If `latitudeI` and/or `longitudeI` are present (integers representing degrees × 1e7), sets `latitude` and/or `longitude` to the corresponding floating-point degree values in the same mapping.

        Parameters:
            position (dict): Mapping that may include `latitudeI` and/or `longitudeI` integer fields.

        Returns:
            dict: The same mapping with `latitude` and/or `longitude` added or updated when the corresponding integer fields were present.
        """
        if "latitudeI" in position:
            position["latitude"] = float(position["latitudeI"] * Decimal("1e-7"))
        if "longitudeI" in position:
            position["longitude"] = float(position["longitudeI"] * Decimal("1e-7"))
        return position

    def _nodeNumToId(self, num: int, isDest=True) -> Optional[str]:
        """
        Resolve a numeric node number to the corresponding node ID string.

        Parameters:
            isDest (bool): When True and `num` equals the broadcast number, return `BROADCAST_ADDR`; when False and `num` equals the broadcast number, return `"Unknown"`.

        Returns:
            `BROADCAST_ADDR` when `num` is `BROADCAST_NUM` and `isDest` is True; `"Unknown"` when `num` is `BROADCAST_NUM` and `isDest` is False; the node's user ID string if the node is known; otherwise `None`.
        """
        if num == BROADCAST_NUM:
            if isDest:
                return BROADCAST_ADDR
            else:
                return "Unknown"

        try:
            return self.nodesByNum[num]["user"]["id"]  # type: ignore[index]
        except:
            logger.debug(f"Node {num} not found for fromId")
            return None

    def _getOrCreateByNum(self, nodeNum):
        """
        Retrieve the node dictionary for a numeric node ID, creating and storing a minimal placeholder if absent.

        Parameters
        ----------
            nodeNum (int): Numeric node identifier to look up or create.

        Returns
        -------
            dict: The existing or newly created node dictionary stored in self.nodesByNum.

        Raises
        ------
            MeshInterface.MeshInterfaceError: If `nodeNum` is the broadcast number, which cannot be used to look up or create a node.

        """
        if nodeNum == BROADCAST_NUM:
            raise MeshInterface.MeshInterfaceError(
                "Can not create/find nodenum by the broadcast num"
            )

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
        Add a received channel to the interface's local channel cache.

        Parameters:
            channel: Channel protobuf or dict representing a channel received from the radio.
        """
        self._localChannels.append(channel)

    def _handlePacketFromRadio(self, meshPacket, hack=False):
        """
        Process an incoming MeshPacket and publish a corresponding meshtastic.receive event.

        Converts the protobuf to a dictionary, preserves the original protobuf under the "raw" key,
        adds numeric and human-readable sender/recipient identifiers when available, and restores
        the raw payload bytes for any decoded payload. If a registered protocol handler exists for
        the packet's port, attempt to parse the payload into that protobuf and store it under
        asDict["decoded"][<handler.name>] (the parsed protobuf is preserved as the "raw" entry).
        Invoke any handler.onReceive callbacks and, if the decoded message contains a requestId,
        dispatch a matching response handler according to ACK/NAK rules. Any decoding or callback
        exceptions are logged and suppressed; this function does not raise.

        Parameters
        ----------
                meshPacket: Incoming MeshPacket protobuf to process.
                hack (bool): If True, accept packets that are missing the "from" field (used for testing).

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
                    try:
                        pb = handler.protobufFactory()
                        pb.ParseFromString(meshPacket.decoded.payload)
                        p = google.protobuf.json_format.MessageToDict(pb)
                        asDict["decoded"][handler.name] = p
                        asDict["decoded"][handler.name]["raw"] = pb
                    except (DecodeError, ValueError, KeyError) as parse_err:
                        logger.warning(
                            "Failed to decode %s payload; skipping. err=%r",
                            handler.name,
                            parse_err,
                        )
                    except Exception:
                        logger.exception(
                            "Unexpected error decoding %s payload; skipping.",
                            handler.name,
                        )

                # Call specialized onReceive if necessary
                if handler.onReceive is not None:
                    try:
                        handler.onReceive(self, asDict)
                    except Exception:
                        logger.exception(
                            "onReceive callback for %s raised; continuing.",
                            handler.name,
                        )

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
                        logger.debug(
                            f"Calling response handler for requestId {requestId}"
                        )
                        try:
                            handler.callback(asDict)
                        except Exception:
                            logger.exception(
                                "Response handler callback for requestId %s raised; continuing.",
                                requestId,
                            )

        logger.debug(f"Publishing {topic}: packet={stripnl(asDict)} ")
        publishingThread.queueWork(
            lambda: pub.sendMessage(topic, packet=asDict, interface=self)
        )
