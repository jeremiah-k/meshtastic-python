"""NodeView - node accessor and presentation methods for MeshInterface."""

import base64
import copy
import sys
from datetime import datetime
from typing import IO, Any, TypeAlias, cast

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
)
from meshtastic.protobuf import mesh_pb2
from meshtastic.util import (
    convert_mac_addr,
    messageToJson,
    remove_keys_from_dict,
)

JSONValue: TypeAlias = (
    None | bool | int | float | str | list["JSONValue"] | dict[str, "JSONValue"]
)

logger = __import__("logging").getLogger(__name__)


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


def _normalize_json_serializable(value: object) -> JSONValue:
    """Recursively normalize common non-JSON-native values into JSON-safe forms."""
    if isinstance(value, (bytes, bytearray, memoryview)):
        return "base64:" + base64.b64encode(bytes(value)).decode("ascii")
    if isinstance(value, dict):
        return {
            str(key): _normalize_json_serializable(inner_value)
            for key, inner_value in value.items()
        }
    if isinstance(value, (list, tuple, set)):
        return [_normalize_json_serializable(item) for item in value]
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    return str(value)


class NodeView:
    """Node accessor and presentation methods for MeshInterface.

    This class provides node lookup, node DB helpers, and presentation/display
    methods (showInfo, showNodes). It delegates to the parent MeshInterface for
    shared state.
    """

    def __init__(self, interface: "MeshInterface") -> None:
        """Initialize NodeView with a parent MeshInterface.

        Parameters
        ----------
        interface : MeshInterface
            The parent MeshInterface instance.
        """
        self._interface = interface

    @property
    def _node_db_lock(self):
        return self._interface._node_db_lock

    @property
    def localNode(self) -> meshtastic.node.Node:
        """Return the local node for this interface."""
        return self._interface.localNode

    @property
    def nodes(self) -> dict[str, dict[str, Any]] | None:
        """Return the node info dictionary, or None if not initialized."""
        return self._interface.nodes

    @property
    def nodesByNum(self) -> dict[int, dict[str, Any]] | None:
        """Return the node-number-to-info dictionary, or None if not initialized."""
        return self._interface.nodesByNum

    @property
    def myInfo(self) -> mesh_pb2.MyNodeInfo | None:
        """Return the MyNodeInfo for this interface, or None."""
        return self._interface.myInfo

    @property
    def metadata(self) -> mesh_pb2.DeviceMetadata | None:
        """Return device metadata, or None if not yet received."""
        return self._interface.metadata

    def _print_log_line(self, line: str) -> None:
        """Print a formatted device log line to the configured debug output.

        Parameters
        ----------
        line : str
            The raw log text to print.
        interface : Any
            The MeshInterface instance (or similar) providing a `debugOut`
            attribute which may be stdout, a callable that accepts a string, or a
            file-like object with a `write()` method.
        """
        interface = self._interface
        if print_color is not None and interface.debugOut == sys.stdout:
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
        nodes: dict[str, JSONValue] = {}
        with self._node_db_lock:
            nodes_snapshot = (
                [copy.deepcopy(node) for node in self.nodes.values()]
                if self.nodes
                else []
            )
        for n in nodes_snapshot:
            keys_to_remove = ("raw", "decoded", "payload")
            n2 = remove_keys_from_dict(keys_to_remove, n)

            user = n2.get("user")
            if not isinstance(user, dict):
                continue

            val = user.get("macaddr")
            if isinstance(val, str):
                try:
                    user["macaddr"] = convert_mac_addr(val)
                except (TypeError, ValueError):
                    logger.debug(
                        "Skipping malformed macaddr for node %s",
                        user.get("id"),
                    )

            node_id = user.get("id")
            if node_id is not None:
                nodes[str(node_id)] = _normalize_json_serializable(n2)
        infos = (
            owner
            + myinfo
            + metadata
            + mesh
            + str(__import__("json").dumps(nodes, indent=2))
        )
        if file is None:
            file = sys.stdout
        print(infos, file=file)
        return infos

    def showNodes(
        self, includeSelf: bool = True, showFields: list[str] | None = None
    ) -> str:
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
                return None
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
            if not isinstance(node_dict, dict):
                return None
            if "." not in key_path:
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

                fields = {}
                for col_name in showFields:
                    if "." in col_name:
                        raw_value = _get_nested_value(node, col_name)
                    elif col_name == "since":
                        raw_value = node.get("lastHeard")
                    else:
                        raw_value = node.get(col_name)

                    formatted_value: str | None = ""

                    if col_name == "channel":
                        if raw_value is None:
                            formatted_value = "0"
                    elif col_name == "deviceMetrics.channelUtilization":
                        formatted_value = _format_float(raw_value, 2, "%")
                    elif col_name == "deviceMetrics.airUtilTx":
                        formatted_value = _format_float(raw_value, 2, "%")
                    elif col_name == "deviceMetrics.batteryLevel":
                        if raw_value in (0, 101):
                            formatted_value = "Powered"
                        else:
                            formatted_value = _format_float(raw_value, 0, "%")
                    elif col_name == "isFavorite":
                        formatted_value = "*" if raw_value else ""
                    elif col_name == "lastHeard":
                        formatted_value = _get_lh(raw_value)
                    elif col_name == "position.latitude":
                        formatted_value = _format_float(raw_value, 4, "°")
                    elif col_name == "position.longitude":
                        formatted_value = _format_float(raw_value, 4, "°")
                    elif col_name == "position.altitude":
                        formatted_value = _format_float(raw_value, 0, "m")
                    elif col_name == "since":
                        formatted_value = _get_time_ago(raw_value) or "N/A"
                    elif col_name == "snr":
                        formatted_value = _format_float(raw_value, 0, " dB")
                    elif col_name == "user.shortName":
                        formatted_value = (
                            raw_value
                            if raw_value is not None
                            else f"Meshtastic {presumptive_id[-4:]}"
                        )
                    elif col_name == "user.id":
                        formatted_value = (
                            raw_value if raw_value is not None else presumptive_id
                        )
                    else:
                        formatted_value = raw_value

                    fields[col_name] = formatted_value

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
        MeshInterface = self._interface.__class__
        if nodeId in (LOCAL_ADDR, BROADCAST_ADDR):
            return self.localNode
        else:
            n = meshtastic.node.Node(self._interface, nodeId, timeout=timeout)
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
                        if new_index != last_index:
                            retries_left = requestChannelAttempts - 1
                        if retries_left <= 0:
                            raise MeshInterface.MeshInterfaceError(
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
        MeshInterface = self._interface.__class__
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
            }
            self.nodesByNum[nodeNum] = n
            return n
