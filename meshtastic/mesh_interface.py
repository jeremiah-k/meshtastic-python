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
import warnings
from datetime import datetime
from types import TracebackType
from typing import IO, Any, Callable, Literal, TypeAlias, cast

import google.protobuf.json_format

try:
    import print_color  # type: ignore[import-untyped]
except ImportError:
    print_color = None

from pubsub import pub  # type: ignore[import-untyped,unused-ignore]
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
from meshtastic.protobuf import channel_pb2, mesh_pb2, portnums_pb2, telemetry_pb2
from meshtastic.util import (
    Acknowledgment,
    Timeout,
    convert_mac_addr,
    messageToJson,
    remove_keys_from_dict,
    stripnl,
)

logger = logging.getLogger(__name__)

# Heartbeat interval for keeping the interface alive (5 minutes)
HEARTBEAT_INTERVAL_SECONDS = 300

# Packet-id generation constants
PACKET_ID_MASK = 0xFFFFFFFF
PACKET_ID_COUNTER_MASK = 0x3FF
PACKET_ID_RANDOM_MAX = 0x3FFFFF
PACKET_ID_RANDOM_SHIFT_BITS = 10

# Queue backoff while waiting for TX slots to free
QUEUE_WAIT_DELAY_SECONDS = 0.5

# Accepted sendTelemetry payload kinds.
TelemetryType: TypeAlias = Literal[
    "environment_metrics",
    "air_quality_metrics",
    "power_metrics",
    "local_stats",
    "device_metrics",
]
DEFAULT_TELEMETRY_TYPE: TelemetryType = "device_metrics"
VALID_TELEMETRY_TYPES: tuple[TelemetryType, ...] = (
    "environment_metrics",
    "air_quality_metrics",
    "power_metrics",
    "local_stats",
    "device_metrics",
)
VALID_TELEMETRY_TYPE_SET: frozenset[str] = frozenset(VALID_TELEMETRY_TYPES)


def _timeago(delta_secs: int) -> str:
    """Produce a short human-readable relative time string for a past interval.

    Parameters
    ----------
    delta_secs : int
        Number of seconds elapsed in the past; zero or negative values are treated as "now".

    Returns
    -------
    str
        A compact relative time string such as "now", "30 sec ago", "1 hour ago", or "2 days ago".
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
            """Create a MeshInterfaceError with a human-readable message.

            Parameters
            ----------
            message : str
                The error message describing the failure.
            """
            self.message = message
            super().__init__(self.message)

    def __init__(
        self,
        debugOut: IO[str] | Callable[[str], Any] | None = None,
        noProto: bool = False,
        noNodes: bool = False,
        timeout: float = 300.0,
    ) -> None:
        """Initialize the MeshInterface and configure runtime options.

        Parameters
        ----------
        debugOut : IO[str] | Callable[[str], Any] | None
            Destination for human-readable log lines; if provided the
            interface will publish device logs to this output. (Default value = None)
        noProto : bool
            If True, disable running the meshtastic protocol layer over the link
            (operate as a dumb serial client). (Default value = False)
        noNodes : bool
            If True, instruct the device not to send its node database on startup;
            only other configuration will be requested. (Default value = False)
        timeout : float
            Default timeout in seconds for operations that wait for replies.
        """
        self.debugOut = debugOut
        self.nodes: dict[str, dict[str, Any]] | None = None
        self.isConnected: threading.Event = threading.Event()
        self.noProto: bool = noProto
        self.localNode: meshtastic.node.Node = meshtastic.node.Node(
            self, -1, timeout=timeout
        )  # We fixup nodenum later
        self.myInfo: mesh_pb2.MyNodeInfo | None = None  # We don't have device info yet
        self.metadata: mesh_pb2.DeviceMetadata | None = (
            None  # We don't have device metadata yet
        )
        # ------------------------------------------------------------------
        # Locking contract for MeshInterface shared state.
        #
        # Shared mutable state is intentionally split by concern:
        # - _response_handlers_lock: responseHandlers map
        # - _heartbeat_lock: _closing, heartbeatTimer, _heartbeat_inflight
        # - _packet_id_lock: currentPacketId generation
        # - _queue_lock: queue + queueStatus
        # - _node_db_lock: nodes/nodesByNum/_localChannels plus myInfo/metadata
        #   and local config/module config copies that are updated from RX thread.
        #
        # Deadlock-avoidance rule:
        # - Do not hold more than one MeshInterface lock at a time.
        # - If a future change must nest locks, establish and document a single
        #   global order in this block before introducing nested acquisition.
        #
        # Current implementation follows the no-nesting rule by acquiring one
        # lock, copying/snapshotting needed state, and doing I/O/callbacks after
        # releasing the lock.
        # ------------------------------------------------------------------
        # responseHandlers is shared by _add_response_handler (sendData path) and
        # _handle_packet_from_radio (receive thread). Use this lock to serialize
        # responseHandlers access across those call sites.
        self._response_handlers_lock = threading.RLock()
        self.responseHandlers: dict[int, ResponseHandler] = (
            {}
        )  # A map from request ID to the handler
        self.failure: BaseException | None = (
            None  # If we've encountered a fatal exception it will be kept here
        )
        self._timeout: Timeout = Timeout(maxSecs=timeout)
        self._acknowledgment: Acknowledgment = Acknowledgment()
        self.heartbeatTimer: threading.Timer | None = None
        self._heartbeat_lock = threading.RLock()
        # Track heartbeat sends that have passed the _closing gate but have not
        # finished I/O yet. close() waits on this to avoid post-close sends.
        self._heartbeat_inflight = 0
        self._heartbeat_idle_condition = threading.Condition(self._heartbeat_lock)
        self._packet_id_lock = threading.Lock()
        self._queue_lock = threading.RLock()
        # Guard node DB plus configuration state updated by the receive thread:
        # nodes/nodesByNum/_localChannels and myInfo/metadata/configId/localNode config copies.
        self._node_db_lock = threading.RLock()
        self._closing = False
        self.currentPacketId: int = random.randint(0, PACKET_ID_MASK)
        self.nodesByNum: dict[int, dict[str, Any]] | None = None
        self.noNodes: bool = noNodes
        self.configId: int | None = NODELESS_WANT_CONFIG_ID if noNodes else None
        self.gotResponse: bool = False  # used in gpio read
        self.mask: int | None = None  # legacy GPIO mask fallback (remote_hardware)
        self.queueStatus: mesh_pb2.QueueStatus | None = None
        self.queue: collections.OrderedDict[int, mesh_pb2.ToRadio | bool] = (
            collections.OrderedDict()
        )
        self._localChannels: list[channel_pb2.Channel] = []

        # We could have just not passed in debugOut to MeshInterface, and instead told consumers to subscribe to
        # the meshtastic.log.line publish instead.  Alas though changing that now would be a breaking API change
        # for any external consumers of the library.
        if debugOut:
            pub.subscribe(MeshInterface._print_log_line, "meshtastic.log.line")

    def close(self) -> None:
        """Shut down the interface and send a disconnect to the radio.

        Marks the interface as closing, cancels any scheduled heartbeat timer,
        waits for any in-flight heartbeat send to finish, then emits a
        disconnect message to the radio transport.
        """
        # Handle case where __init__ returned early before parent initialization
        heartbeat_lock = getattr(self, "_heartbeat_lock", None)
        heartbeat_idle_condition = getattr(self, "_heartbeat_idle_condition", None)
        if heartbeat_lock is not None:
            with heartbeat_lock:
                if self._closing:
                    return
                self._closing = True
                timer = self.heartbeatTimer
                self.heartbeatTimer = None
            if timer:
                timer.cancel()
            # Extra complexity is intentional: a callback can pass the _closing
            # check and begin sendHeartbeat() just before close() starts. Wait
            # until any such in-flight send completes so close() provides a
            # strong "no heartbeat after close returns" guarantee.
            if isinstance(heartbeat_idle_condition, threading.Condition):
                with heartbeat_lock:
                    while self._heartbeat_inflight > 0:
                        heartbeat_idle_condition.wait()
            self._send_disconnect()
        # debugOut is caller-owned (often shared via outer context managers);
        # do not close it here. Only clear our reference on shutdown.
        if hasattr(self, "debugOut"):
            self.debugOut = None

    def __enter__(self) -> "MeshInterface":
        """Enter a context for use with the with statement and return this MeshInterface instance.

        Returns
        -------
        'MeshInterface'
            This MeshInterface instance.
        """
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        trace: TracebackType | None,
    ) -> None:
        """Handle context-manager exit: log any exception information and close the interface.

        If an exception occurred within the with-block (exc_type is not None),
        any exception raised by close() is logged and suppressed so the original
        exception propagates. If no with-block exception occurred, close() exceptions
        are allowed to propagate normally.

        Parameters
        ----------
        exc_type : type[BaseException] | None
            The exception class if an exception was raised, otherwise None.
        exc_value : BaseException | None
            The exception instance if one was raised, otherwise None.
        trace : TracebackType | None
            The traceback object for the exception if present, otherwise None.
        """
        if exc_type is not None and exc_value is not None:
            logger.error(
                f"An exception of type {exc_type} with value {exc_value} has occurred"
            )
            if trace is not None:
                logger.error(f"Traceback:\n{''.join(traceback.format_tb(trace))}")
        try:
            self.close()
        except Exception:
            if exc_type is not None:
                logger.warning(
                    "close() failed while unwinding an existing exception.",
                    exc_info=True,
                )
            else:
                raise

    @staticmethod
    def _print_log_line(line: str, interface: Any) -> None:
        """Format and emit a single log line to the configured debug output.

        If a color printer is available and the interface is using stdout, apply a color
        based on the log level text (DEBUG→cyan, INFO→white, WARN→yellow, ERR→red).
        If interface.debugOut is a callable, invoke it with the line.
        If interface.debugOut exposes a write() method, write the line followed by a newline.

        Parameters
        ----------
        line : str
            The log line text to emit.
        interface : Any
            The MeshInterface instance (or similar) providing a `debugOut`
            attribute which may be stdout, a callable that accepts a string, or a
            file-like object with a `write()` method.
        """
        if print_color is not None and interface.debugOut == sys.stdout:
            # this isn't quite correct (could cause false positives), but currently our formatting differs between different log representations
            if "DEBUG" in line:
                print_color.print(line, color=cast(Any, "cyan"))
            elif "INFO" in line:
                print_color.print(line, color="white")
            elif "WARN" in line:
                print_color.print(line, color="yellow")
            elif "ERR" in line:
                print_color.print(line, color="red")
            else:
                print_color.print(line)
        elif callable(interface.debugOut):
            interface.debugOut(line)
        elif hasattr(interface.debugOut, "write"):
            interface.debugOut.write(line + "\n")

    def _handle_log_line(self, line: str) -> None:
        """Publish a device log line to the "meshtastic.log.line" topic, normalizing any trailing newline.

        Parameters
        ----------
        line : str
            Log text received from the device; a trailing newline (if present) is removed before publishing.
        """

        # Devices should _not_ be including a newline at the end of each log-line str (especially when
        # encapsulated as a LogRecord).  But to cope with old device loads, we check for that and fix it here:
        if line.endswith("\n"):
            line = line[:-1]

        pub.sendMessage("meshtastic.log.line", line=line, interface=self)

    def _handle_log_record(self, record: mesh_pb2.LogRecord) -> None:
        """Process a protobuf LogRecord by extracting its message text and handling it as a device log line.

        Parameters
        ----------
        record : mesh_pb2.LogRecord
            Protobuf log record containing the `message` field to be processed.
        """
        # For now we just try to format the line as if it had come in over the serial port
        self._handle_log_line(record.message)

    def showInfo(self, file: IO[str] | None = None) -> str:
        """Return a human-readable JSON summary of the mesh interface including owner, local node info, metadata, and known nodes.

        The summary omits internal node fields (`raw`, `decoded`, `payload`) and normalizes stored MAC addresses
        to a human-readable form before formatting.

        Parameters
        ----------
        file : IO[str] | None
            File-like object to which the summary is written; defaults to sys.stdout.

        Returns
        -------
        summary : str
            The formatted summary text that was written to `file`.
        """
        owner = f"Owner: {self.getLongName()} ({self.getShortName()})"
        with self._node_db_lock:
            my_info = self.myInfo
            metadata_info = self.metadata
        myinfo = ""
        if my_info:
            myinfo = f"\nMy info: {messageToJson(my_info)}"
        metadata = ""
        if metadata_info:
            metadata = f"\nMetadata: {messageToJson(metadata_info)}"
        mesh = "\n\nNodes in mesh: "
        nodes: dict[str, dict[str, Any]] = {}
        with self._node_db_lock:
            nodes_snapshot = list(self.nodes.values()) if self.nodes else []
        for n in nodes_snapshot:
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
        self, includeSelf: bool = True, showFields: list[str] | None = None
    ) -> str:  # pylint: disable=W0613
        """Produce a formatted table summarizing known mesh nodes.

        Parameters
        ----------
        includeSelf : bool
            If False, omit the local node from the output. (Default value = True)
        showFields : list[str] | None
            Ordered list of node fields to
            include (dotted paths for nested fields). If omitted or empty,
            a sensible default set of fields is used; the row-number column
            "N" is always included.

        Returns
        -------
        table : str
            The rendered table string (also printed to stdout)
            containing one row per node and columns mapped to human-readable
            headings.
        """

        def _get_human_readable(name: str) -> str:
            """Map an internal dotted field path to a human-readable column label.

            Parameters
            ----------
            name : str
                Dotted field path or key to convert (e.g., "user.longName", "position.latitude").

            Returns
            -------
            str
                A human-readable label for the given field when known (e.g., "User", "Latitude"); otherwise returns the original `name`.
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

            return name_map.get(name, name)

        def _format_float(
            value: int | float | None,
            precision: int = 2,
            unit: str = "",
        ) -> str | None:
            """Format a numeric value as a string with fixed decimal precision and an optional unit suffix.

            Parameters
            ----------
            value : int | float | None
                Value to format; returns `None` when this is `None`.
            precision : int
                Number of digits after the decimal point. (Default value = 2)
            unit : str
                Suffix appended directly after the number (for example, "V" or " dB"). (Default value = '')

            Returns
            -------
            str | None
                `None` if `value` is `None`, otherwise the formatted string using the given precision and unit (e.g., "3.14V").
            """
            return f"{value:.{precision}f}{unit}" if value is not None else None

        def _get_lh(ts: int | float | None) -> str | None:
            """Format a Unix timestamp as "YYYY-MM-DD HH:MM:SS" or return None when no timestamp is available.

            Parameters
            ----------
            ts : int | float | None
                Seconds since the Unix epoch. If `ts` is `None` or `0`, no timestamp is available.

            Returns
            -------
            str | None
                Formatted timestamp string in `YYYY-MM-DD HH:MM:SS` form, or `None` if `ts` is `None` or `0`.
            """
            return (
                datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")
                if ts is not None and ts != 0
                else None
            )

        def _get_time_ago(ts: int | float | None) -> str | None:
            """Return a short human-readable relative time string for a past Unix epoch timestamp.

            Parameters
            ----------
            ts : int | float | None
                Unix timestamp in seconds since the epoch. If `None` or `0`, no computation is performed.

            Returns
            -------
            str | None
                A concise relative time string such as "now", "5 sec ago", or "2 min ago",
                or `None` if `ts` is `None`, `0`, or represents a time in the future.
            """
            if ts is None or ts == 0:
                return None
            delta = datetime.now() - datetime.fromtimestamp(ts)
            delta_secs = int(delta.total_seconds())
            if delta_secs < 0:
                return None  # not handling a timestamp from the future
            return _timeago(delta_secs)

        def _get_nested_value(node_dict: dict[str, Any], key_path: str) -> Any:
            """Retrieve a nested value from a dictionary using a dotted key path.

            Parameters
            ----------
            node_dict : dict[str, Any]
                Dictionary to traverse.
            key_path : str
                Dotted path (e.g., "a.b.c"). Non-dotted paths are treated
                as a single-level lookup on node_dict.

            Returns
            -------
            Any
                The value found at the given path, or `None` if any intermediate
                key is missing or an intermediate value is not a dictionary.
            """
            # Guard against None or non-dict input
            if not isinstance(node_dict, dict):
                return None
            if "." not in key_path:
                # Treat non-dotted path as a single-level lookup
                return node_dict.get(key_path)
            keys = key_path.split(".")
            value: Any = node_dict
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
            # Always at least include the row number, but avoid duplicates.
            showFields = (
                ["N", *showFields] if "N" not in showFields else list(showFields)
            )

        rows: list[dict[str, Any]] = []
        with self._node_db_lock:
            nodes_snapshot = list(self.nodesByNum.values()) if self.nodesByNum else []
            nodes_log_snapshot = dict(self.nodes) if self.nodes else {}
            local_node_num = self.localNode.nodeNum
        if nodes_snapshot:
            logger.debug("self.nodes:%s", nodes_log_snapshot)
            for node in nodes_snapshot:
                if not includeSelf and node["num"] == local_node_num:
                    continue

                presumptive_id = f"!{node['num']:08x}"

                # This allows the user to specify fields that wouldn't otherwise be included.
                fields = {}
                for field in showFields:
                    if "." in field:
                        raw_value = _get_nested_value(node, field)
                    else:
                        # The "since" column is synthesized, it's not retrieved from the device. Get the
                        # lastHeard value here, and then we'll format it properly below.
                        if field == "since":
                            raw_value = node.get("lastHeard")
                        else:
                            raw_value = node.get(field)

                    formatted_value: str | None = ""

                    # Some of these need special formatting or processing.
                    if field == "channel":
                        if raw_value is None:
                            formatted_value = "0"
                    elif field == "deviceMetrics.channelUtilization":
                        formatted_value = _format_float(raw_value, 2, "%")
                    elif field == "deviceMetrics.airUtilTx":
                        formatted_value = _format_float(raw_value, 2, "%")
                    elif field == "deviceMetrics.batteryLevel":
                        if raw_value in (0, 101):
                            formatted_value = "Powered"
                        else:
                            formatted_value = _format_float(raw_value, 0, "%")
                    elif field == "isFavorite":
                        formatted_value = "*" if raw_value else ""
                    elif field == "lastHeard":
                        formatted_value = _get_lh(raw_value)
                    elif field == "position.latitude":
                        formatted_value = _format_float(raw_value, 4, "°")
                    elif field == "position.longitude":
                        formatted_value = _format_float(raw_value, 4, "°")
                    elif field == "position.altitude":
                        formatted_value = _format_float(raw_value, 0, "m")
                    elif field == "since":
                        formatted_value = _get_time_ago(raw_value) or "N/A"
                    elif field == "snr":
                        formatted_value = _format_float(raw_value, 0, " dB")
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
                filtered_data = {
                    _get_human_readable(k): v
                    for k, v in fields.items()
                    if k in showFields
                }
                rows.append(filtered_data)

        rows.sort(key=lambda r: r.get("LastHeard") or "0000", reverse=True)
        for i, row in enumerate(rows):
            row["N"] = i + 1

        table = str(
            tabulate(rows, headers="keys", missingval="N/A", tablefmt="fancy_grid")
        )
        print(table)
        return table

    def getNode(
        self,
        nodeId: str,
        requestChannels: bool = True,
        requestChannelAttempts: int = 3,
        timeout: float = 300.0,
    ) -> meshtastic.node.Node:
        """Get the Node object for the given node identifier.

        If nodeId is the local or broadcast address, return the already-initialized local node.
        If requestChannels is True, request channel information from the remote node and retry up to
        requestChannelAttempts until channel data is received or the operation times out.
        Raises MeshInterfaceError if channel retrieval repeatedly fails.

        Parameters
        ----------
        nodeId : str
            Node identifier (hex/node-id string, or LOCAL_ADDR/BROADCAST_ADDR).
        requestChannels : bool
            If True, request channel settings from the remote node. (Default value = True)
        requestChannelAttempts : int
            Number of attempts to retrieve channel info before giving up. (Default value = 3)
        timeout : float
            Timeout in seconds passed to the Node constructor and used while waiting for responses. (Default value = 300.0)

        Returns
        -------
        meshtastic.node.Node
            The Node object corresponding to nodeId.

        Raises
        ------
        MeshInterfaceError
            If channel retrieval repeatedly fails or times out.
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
                            raise self.MeshInterfaceError(
                                "Error: Timed out waiting for channels, giving up"
                            )
                        logger.warning(
                            "Timed out trying to retrieve channel info, retrying"
                        )
                        n.requestChannels(startingIndex=new_index)
                        last_index = new_index
                    else:
                        break
            return n

    def sendText(
        self,
        text: str,
        destinationId: int | str = BROADCAST_ADDR,
        wantAck: bool = False,
        wantResponse: bool = False,
        onResponse: Callable[[dict[str, Any]], Any] | None = None,
        channelIndex: int = 0,
        portNum: portnums_pb2.PortNum.ValueType = portnums_pb2.PortNum.TEXT_MESSAGE_APP,
        replyId: int | None = None,
        hopLimit: int | None = None,
    ) -> mesh_pb2.MeshPacket:
        """Send UTF-8 text to a node (or broadcast) and return the transmitted MeshPacket.

        Parameters
        ----------
        text : str
            Message text to send; encoded as UTF-8 for transport.
        destinationId : int | str
            Target node as numeric node number or node ID string; use BROADCAST_ADDR
            to send to all nodes. (Default value = BROADCAST_ADDR)
        replyId : int | None
            If provided, marks this packet as a response to the given message ID. (Default value = None)
        wantAck : bool
            If True, request transport-level acknowledgment. (Default value = False)
        wantResponse : bool
            If True, request an application-level response. (Default value = False)
        onResponse : Callable[[dict[str, Any]], Any] | None
            Optional callback for the response. (Default value = None)
        channelIndex : int
            Channel index to send on. (Default value = 0)
        portNum : portnums_pb2.PortNum.ValueType
            Application port number for the text message. (Default value = portnums_pb2.PortNum.TEXT_MESSAGE_APP)
        hopLimit : int | None
            Optional hop limit override for the outgoing packet. (Default value = None)

        Returns
        -------
        mesh_pb2.MeshPacket
            The packet that was sent; its `id` field will be populated and can be used to track acknowledgments or naks.
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
            hopLimit=hopLimit,
        )

    def sendAlert(
        self,
        text: str,
        destinationId: int | str = BROADCAST_ADDR,
        onResponse: Callable[[dict[str, Any]], Any] | None = None,
        channelIndex: int = 0,
        hopLimit: int | None = None,
    ) -> mesh_pb2.MeshPacket:
        """Send a high-priority alert text to a node, which may trigger special notifications on clients.

        Parameters
        ----------
        text : str
            Alert text to send.
        destinationId : int | str
            Node ID or node number to receive the alert (defaults to broadcast).
        onResponse : Callable[[dict[str, Any]], Any] | None
            Optional callback invoked if a response is received for this message. (Default value = None)
        channelIndex : int
            Channel index to use when sending. (Default value = 0)
        hopLimit : int | None
            Optional hop limit override for the outgoing packet. (Default value = None)

        Returns
        -------
        mesh_pb2.MeshPacket
            The sent mesh packet with its `id` populated.
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
            hopLimit=hopLimit,
        )

    def sendMqttClientProxyMessage(self, topic: str, data: bytes) -> None:
        """Send an MQTT client-proxy message through the radio.

        Parameters
        ----------
        topic : str
            MQTT topic to forward.
        data : bytes
            MQTT payload to forward.
        """
        prox = mesh_pb2.MqttClientProxyMessage()
        prox.topic = topic
        prox.data = data
        toRadio = mesh_pb2.ToRadio()
        toRadio.mqttClientProxyMessage.CopyFrom(prox)
        self._send_to_radio(toRadio)

    def sendData(  # pylint: disable=R0913
        self,
        data: Any,
        destinationId: int | str = BROADCAST_ADDR,
        portNum: portnums_pb2.PortNum.ValueType = portnums_pb2.PortNum.PRIVATE_APP,
        wantAck: bool = False,
        wantResponse: bool = False,
        onResponse: Callable[[dict[str, Any]], Any] | None = None,
        onResponseAckPermitted: bool = False,
        channelIndex: int = 0,
        hopLimit: int | None = None,
        pkiEncrypted: bool = False,
        publicKey: bytes | None = None,
        priority: mesh_pb2.MeshPacket.Priority.ValueType = mesh_pb2.MeshPacket.Priority.RELIABLE,
        replyId: int | None = None,
    ) -> mesh_pb2.MeshPacket:
        """Send a payload to a mesh node.

        Serializes protobuf payloads when provided, validates payload size and port number,
        assigns a packet id, registers an optional response handler, and transmits the packet.

        Parameters
        ----------
        data : Any
            Payload to send; protobuf messages will be serialized to bytes.
        destinationId : int | str
            Destination node identifier (node id string, numeric node number, BROADCAST_ADDR, or LOCAL_ADDR). (Default value = BROADCAST_ADDR)
        portNum : portnums_pb2.PortNum.ValueType
            Application port number for the payload; must not be UNKNOWN_APP. (Default value = portnums_pb2.PortNum.PRIVATE_APP)
        wantAck : bool
            Request transport-level acknowledgement/retry behavior. (Default value = False)
        wantResponse : bool
            Request an application-layer response from the recipient. (Default value = False)
        onResponse : Callable[[dict[str, Any]], Any] | None
            Optional callback invoked with the response
            packet dict when a response (or permitted ACK/NAK) arrives. (Default value = None)
        onResponseAckPermitted : bool
            If True, invoke onResponse for regular ACKs as well as data responses and NAKs. (Default value = False)
        channelIndex : int
            Channel index to send on. (Default value = 0)
        hopLimit : int | None
            Optional maximum hop count for the packet; None uses the default.
        pkiEncrypted : bool
            If True, request PKI encryption for the packet. (Default value = False)
        publicKey : bytes | None
            Public key to use for PKI encryption when pkiEncrypted is True. (Default value = None)
        priority : mesh_pb2.MeshPacket.Priority.ValueType
            Packet priority enum value to assign to the packet. (Default value = mesh_pb2.MeshPacket.Priority.RELIABLE)
        replyId : int | None
            If provided, marks this packet as a reply to the given message id. (Default value = None)

        Returns
        -------
        mesh_pb2.MeshPacket
            The transmitted MeshPacket with its `id` field populated.

        Raises
        ------
        MeshInterface.MeshInterfaceError
            If the payload exceeds the maximum allowed size, a valid
            packet id cannot be generated, or if an invalid port number is supplied.
        """

        serializer = getattr(data, "SerializeToString", None)
        if callable(serializer):
            logger.debug("Serializing protobuf as data: %s", stripnl(data))
            data = serializer()

        logger.debug("len(data): %s", len(data))
        logger.debug(
            "mesh_pb2.Constants.DATA_PAYLOAD_LEN: %s",
            mesh_pb2.Constants.DATA_PAYLOAD_LEN,
        )
        if len(data) > mesh_pb2.Constants.DATA_PAYLOAD_LEN:
            raise MeshInterface.MeshInterfaceError("Data payload too big")

        if (
            portNum == portnums_pb2.PortNum.UNKNOWN_APP
        ):  # we are now more strict wrt port numbers
            raise MeshInterface.MeshInterfaceError(
                "A non-zero port number must be specified"
            )

        meshPacket = mesh_pb2.MeshPacket()
        meshPacket.channel = channelIndex
        meshPacket.decoded.payload = data
        meshPacket.decoded.portnum = portNum
        meshPacket.decoded.want_response = wantResponse
        meshPacket.id = self._generate_packet_id()
        if replyId is not None:
            meshPacket.decoded.reply_id = replyId
        if priority is not None:
            meshPacket.priority = priority

        if onResponse is not None:
            logger.debug("Setting a response handler for requestId %s", meshPacket.id)
            self._add_response_handler(
                meshPacket.id, onResponse, ackPermitted=onResponseAckPermitted
            )
        p = self._send_packet(
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
        destinationId: int | str = BROADCAST_ADDR,
        wantAck: bool = False,
        wantResponse: bool = False,
        channelIndex: int = 0,
        hopLimit: int | None = None,
    ) -> mesh_pb2.MeshPacket:
        """Send the device's position to a specific node or to broadcast.

        Parameters
        ----------
        latitude : float
            Latitude in degrees; if 0.0 the latitude field is omitted. (Default value = 0.0)
        longitude : float
            Longitude in degrees; if 0.0 the longitude field is omitted. (Default value = 0.0)
        altitude : int
            Altitude in meters; if 0 the altitude field is omitted. (Default value = 0)
        destinationId : int | str
            Destination address or node ID; defaults to broadcast.
        wantAck : bool
            Request an acknowledgment from the recipient. (Default value = False)
        wantResponse : bool
            If True, blocks until a position response is received. (Default value = False)
        channelIndex : int
            Channel index to send the packet on. (Default value = 0)
        hopLimit : int | None
            Optional hop limit override for the outgoing packet. (Default value = None)

        Returns
        -------
        mesh_pb2.MeshPacket
            The sent packet with its `id` populated.
        """
        p = mesh_pb2.Position()
        if latitude != 0.0:
            p.latitude_i = int(latitude / 1e-7)
            logger.debug("p.latitude_i:%s", p.latitude_i)

        if longitude != 0.0:
            p.longitude_i = int(longitude / 1e-7)
            logger.debug("p.longitude_i:%s", p.longitude_i)

        if altitude != 0:
            p.altitude = int(altitude)
            logger.debug("p.altitude:%s", p.altitude)

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
            hopLimit=hopLimit,
        )
        if wantResponse:
            self.waitForPosition()
        return d

    def onResponsePosition(self, p: dict[str, Any]) -> None:
        """Process a position response packet and display a concise human-readable summary.

        Marks the interface's position acknowledgment as received, parses the Position
        protobuf from the packet payload, and prints latitude/longitude (degrees),
        altitude (meters) when present, and precision information. If the packet is a
        routing response whose `decoded["routing"]["errorReason"]` equals `"NO_RESPONSE"`,
        raises MeshInterfaceError with a message about the minimum required firmware.

        Parameters
        ----------
        p : dict[str, Any]
            Decoded packet dictionary expected to contain at minimum
            `decoded["portnum"]` and `decoded["payload"]`. For routing error checks
            the nested `decoded["routing"]["errorReason"]` may be present.

        Raises
        ------
        MeshInterfaceError
            If a routing error occurs or no response is received from the node.
        """
        if p["decoded"]["portnum"] == portnums_pb2.PortNum.Name(
            portnums_pb2.PortNum.POSITION_APP
        ):
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

        elif p["decoded"]["portnum"] == portnums_pb2.PortNum.Name(
            portnums_pb2.PortNum.ROUTING_APP
        ):
            if p["decoded"]["routing"]["errorReason"] == "NO_RESPONSE":
                raise self.MeshInterfaceError(
                    "No response from node. At least firmware 2.1.22 is required on the destination node."
                )

    def sendTraceRoute(
        self, dest: int | str, hopLimit: int, channelIndex: int = 0
    ) -> None:
        """Initiate a traceroute request toward a destination node and wait for responses.

        Sends a RouteDiscovery to the specified destination using the given channel and waits for
        traceroute responses; the waiting period is extended based on the current node count up to
        the provided hopLimit.

        Parameters
        ----------
        dest : int | str
            Destination node (numeric node number, node ID string, or broadcast/local constants).
        hopLimit : int
            Maximum number of hops to probe for the traceroute.
        channelIndex : int
            Channel index to use for transmission. (Default value = 0)

        Raises
        ------
        MeshInterfaceError
            If waiting for traceroute responses times out or the operation fails.
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
        with self._node_db_lock:
            node_count = len(self.nodes) if self.nodes else 0
        nodes_based_factor = (node_count - 1) if node_count else (hopLimit + 1)
        waitFactor = max(1, min(nodes_based_factor, hopLimit + 1))
        self.waitForTraceRoute(waitFactor)

    def onResponseTraceRoute(self, p: dict[str, Any]) -> None:
        """Display human-readable traceroute results from a RouteDiscovery payload.

        Parses the RouteDiscovery protobuf found in p["decoded"]["payload"], prints a forward route
        from the response destination back to the origin and, when present, the traced return route
        back to this node.

        Parameters
        ----------
        p : dict[str, Any]
            The traceroute response packet.

        Notes
        -----
        Prints formatted route strings to stdout and sets self._acknowledgment.receivedTraceRoute to True.
        """
        UNK_SNR = -128  # Value representing unknown SNR

        routeDiscovery = mesh_pb2.RouteDiscovery()
        routeDiscovery.ParseFromString(p["decoded"]["payload"])
        asDict = google.protobuf.json_format.MessageToDict(routeDiscovery)

        def _node_label(node_num: int) -> str:
            """Return a human-readable identifier for a numeric node.

            Parameters
            ----------
            node_num : int
                Numeric node identifier.

            Returns
            -------
            str
                The node's ID string if known; otherwise the node number formatted as an 8-digit lowercase hexadecimal string.
            """
            return self._node_num_to_id(node_num, False) or f"{node_num:08x}"

        def _format_snr(snr_value: int | None) -> str:
            """Format an SNR value encoded in quarter-dB units into a human-readable string.

            Parameters
            ----------
            snr_value : int | None
                SNR expressed as an integer number of quarter dB units,
                or `None`/`UNK_SNR` to indicate an unknown value.

            Returns
            -------
            str
                Formatted SNR in dB as a string (for example, "7.5"), or "?" when the SNR is unknown.
            """
            return (
                str(snr_value / 4)
                if snr_value is not None and snr_value != UNK_SNR
                else "?"
            )

        def _append_hop(route_text: str, node_num: int, snr_text: str) -> str:
            """Append a hop description to an existing route string.

            Parameters
            ----------
            route_text : str
                Current route representation.
            node_num : int
                Numeric node identifier to convert to a human-readable label.
            snr_text : str
                Signal-to-noise ratio value as text (without units).

            Returns
            -------
            str
                The route string with " --> <node_label> (<snr_text>dB)" appended.
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
        destinationId: int | str = BROADCAST_ADDR,
        wantResponse: bool = False,
        channelIndex: int = 0,
        telemetryType: TelemetryType | str = DEFAULT_TELEMETRY_TYPE,
        hopLimit: int | None = None,
    ) -> None:
        """Send a telemetry message to a node or broadcast and optionally wait for a telemetry response.

        Parameters
        ----------
        destinationId : int | str
            Numeric node id, node id string, or the broadcast address to receive the telemetry. (Default value = BROADCAST_ADDR)
        wantResponse : bool
            If true, register a telemetry response handler and wait for the corresponding response. (Default value = False)
        channelIndex : int
            Channel index to use for the outgoing packet. (Default value = 0)
        telemetryType : str
            Telemetry payload to send. Supported values: "environment_metrics", "air_quality_metrics",
            "power_metrics", "local_stats", and "device_metrics". When "device_metrics" is selected and local device
            metrics are available, the payload is populated from the local node's cached device metrics. (Default value = 'device_metrics')
        hopLimit : int | None
            Optional hop limit override for the outgoing packet. (Default value = None)
        """
        telemetry_type = telemetryType
        if telemetry_type not in VALID_TELEMETRY_TYPE_SET:
            # Backwards compatibility: unknown types fall back to device_metrics with deprecation warning
            warnings.warn(
                f"Unsupported telemetryType '{telemetry_type}' is deprecated. "
                f"Supported values: {sorted(VALID_TELEMETRY_TYPES)}. "
                f"Falling back to '{DEFAULT_TELEMETRY_TYPE}'. "
                "This will raise an error in a future version.",
                DeprecationWarning,
                stacklevel=2,
            )
            telemetry_type = DEFAULT_TELEMETRY_TYPE
        r = telemetry_pb2.Telemetry()

        if telemetry_type == "environment_metrics":
            r.environment_metrics.CopyFrom(telemetry_pb2.EnvironmentMetrics())
        elif telemetry_type == "air_quality_metrics":
            r.air_quality_metrics.CopyFrom(telemetry_pb2.AirQualityMetrics())
        elif telemetry_type == "power_metrics":
            r.power_metrics.CopyFrom(telemetry_pb2.PowerMetrics())
        elif telemetry_type == "local_stats":
            r.local_stats.CopyFrom(telemetry_pb2.LocalStats())
        elif telemetry_type == DEFAULT_TELEMETRY_TYPE:
            with self._node_db_lock:
                node = (
                    self.nodesByNum.get(self.localNode.nodeNum)
                    if self.nodesByNum is not None
                    else None
                )
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
            hopLimit=hopLimit,
        )
        if wantResponse:
            self.waitForTelemetry()

    def onResponseTelemetry(self, p: dict[str, Any]) -> None:
        """Handle an incoming telemetry response: mark telemetry as received and print human-readable telemetry values.

        This inspects p["decoded"]["portnum"] and:
        - For TELEMETRY_APP: parses the Telemetry payload, sets the telemetry-received flag on the interface,
          and prints device metrics (battery, voltage, channel/air utilization, uptime) when present;
          for non-device_metrics telemetry, prints top-level keys and their subfields except the protobuf 'time' field.
        - For ROUTING_APP: if routing.errorReason is "NO_RESPONSE", exits with a firmware-requirement message.

        Parameters
        ----------
        p : dict[str, Any]
            Decoded packet dictionary produced by _handle_packet_from_radio. Must contain "decoded"
            with a "portnum" string and either a "payload" (Telemetry protobuf bytes) for TELEMETRY_APP
            or a "routing" mapping for ROUTING_APP.

        Raises
        ------
        MeshInterfaceError
            If the destination node requires a newer firmware version for telemetry responses.
        """
        if p["decoded"]["portnum"] == portnums_pb2.PortNum.Name(
            portnums_pb2.PortNum.TELEMETRY_APP
        ):
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

        elif p["decoded"]["portnum"] == portnums_pb2.PortNum.Name(
            portnums_pb2.PortNum.ROUTING_APP
        ):
            if p["decoded"]["routing"]["errorReason"] == "NO_RESPONSE":
                raise self.MeshInterfaceError(
                    "No response from node. At least firmware 2.1.22 is required on the destination node."
                )

    def onResponseWaypoint(self, p: dict[str, Any]) -> None:
        """Handle a waypoint response or routing error contained in a received packet.

        When the packet's port is WAYPOINT_APP, parse the Waypoint protobuf from decoded['payload'],
        mark the waypoint acknowledgment as received, and print the waypoint. When the packet's port
        is ROUTING_APP and decoded['routing']['errorReason'] == "NO_RESPONSE", raises
        MeshInterfaceError with a message about the minimum firmware requirement.

        Parameters
        ----------
        p : dict[str, Any]
            Packet dictionary containing a 'decoded' mapping. Expected keys include
            'portnum' (str) and, for WAYPOINT_APP, 'payload' (bytes) with a serialized Waypoint.

        Raises
        ------
        MeshInterfaceError
            If the destination node requires a newer firmware version for waypoint responses.
        """
        if p["decoded"]["portnum"] == portnums_pb2.PortNum.Name(
            portnums_pb2.PortNum.WAYPOINT_APP
        ):
            self._acknowledgment.receivedWaypoint = True
            w = mesh_pb2.Waypoint()
            w.ParseFromString(p["decoded"]["payload"])
            print(f"Waypoint received: {w}")
        elif p["decoded"]["portnum"] == portnums_pb2.PortNum.Name(
            portnums_pb2.PortNum.ROUTING_APP
        ):
            if p["decoded"]["routing"]["errorReason"] == "NO_RESPONSE":
                raise self.MeshInterfaceError(
                    "No response from node. At least firmware 2.1.22 is required on the destination node."
                )

    def sendWaypoint(  # pylint: disable=R0913
        self,
        name: str,
        description: str,
        icon: int | str,
        expire: int,
        waypoint_id: int | None = None,
        latitude: float = 0.0,
        longitude: float = 0.0,
        destinationId: int | str = BROADCAST_ADDR,
        wantAck: bool = True,
        wantResponse: bool = False,
        channelIndex: int = 0,
        hopLimit: int | None = None,
    ) -> mesh_pb2.MeshPacket:
        """Send a waypoint to a node or broadcast.

        Parameters
        ----------
        name : str
            Human-readable waypoint name.
        description : str
            Text description for the waypoint.
        icon : int | str
            Icon identifier (will be converted to an integer).
        expire : int
            Expiration time for the waypoint, in seconds.
        waypoint_id : int | None
            Waypoint identifier to use; if None a pseudo-random id is generated. (Default value = None)
        latitude : float
            Latitude in decimal degrees; included only when not 0.0. (Default value = 0.0)
        longitude : float
            Longitude in decimal degrees; included only when not 0.0. (Default value = 0.0)
        destinationId : int | str
            Destination node id or special address (broadcast/local). (Default value = BROADCAST_ADDR)
        wantAck : bool
            If True, request an acknowledgement for the sent packet. (Default value = True)
        wantResponse : bool
            If True, wait for and process a waypoint response before returning. (Default value = False)
        channelIndex : int
            Channel index to send the waypoint on. (Default value = 0)
        hopLimit : int | None
            Optional hop limit override for the outgoing packet. (Default value = None)

        Returns
        -------
        mesh_pb2.MeshPacket
            The MeshPacket that was sent; its `id` is populated for tracking.
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
            logger.debug("w.id:%s", w.id)
        else:
            w.id = waypoint_id
        if latitude != 0.0:
            w.latitude_i = int(latitude * 1e7)
            logger.debug("w.latitude_i:%s", w.latitude_i)
        if longitude != 0.0:
            w.longitude_i = int(longitude * 1e7)
            logger.debug("w.longitude_i:%s", w.longitude_i)

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
            hopLimit=hopLimit,
        )
        if wantResponse:
            self.waitForWaypoint()
        return d

    def deleteWaypoint(
        self,
        waypoint_id: int,
        destinationId: int | str = BROADCAST_ADDR,
        wantAck: bool = True,
        wantResponse: bool = False,
        channelIndex: int = 0,
        hopLimit: int | None = None,
    ) -> mesh_pb2.MeshPacket:
        """Delete a waypoint by sending a Waypoint message with expire=0 to a destination.

        Parameters
        ----------
        waypoint_id : int
            The waypoint's identifier to delete (the waypoint id, not a packet id).
        destinationId : int | str
            Destination node numeric id or address string; defaults to broadcast.
        wantAck : bool
            Request an acknowledgement for the transmitted packet when True. (Default value = True)
        wantResponse : bool
            If True, wait for and process a waypoint response before returning. (Default value = False)
        channelIndex : int
            Channel index to send the packet on. (Default value = 0)
        hopLimit : int | None
            Optional hop limit override for the outgoing packet. (Default value = None)

        Returns
        -------
        mesh_pb2.MeshPacket
            The MeshPacket that was sent; its `id` field is populated and can be used to track acknowledgements.
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
            hopLimit=hopLimit,
        )
        if wantResponse:
            self.waitForWaypoint()
        return d

    def _add_response_handler(
        self,
        requestId: int,
        callback: Callable[[dict[str, Any]], Any],
        ackPermitted: bool = False,
    ) -> None:
        """Register a response callback for a specific request identifier.

        Parameters
        ----------
        requestId : int
            Request identifier used to match incoming responses to this handler.
        callback : Callable[[dict[str, Any]], Any]
            Function called with the decoded response packet
            dictionary when a matching response is received.
        ackPermitted : bool
            If True, allow acknowledgement/negative-acknowledgement packets
            to invoke the callback; if False, only full responses will invoke it. (Default value = False)
        """
        with self._response_handlers_lock:
            self.responseHandlers[requestId] = ResponseHandler(
                callback=callback, ackPermitted=ackPermitted
            )

    def _send_packet(
        self,
        meshPacket: mesh_pb2.MeshPacket,
        destinationId: int | str = BROADCAST_ADDR,
        wantAck: bool = False,
        hopLimit: int | None = None,
        pkiEncrypted: bool | None = False,
        publicKey: bytes | None = None,
    ) -> mesh_pb2.MeshPacket:
        """Send a MeshPacket to a specific node or broadcast.

        Parameters
        ----------
        meshPacket : mesh_pb2.MeshPacket
            The packet to send; fields such as payload and port should be set by the caller.
        destinationId : int | str
            Destination identifier — can be a node
            number (int), a node ID string (hex-style), or the constants
            BROADCAST_ADDR or LOCAL_ADDR. Determines the packet's `to`
            field. (Default value = BROADCAST_ADDR)
        wantAck : bool
            If true, request an acknowledgement from the recipient; sets the packet's `want_ack`. (Default value = False)
        hopLimit : int | None
            Maximum hop count for the packet. If omitted, the local node's LoRa `hop_limit` is used. (Default value = None)
        pkiEncrypted : bool | None
            If true, mark the packet as PKI-encrypted. (Default value = False)
        publicKey : bytes | None
            Optional public key to include on the packet. (Default value = None)

        Returns
        -------
        mesh_pb2.MeshPacket
            The same MeshPacket instance that was sent,
            with transport-related fields populated (for example: `to`,
            `want_ack`, `hop_limit`, `id`, and encryption/public key
            fields).

        Raises
        ------
        MeshInterface.MeshInterfaceError
            If the destination node is not found in the node database or if no info is available.
        """

        with self._node_db_lock:
            my_node_num = self.myInfo.my_node_num if self.myInfo is not None else None

        # We allow users to talk to the local node before we've completed the full connection flow...
        if my_node_num is not None and destinationId != my_node_num:
            self._wait_connected()

        toRadio = mesh_pb2.ToRadio()

        nodeNum: int = 0
        if destinationId is None:
            raise MeshInterface.MeshInterfaceError("destinationId must not be None")
        elif isinstance(destinationId, int):
            nodeNum = destinationId
        elif destinationId == BROADCAST_ADDR:
            nodeNum = BROADCAST_NUM
        elif destinationId == LOCAL_ADDR:
            if my_node_num is not None:
                nodeNum = my_node_num
            else:
                raise MeshInterface.MeshInterfaceError("No myInfo found.")
        # A simple hex style nodeid - we can parse this without needing the DB
        elif isinstance(destinationId, str) and len(destinationId) >= 8:
            # assuming some form of node id string such as !1234578 or 0x12345678
            # always grab the last 8 items of the hexadecimal id str and parse to integer
            nodeNum = int(destinationId[-8:], 16)
        else:
            with self._node_db_lock:
                node = self.nodes.get(destinationId) if self.nodes else None
                has_nodes = self.nodes is not None
            if node is not None:
                node_num = node.get("num") if isinstance(node, dict) else None
                if isinstance(node_num, int):
                    nodeNum = node_num
                else:
                    raise MeshInterface.MeshInterfaceError(
                        f"NodeId {destinationId} has no numeric 'num' in DB"
                    )
            elif has_nodes:
                raise MeshInterface.MeshInterfaceError(
                    f"NodeId {destinationId} not found in DB"
                )
            else:
                logger.warning("Warning: There were no self.nodes.")

        meshPacket.to = nodeNum
        meshPacket.want_ack = wantAck

        if hopLimit is not None:
            meshPacket.hop_limit = hopLimit
        else:
            with self._node_db_lock:
                default_hop_limit = self.localNode.localConfig.lora.hop_limit
            meshPacket.hop_limit = default_hop_limit

        if pkiEncrypted:
            meshPacket.pki_encrypted = True

        if publicKey is not None:
            meshPacket.public_key = publicKey

        # if the user hasn't set an ID for this packet (likely and recommended),
        # we should pick a new unique ID so the message can be tracked.
        if meshPacket.id == 0:
            meshPacket.id = self._generate_packet_id()

        toRadio.packet.CopyFrom(meshPacket)
        if self.noProto:
            logger.warning(
                "Not sending packet because protocol use is disabled by noProto"
            )
        else:
            logger.debug("Sending packet: %s", stripnl(meshPacket))
            self._send_to_radio(toRadio)
        return meshPacket

    def waitForConfig(self) -> None:
        """Block until the radio configuration and the local node's configuration are available.

        Raises
        ------
        MeshInterface.MeshInterfaceError
            If the configuration is not received before the interface timeout.
        """
        success = (
            self._timeout.waitForSet(self, attrs=("myInfo", "nodes"))
            and self.localNode.waitForConfig()
        )
        if not success:
            raise MeshInterface.MeshInterfaceError(
                "Timed out waiting for interface config"
            )

    def waitForAckNak(self) -> None:
        """Wait until an acknowledgement (ACK) or negative acknowledgement (NAK) is received or the wait times out.

        Raises
        ------
        MeshInterface.MeshInterfaceError
            If waiting times out before an ACK/NAK is received.
        """
        success = self._timeout.waitForAckNak(self._acknowledgment)
        if not success:
            raise MeshInterface.MeshInterfaceError(
                "Timed out waiting for an acknowledgment"
            )

    def waitForTraceRoute(self, waitFactor: float) -> None:
        """Wait for trace route completion using the configured timeout.

        Blocks until a trace route response is acknowledged or the configured timeout multiplied by waitFactor elapses.

        Parameters
        ----------
        waitFactor : float
            Multiplier applied to the base trace-route timeout to extend the wait period.

        Raises
        ------
        MeshInterface.MeshInterfaceError
            If the wait times out before a traceroute response is received.
        """
        success = self._timeout.waitForTraceRoute(waitFactor, self._acknowledgment)
        if not success:
            raise MeshInterface.MeshInterfaceError("Timed out waiting for traceroute")

    def waitForTelemetry(self) -> None:
        """Wait for a telemetry response or until the configured timeout elapses.

        Raises
        ------
        MeshInterface.MeshInterfaceError
            If a telemetry response is not received before the configured timeout.
        """
        success = self._timeout.waitForTelemetry(self._acknowledgment)
        if not success:
            raise MeshInterface.MeshInterfaceError("Timed out waiting for telemetry")

    def waitForPosition(self) -> None:
        """Block until a position acknowledgment is received.

        Raises
        ------
        MeshInterface.MeshInterfaceError
            If waiting for the position times out.
        """
        success = self._timeout.waitForPosition(self._acknowledgment)
        if not success:
            raise MeshInterface.MeshInterfaceError("Timed out waiting for position")

    def waitForWaypoint(self) -> None:
        """Block until a waypoint acknowledgment is received.

        Waits for the internal waypoint acknowledgment event to be set; raises MeshInterface.MeshInterfaceError if the wait times out.

        Raises
        ------
        MeshInterface.MeshInterfaceError
            If the wait times out before a waypoint acknowledgment is received.
        """
        success = self._timeout.waitForWaypoint(self._acknowledgment)
        if not success:
            raise MeshInterface.MeshInterfaceError("Timed out waiting for waypoint")

    def getMyNodeInfo(self) -> dict[str, Any] | None:
        """Get the stored node-info dictionary for the local node.

        Returns
        -------
        dict[str, Any] | None
            The local node's node-info entry from `nodesByNum`, or `None` if `myInfo`
            or `nodesByNum` is unset or the local node entry is missing.
        """
        with self._node_db_lock:
            if self.myInfo is None or self.nodesByNum is None:
                return None
            logger.debug("self.nodesByNum:%s", self.nodesByNum)
            return self.nodesByNum.get(self.myInfo.my_node_num)

    def getMyUser(self) -> dict[str, Any] | None:
        """Get the user information for the local node.

        Returns
        -------
        user : dict[str, Any] | None
            The local node's `user` dictionary, or `None` if no local node info or no `user` field is present.
        """
        nodeInfo = self.getMyNodeInfo()
        if nodeInfo is not None:
            return nodeInfo.get("user")
        return None

    def getLongName(self) -> str | None:
        """Get the local user's configured long name.

        Returns
        -------
        str | None
            The long name string if configured, `None` otherwise.
        """
        user = self.getMyUser()
        if user is not None:
            return user.get("longName", None)
        return None

    def getShortName(self) -> str | None:
        """Get the local node user's short name.

        Returns
        -------
        str | None
            The user's `shortName` if present, `None` otherwise.
        """
        user = self.getMyUser()
        if user is not None:
            return user.get("shortName", None)
        return None

    def getPublicKey(self) -> bytes | None:
        """Return the local node's public key if available.

        Returns
        -------
        bytes | None
            The local node's public key bytes if present, `None` otherwise.
        """
        user = self.getMyUser()
        if user is not None:
            return user.get("publicKey", None)
        return None

    def getCannedMessage(self) -> str | None:
        """Retrieve the canned (predefined) message configured for the local node.

        Returns
        -------
        str | None
            The canned message text, or `None` if there is no local node or no canned message configured.
        """
        node = self.localNode
        if node is not None:
            return node.get_canned_message()
        return None

    def getRingtone(self) -> str | None:
        """Get the local node's ringtone name or identifier.

        Returns
        -------
        str | None
            The ringtone name or identifier as a string, or None if the local node or ringtone is unavailable.
        """
        node = self.localNode
        if node is not None:
            return node.get_ringtone()
        return None

    def _wait_connected(self, timeout: float = 30.0) -> None:
        """Wait until the interface is marked connected or the timeout elapses.

        Parameters
        ----------
        timeout : float
            Maximum seconds to wait for the connection. (Default value = 30.0)

        Raises
        ------
        MeshInterface.MeshInterfaceError
            If waiting timed out.
        Exception
            Re-raises a stored fatal exception if one occurred during connection.
        """
        if not self.noProto:
            if not self.isConnected.wait(timeout):  # timeout after x seconds
                raise MeshInterface.MeshInterfaceError(
                    "Timed out waiting for connection completion"
                )

        # If we failed while connecting, raise the connection to the client
        if self.failure is not None:
            raise self.failure

    def _generate_packet_id(self) -> int:
        """Generate a new 32-bit packet identifier combining a 10-bit monotonic counter with randomized upper bits.

        Returns
        -------
        packet_id : int
            New packet id where the low 10 bits are a monotonic counter and the remaining bits are randomized.

        Raises
        ------
        MeshInterface.MeshInterfaceError
            If `currentPacketId` is None and a packet id cannot be generated.
        """
        with self._packet_id_lock:
            if self.currentPacketId is None:
                raise MeshInterface.MeshInterfaceError(
                    "Not connected yet, can not generate packet"
                )
            next_packet_id = (self.currentPacketId + 1) & PACKET_ID_MASK
            next_packet_id = (
                next_packet_id & PACKET_ID_COUNTER_MASK
            )  # Keep only low 10-bit counter (clear upper 22 bits)
            random_part = (
                random.randint(0, PACKET_ID_RANDOM_MAX)
                << PACKET_ID_RANDOM_SHIFT_BITS  # noqa: S311
            ) & PACKET_ID_MASK  # generate number with 10 zeros at end
            self.currentPacketId = next_packet_id | random_part  # combine
            return self.currentPacketId

    def _disconnected(self) -> None:
        """Mark the interface as disconnected and publish meshtastic.connection.lost once per connection.

        Clears the internal connected flag and publishes a lost-connection
        notification only if the interface was previously connected. This keeps
        shutdown/retry paths from publishing duplicate "lost" events.
        """
        was_connected = self.isConnected.is_set()
        self.isConnected.clear()
        if was_connected:
            publishingThread.queueWork(
                lambda: pub.sendMessage("meshtastic.connection.lost", interface=self)
            )

    def sendHeartbeat(self) -> None:
        """Send a heartbeat message to the radio to indicate the interface is alive."""
        p = mesh_pb2.ToRadio()
        p.heartbeat.CopyFrom(mesh_pb2.Heartbeat())
        self._send_to_radio(p)

    def _start_heartbeat(self) -> None:
        """Start a recurring daemon timer that sends a heartbeat to the radio every 300 seconds.

        Schedules and runs the first heartbeat immediately and then re-schedules a daemon
        threading.Timer to invoke subsequent heartbeats at a fixed 300-second interval. The
        scheduler respects shutdown by checking self._closing and uses self._heartbeat_lock to
        avoid scheduling or storing timers after shutdown begins. Heartbeat sends are tracked
        as in-flight so close() can wait for quiescence and guarantee no post-close sends. The
        actual call to self.sendHeartbeat() is still performed outside the lock.
        """

        def callback() -> None:
            """Schedule the next heartbeat and emit one unless the interface is shutting down.

            Schedules a daemon timer to re-run this callback after a fixed interval and then calls
            self.sendHeartbeat(). If self._closing is set, no timer is scheduled and no heartbeat is sent.
            """
            interval = HEARTBEAT_INTERVAL_SECONDS
            logger.debug("Sending heartbeat, interval %s seconds", interval)
            with self._heartbeat_lock:
                # Keep timer update/start in one critical section for simpler
                # state reasoning while still honoring shutdown.
                if self._closing:
                    return
                self.heartbeatTimer = None
                timer = threading.Timer(interval, callback)
                # Heartbeat maintenance should never prevent process shutdown.
                timer.daemon = True
                self.heartbeatTimer = timer
                timer.start()
                # Mark this send as in-flight before releasing the lock so
                # close() cannot miss it when establishing shutdown quiescence.
                self._heartbeat_inflight += 1
            # sendHeartbeat() is intentionally outside the lock to avoid
            # holding the lock during I/O. close() handles timer cancellation.
            try:
                self.sendHeartbeat()
            finally:
                with self._heartbeat_lock:
                    self._heartbeat_inflight -= 1
                    if self._heartbeat_inflight == 0:
                        self._heartbeat_idle_condition.notify_all()

        callback()  # run our periodic callback now, it will make another timer if necessary

    def _connected(self) -> None:
        """Mark the interface as connected, start the heartbeat timer, and publish a connection-established event.

        If the interface is shutting down, do nothing. Otherwise set the
        internal connected event, start periodic heartbeats, and enqueue a
        "meshtastic.connection.established" publication.
        """
        start_heartbeat = False
        with self._heartbeat_lock:
            if self._closing:
                logger.debug("Skipping _connected(): interface is closing")
                return
            # (because I'm lazy) _connected might be called when remote Node
            # objects complete their config reads, don't generate redundant isConnected
            # for the local interface
            if not self.isConnected.is_set():
                self.isConnected.set()
                start_heartbeat = True
        if start_heartbeat:
            self._start_heartbeat()
        # Check _closing again before publishing to avoid race with close()
        with self._heartbeat_lock:
            # Publish once per disconnected->connected transition.
            should_publish = start_heartbeat and not self._closing
        if should_publish:
            publishingThread.queueWork(
                lambda: pub.sendMessage(
                    "meshtastic.connection.established", interface=self
                )
            )

    def _start_config(self) -> None:
        """Initialize internal node/config state and request the radio's configuration.

        Resets local state used during configuration (myInfo, nodes, nodesByNum, and local channel list),
        allocates a new non-conflicting configId when appropriate, and sends a ToRadio message
        requesting configuration using the chosen configId.
        """
        with self._node_db_lock:
            self.myInfo = None
            self.nodes = {}  # nodes keyed by ID
            self.nodesByNum = {}  # nodes keyed by nodenum
            self._localChannels = (
                []
            )  # empty until we start getting channels pushed from the device (during config)
            config_id = self.configId
            if config_id is None or not self.noNodes:
                config_id = random.randint(0, PACKET_ID_MASK)
                if config_id == NODELESS_WANT_CONFIG_ID:
                    config_id = config_id + 1
                self.configId = config_id

        startConfig = mesh_pb2.ToRadio()
        startConfig.want_config_id = config_id
        self._send_to_radio(startConfig)

    def _send_disconnect(self) -> None:
        """Notify the radio device that this interface is disconnecting."""
        m = mesh_pb2.ToRadio()
        m.disconnect = True
        self._send_to_radio(m)

    def _queue_has_free_space(self) -> bool:
        # We never got queueStatus, maybe the firmware is old
        """Indicate whether the cached transmit queue has free slots.

        Returns
        -------
        bool
            `True` if at least one free slot is available or the queue status is unknown, `False` otherwise.
        """
        with self._queue_lock:
            if self.queueStatus is None:
                return True
            return self.queueStatus.free > 0

    def _queue_claim(self) -> None:
        """Decrement the cached transmit-queue free-slot counter when a packet is claimed.

        Does nothing if queue status information is not available.
        """
        with self._queue_lock:
            if self.queueStatus is None:
                return
            self.queueStatus.free -= 1

    def _queue_pop_for_send(self) -> tuple[int, mesh_pb2.ToRadio | bool] | None:
        """Atomically pop the next queued packet if TX queue state permits sending.

        Returns
        -------
        tuple[int, mesh_pb2.ToRadio | bool] | None
            The popped queue entry `(packet_id, payload)` when available and sendable,
            otherwise `None`.
        """
        with self._queue_lock:
            if not self.queue:
                return None
            if self.queueStatus is not None and self.queueStatus.free <= 0:
                return None
            to_resend = self.queue.popitem(last=False)
            if self.queueStatus is not None:
                self.queueStatus.free -= 1
            return to_resend

    def _send_to_radio(self, toRadio: mesh_pb2.ToRadio) -> None:
        """Queue and transmit a ToRadio protobuf to the radio device.

        If the ToRadio has a MeshPacket in its `packet` field, that packet is enqueued and will be
        transmitted when TX queue space is available; otherwise the message is sent immediately.
        The method respects the interface's `noProto` setting (no transmission when disabled),
        may block while waiting for TX queue space, and preserves/requeues packets that remain unacknowledged.

        Parameters
        ----------
        toRadio : mesh_pb2.ToRadio
            The ToRadio protobuf to send; if it contains a `packet` field
            the contained MeshPacket will be queued for transmission.
        """
        if self.noProto:
            logger.warning(
                "Not sending packet because protocol use is disabled by noProto"
            )
            return

        # logger.debug(f"Sending toRadio: {stripnl(toRadio)}")
        if not toRadio.HasField("packet"):
            # not a meshpacket -- send immediately, give queue a chance,
            # this makes heartbeat trigger queue
            self._send_to_radio_impl(toRadio)
        else:
            # meshpacket -- queue
            with self._queue_lock:
                self.queue[toRadio.packet.id] = toRadio

        resentQueue = collections.OrderedDict()

        while True:
            toResend = self._queue_pop_for_send()
            if toResend is None:
                with self._queue_lock:
                    queue_has_items = bool(self.queue)
                if not queue_has_items:
                    break
                logger.debug("Waiting for free space in TX Queue")
                time.sleep(QUEUE_WAIT_DELAY_SECONDS)
                continue

            packetId, packet = toResend
            # logger.warn(f"packet: {packetId:08x} {packet}")
            resentQueue[packetId] = packet
            if not isinstance(packet, mesh_pb2.ToRadio):
                continue
            if packet != toRadio:
                logger.debug("Resending packet ID %08x %s", packetId, packet)
            self._send_to_radio_impl(packet)

        # logger.warn("resentQueue: " + " ".join(f'{k:08x}' for k in resentQueue))
        for packetId, packet in resentQueue.items():
            with self._queue_lock:
                # Returns False if packetId is absent (default) or if a
                # False marker was stored for an earlier unexpected ACK.
                # Both cases indicate "already ACKed" for resend purposes.
                acked = self.queue.pop(packetId, False) is False
            if acked:  # Packet got acked under us
                logger.debug("packet %08x got acked under us", packetId)
                continue
            if packet:
                with self._queue_lock:
                    self.queue[packetId] = packet
        # logger.warn("queue + resentQueue: " + " ".join(f'{k:08x}' for k in self.queue))

    def _send_to_radio_impl(self, toRadio: mesh_pb2.ToRadio) -> None:
        """Transport hook that delivers a ToRadio protobuf to the radio device.

        Subclasses must override this method to perform the actual transmission; the base
        implementation logs an error when invoked.

        Parameters
        ----------
        toRadio : mesh_pb2.ToRadio
            Protobuf describing the action or packet to send to the radio.
        """
        logger.error(f"Subclass must provide toradio: {toRadio}")

    def _handle_config_complete(self) -> None:
        """Finalize initial configuration by applying collected local channels and marking the interface as connected.

        Sets the local node's channels from the internally collected
        _localChannels and invokes _connected() to signal that configuration is
        complete and normal packet handling may begin.
        """
        # This is no longer necessary because the current protocol statemachine has already proactively sent us the locally visible channels
        # self.localNode.requestChannels()
        with self._node_db_lock:
            local_channels = list(self._localChannels)
        self.localNode.setChannels(local_channels)

        # the following should only be called after we have settings and channels
        self._connected()  # Tell everyone else we are ready to go

    def _handle_queue_status_from_radio(
        self, queueStatus: mesh_pb2.QueueStatus
    ) -> None:
        """Update internal transmit-queue state from a received QueueStatus message.

        Sets self.queueStatus and logs the reported free/total slots and packet id.
        If queueStatus.res is falsy, removes the entry for queueStatus.mesh_packet_id from
        self.queue; if no entry exists and mesh_packet_id is nonzero, records a False
        marker for that id to indicate an unexpected reply was observed.

        Parameters
        ----------
        queueStatus : mesh_pb2.QueueStatus
            An object (protobuf-like) with attributes `free`, `maxlen`,
            `res`, and `mesh_packet_id` describing the radio's transmit-queue state.
        """
        with self._queue_lock:
            self.queueStatus = queueStatus
        logger.debug(
            f"TX QUEUE free {queueStatus.free} of {queueStatus.maxlen}, res = {queueStatus.res}, id = {queueStatus.mesh_packet_id:08x} "
        )

        if queueStatus.res:
            return

        # logger.warn("queue: " + " ".join(f'{k:08x}' for k in self.queue))
        with self._queue_lock:
            justQueued = self.queue.pop(queueStatus.mesh_packet_id, None)

        if justQueued is None and queueStatus.mesh_packet_id != 0:
            with self._queue_lock:
                self.queue[queueStatus.mesh_packet_id] = False
            logger.debug(
                f"Reply for unexpected packet ID {queueStatus.mesh_packet_id:08x}"
            )
        # logger.warn("queue: " + " ".join(f'{k:08x}' for k in self.queue))

    def _handle_from_radio(self, fromRadioBytes: bytes) -> None:
        """Handle a raw FromRadio protobuf payload: update interface state and publish corresponding events.

        Processes the provided protobuf bytes to update myInfo, metadata, node records, local configuration/moduleConfig,
        and queue status; dispatches received channel, packet, log, client notification, MQTT proxy, and XMODEM events;
        and on reboot marks the interface disconnected and restarts configuration download.

        Parameters
        ----------
        fromRadioBytes : bytes
            Raw protobuf bytes representing a mesh_pb2.FromRadio message.

        Raises
        ------
        Exception
            If parsing the protobuf fails (parse errors are logged and re-raised).
        """
        fromRadio = mesh_pb2.FromRadio()
        logger.debug(
            "in mesh_interface.py _handle_from_radio() fromRadioBytes: %r",
            fromRadioBytes,
        )
        try:
            fromRadio.ParseFromString(fromRadioBytes)
        except Exception:
            logger.exception("Error while parsing FromRadio bytes:%s", fromRadioBytes)
            raise
        asDict = google.protobuf.json_format.MessageToDict(fromRadio)
        logger.debug("Received from radio: %s", fromRadio)
        with self._node_db_lock:
            config_id = self.configId
        if fromRadio.HasField("my_info"):
            with self._node_db_lock:
                my_info = mesh_pb2.MyNodeInfo()
                my_info.CopyFrom(fromRadio.my_info)
                self.myInfo = my_info
                self.localNode.nodeNum = my_info.my_node_num
            logger.debug("Received myinfo: %s", stripnl(fromRadio.my_info))

        elif fromRadio.HasField("metadata"):
            with self._node_db_lock:
                metadata = mesh_pb2.DeviceMetadata()
                metadata.CopyFrom(fromRadio.metadata)
                self.metadata = metadata
            logger.debug("Received device metadata: %s", stripnl(fromRadio.metadata))

        elif fromRadio.HasField("node_info"):
            logger.debug("Received nodeinfo: %s", asDict["nodeInfo"])

            with self._node_db_lock:
                node = self._get_or_create_by_num(asDict["nodeInfo"]["num"])
                node.update(asDict["nodeInfo"])
                try:
                    newpos = self._fixup_position(node["position"])
                    node["position"] = newpos
                except KeyError:
                    logger.debug("Node has no position key")

                # no longer necessary since we're mutating directly in nodesByNum via _get_or_create_by_num
                # self.nodesByNum[node["num"]] = node
                if "user" in node:  # Some nodes might not have user/ids assigned yet
                    if "id" in node["user"]:
                        # Keep nodes and nodesByNum mutation under the same lock
                        # so readers never observe partially-updated node mappings.
                        if self.nodes is not None:
                            self.nodes[node["user"]["id"]] = node
            publishingThread.queueWork(
                lambda: pub.sendMessage(
                    "meshtastic.node.updated", node=node, interface=self
                )
            )
        elif fromRadio.config_complete_id == config_id:
            # we ignore the config_complete_id, it is unneeded for our
            # stream API fromRadio.config_complete_id
            logger.debug("Config complete ID %s", config_id)
            self._handle_config_complete()
        elif fromRadio.HasField("channel"):
            self._handle_channel(fromRadio.channel)
        elif fromRadio.HasField("packet"):
            self._handle_packet_from_radio(fromRadio.packet)
        elif fromRadio.HasField("log_record"):
            self._handle_log_record(fromRadio.log_record)
        elif fromRadio.HasField("queueStatus"):
            self._handle_queue_status_from_radio(fromRadio.queueStatus)
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

            self._start_config()  # redownload the node db etc...

        elif fromRadio.HasField("config") or fromRadio.HasField("moduleConfig"):
            with self._node_db_lock:
                if fromRadio.config.HasField("device"):
                    self.localNode.localConfig.device.CopyFrom(fromRadio.config.device)
                elif fromRadio.config.HasField("position"):
                    self.localNode.localConfig.position.CopyFrom(
                        fromRadio.config.position
                    )
                elif fromRadio.config.HasField("power"):
                    self.localNode.localConfig.power.CopyFrom(fromRadio.config.power)
                elif fromRadio.config.HasField("network"):
                    self.localNode.localConfig.network.CopyFrom(
                        fromRadio.config.network
                    )
                elif fromRadio.config.HasField("display"):
                    self.localNode.localConfig.display.CopyFrom(
                        fromRadio.config.display
                    )
                elif fromRadio.config.HasField("lora"):
                    self.localNode.localConfig.lora.CopyFrom(fromRadio.config.lora)
                elif fromRadio.config.HasField("bluetooth"):
                    self.localNode.localConfig.bluetooth.CopyFrom(
                        fromRadio.config.bluetooth
                    )
                elif fromRadio.config.HasField("security"):
                    self.localNode.localConfig.security.CopyFrom(
                        fromRadio.config.security
                    )
                elif fromRadio.moduleConfig.HasField("mqtt"):
                    self.localNode.moduleConfig.mqtt.CopyFrom(
                        fromRadio.moduleConfig.mqtt
                    )
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
                    self.localNode.moduleConfig.audio.CopyFrom(
                        fromRadio.moduleConfig.audio
                    )
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
                elif fromRadio.moduleConfig.HasField("traffic_management"):
                    self.localNode.moduleConfig.traffic_management.CopyFrom(
                        fromRadio.moduleConfig.traffic_management
                    )

        else:
            logger.debug("Unexpected FromRadio payload")

    def _fixup_position(self, position: dict[str, Any]) -> dict[str, Any]:
        """Convert integer micro-degree coordinates in a position dict to floating-point degrees.

        If present, 'latitudeI' and 'longitudeI' are converted to 'latitude' and 'longitude'
        by multiplying by 1e-7 (micro-degrees -> degrees) and stored back into the same dict.

        Parameters
        ----------
        position : dict[str, Any]
            Position dictionary that may contain integer keys 'latitudeI' and 'longitudeI'.

        Returns
        -------
        dict[str, Any]
            The same position dictionary with 'latitude' and/or 'longitude' set to float degrees when corresponding integer fields were present.
        """
        if "latitudeI" in position:
            position["latitude"] = position["latitudeI"] * 1e-7
        if "longitudeI" in position:
            position["longitude"] = position["longitudeI"] * 1e-7
        return position

    def _node_num_to_id(self, num: int, isDest: bool = True) -> str | None:
        """Map a mesh numeric node number to its node ID string or a broadcast/unknown literal.

        If num equals the broadcast numeric constant, returns BROADCAST_ADDR when isDest is True
        or the string "Unknown" when isDest is False. Otherwise looks up and returns the stored
        user ID for that node number.

        Parameters
        ----------
        isDest : bool
            When True treat the broadcast number as a destination (return
            BROADCAST_ADDR); when False treat it as an unknown source (return "Unknown"). (Default value = True)
        num : int
            Numeric node identifier.

        Returns
        -------
        str | None
            The node ID string, BROADCAST_ADDR for broadcast destinations, "Unknown" for
            broadcast sources, or `None` if the node number is not present in the local node map.
        """
        if num == BROADCAST_NUM:
            return BROADCAST_ADDR if isDest else "Unknown"

        with self._node_db_lock:
            nodes = self.nodesByNum
            if nodes is None:
                logger.debug(
                    "Node database not initialized while resolving node id for %s", num
                )
                return None
            node = nodes.get(num)
            if not isinstance(node, dict):
                logger.debug("Node %s not found for fromId", num)
                return None
            user = node.get("user")
            if not isinstance(user, dict):
                logger.debug("Node %s has no user payload for fromId", num)
                return None
            node_id = user.get("id")
            if not isinstance(node_id, str):
                logger.debug("Node %s user payload has no valid id", num)
                return None
            return node_id

    def _get_or_create_by_num(self, nodeNum: int) -> dict[str, Any]:
        """Retrieve the node record for a numeric node ID, creating a minimal placeholder if none exists.

        Parameters
        ----------
        nodeNum : int
            Numeric node identifier.

        Returns
        -------
        dict[str, Any]
            The node info dictionary stored in self.nodesByNum for the given nodeNum.

        Raises
        ------
        MeshInterface.MeshInterfaceError
            If nodeNum is the broadcast node number or if the node database has not been initialized.
        """
        if nodeNum == BROADCAST_NUM:
            raise MeshInterface.MeshInterfaceError(
                "Can not create/find nodenum by the broadcast num"
            )

        with self._node_db_lock:
            if self.nodesByNum is None:
                raise MeshInterface.MeshInterfaceError("Node database not initialized")

            if nodeNum in self.nodesByNum:
                return self.nodesByNum[nodeNum]
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

    def _handle_channel(self, channel: channel_pb2.Channel) -> None:
        """Record a received local channel descriptor for later configuration.

        Parameters
        ----------
        channel : channel_pb2.Channel
            Channel descriptor to append to the internal _localChannels list.
        """
        with self._node_db_lock:
            self._localChannels.append(channel)

    def _handle_packet_from_radio(
        self, meshPacket: mesh_pb2.MeshPacket, hack: bool = False
    ) -> None:
        """Process an incoming MeshPacket from the radio and publish an appropriate meshtastic.receive event.

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
        meshPacket : mesh_pb2.MeshPacket
            protobuf MeshPacket instance received from the radio.
        hack : bool
            When True, skip the usual check that requires a "from" field so unit
            tests can construct packets lacking that field. (Default value = False)

        Returns
        -------
        None
            This method does not return a value.
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
            return
        if "to" not in asDict:
            asDict["to"] = 0

        # /add fromId and toId fields based on the node ID
        try:
            asDict["fromId"] = self._node_num_to_id(asDict["from"], False)
        except Exception as ex:
            logger.warning("Not populating fromId: %s", ex, exc_info=True)
        try:
            asDict["toId"] = self._node_num_to_id(asDict["to"])
        except Exception as ex:
            logger.warning("Not populating toId: %s", ex, exc_info=True)

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
                logger.debug("Got a response for requestId %s", requestId)
                # We ignore ACK packets unless the callback is named `onAckNak`
                # or the handler is set as ackPermitted, but send NAKs and
                # other, data-containing responses to the handlers
                routing = decoded.get("routing")
                isAck = routing is not None and (
                    "errorReason" not in routing or routing["errorReason"] == "NONE"
                )
                response_handler: ResponseHandler | None = None
                # Keep lookup/eligibility/pop atomic under the response-handler lock
                # so a competing thread cannot remove the handler between checks.
                with self._response_handlers_lock:
                    candidate = self.responseHandlers.get(requestId, None)
                    if candidate is not None and (
                        (not isAck)
                        or candidate.callback.__name__ == "onAckNak"
                        or candidate.ackPermitted
                    ):
                        response_handler = self.responseHandlers.pop(requestId, None)
                if response_handler is not None:
                    logger.debug("Calling response handler for requestId %s", requestId)
                    try:
                        response_handler.callback(asDict)
                    except Exception:
                        logger.exception(
                            "Error in response handler for requestId %s",
                            requestId,
                        )

        logger.debug("Publishing %s: packet=%s", topic, stripnl(asDict))
        publishingThread.queueWork(
            lambda: pub.sendMessage(topic, packet=asDict, interface=self)
        )
