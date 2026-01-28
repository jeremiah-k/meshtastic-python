# pylint: disable=too-many-lines
"""Node class for representing and managing mesh nodes.

This module provides the Node class which represents a (local or remote) node
in the mesh, including methods for localConfig, moduleConfig, and channels management.
"""

import base64
import binascii
import logging
import threading
import time
from typing import (
    TYPE_CHECKING,
    Any,
    Callable,
    NoReturn,
    Sequence,
)

from google.protobuf.message import DecodeError

from meshtastic.protobuf import (
    admin_pb2,
    apponly_pb2,
    channel_pb2,
    config_pb2,
    localonly_pb2,
    mesh_pb2,
    portnums_pb2,
)
from meshtastic.util import (
    Timeout,
    camel_to_snake,
    flagsToList,
    fromPSK,
    generate_channel_hash,
    messageToJson,
    pskToString,
    stripnl,
    toNodeNum,
)

if TYPE_CHECKING:
    from meshtastic.mesh_interface import MeshInterface

logger = logging.getLogger(__name__)

# Validation error messages for setOwner
EMPTY_LONG_NAME_MSG = "Long Name cannot be empty or contain only whitespace characters"
EMPTY_SHORT_NAME_MSG = (
    "Short Name cannot be empty or contain only whitespace characters"
)
# Maximum length for long_name (per protobuf definition in mesh.options)
MAX_LONG_NAME_LEN = 40


class Node:
    """A model of a (local or remote) node in the mesh.

    Includes methods for localConfig, moduleConfig and channels
    """

    def __init__(
        self,
        iface: "MeshInterface",
        nodeNum: int | str,
        noProto: bool = False,
        timeout: float = 300.0,
    ) -> None:
        """Create and initialize a Node instance that holds configuration, channel state, and runtime flags for a mesh node.

        Parameters
        ----------
        iface : 'MeshInterface'
            Interface used for network I/O and device interactions.
        nodeNum : int | str
            Node identifier (numeric or string convertible to a node number).
        noProto : bool
            If True, protocol-based operations are disabled for this node. (Default value = False)
        timeout : float
            Maximum seconds used for operations that wait for responses. (Default value = 300.0)
        """
        self.iface = iface
        self.nodeNum = toNodeNum(nodeNum) if isinstance(nodeNum, str) else nodeNum
        self.localConfig = localonly_pb2.LocalConfig()
        self.moduleConfig = localonly_pb2.LocalModuleConfig()
        self.channels: list[channel_pb2.Channel] | None = None
        self._timeout = Timeout(maxSecs=timeout)
        self.partialChannels: list[channel_pb2.Channel] = []
        self.noProto = noProto
        self.cannedPluginMessage: str | None = None
        self.cannedPluginMessageMessages: str | None = None
        self.ringtone: str | None = None
        self.ringtonePart: str | None = None
        self._ringtone_lock = threading.Lock()
        self._canned_message_lock = threading.Lock()

    def __repr__(self) -> str:
        """Return a developer-oriented string identifying the Node.

        Returns
        -------
        str
            A debug-friendly representation containing the interface repr, the node number
            formatted as eight-hex digits (prefixed with '0x'), and any active
            non-default flags such as `noProto` or a non-default `timeout`.
        """
        r = f"Node({self.iface!r}, 0x{self.nodeNum:08x}"
        if self.noProto:
            r += ", noProto=True"
        if self._timeout.expireTimeout != 300.0:
            r += f", timeout={self._timeout.expireTimeout!r}"
        r += ")"
        return r

    @staticmethod
    def positionFlagsList(position_flags: int) -> list[str]:
        """Convert a PositionConfig position flags bitfield into a list of flag names.

        Parameters
        ----------
        position_flags : int
            Bitfield of flags from Config.PositionConfig.PositionFlags.

        Returns
        -------
        list[str]
            Names of the flags set in `position_flags`.
        """
        return flagsToList(
            config_pb2.Config.PositionConfig.PositionFlags, position_flags
        )

    # COMPAT_STABLE_SHIM: alias for positionFlagsList
    @staticmethod
    def position_flags_list(position_flags: int) -> list[str]:
        """Backward-compatible alias for positionFlagsList."""
        return Node.positionFlagsList(position_flags)

    @staticmethod
    def excludedModulesList(excluded_modules: int) -> list[str]:
        """Convert an ExcludedModules bitfield to a list of excluded module names.

        Parameters
        ----------
        excluded_modules : int
            Bitfield using values from mesh_pb2.ExcludedModules.

        Returns
        -------
        list[str]
            Names of modules whose bits are set in the bitfield.
        """
        return flagsToList(mesh_pb2.ExcludedModules, excluded_modules)

    # COMPAT_STABLE_SHIM: alias for excludedModulesList
    @staticmethod
    def excluded_modules_list(excluded_modules: int) -> list[str]:
        """Backward-compatible alias for excludedModulesList."""
        return Node.excludedModulesList(excluded_modules)

    def moduleAvailable(self, excluded_bit: int) -> bool:
        """Determine whether a specific module bit is allowed by the interface metadata.

        Parameters
        ----------
        excluded_bit : int
            Bit mask for a module as defined in DeviceMetadata.excluded_modules.

        Returns
        -------
        bool
            `True` if the bit is not set in the interface metadata (module available), or if
            metadata is missing or an error occurs; `False` if the bit is set (module excluded).
        """
        meta = getattr(self.iface, "metadata", None)
        if meta is None:
            return True
        try:
            return bool((meta.excluded_modules & excluded_bit) == 0)
        except Exception as ex:  # noqa: BLE001 - defensive metadata compatibility
            logger.debug("Unable to evaluate module availability: %s", ex)
            return True

    # COMPAT_STABLE_SHIM: alias for moduleAvailable
    def module_available(self, excluded_bit: int) -> bool:
        """Backward-compatible alias for moduleAvailable."""
        return self.moduleAvailable(excluded_bit)

    def showChannels(self) -> None:
        """Print a human-readable list of configured channels and their shareable URLs.

        Each non-disabled channel is printed with its index, role, masked PSK, and settings as JSON.
        After listing channels, the primary channel URL is printed; if the full URL that includes
        all channels differs, it is printed as the "Complete URL".
        """
        print("Channels:")
        if self.channels:
            logger.debug(f"self.channels:{self.channels}")
            for c in self.channels:
                cStr = messageToJson(c.settings)
                # don't show disabled channels
                if channel_pb2.Channel.Role.Name(c.role) != "DISABLED":
                    print(
                        f"  Index {c.index}: {channel_pb2.Channel.Role.Name(c.role)} psk={pskToString(c.settings.psk)} {cStr}"
                    )
        publicURL = self.getURL(includeAll=False)
        adminURL = self.getURL(includeAll=True)
        print(f"\nPrimary channel URL: {publicURL}")
        if adminURL != publicURL:
            print(f"Complete URL (includes all channels): {adminURL}")

    def showInfo(self) -> None:
        """Print the node's local and module configurations (as JSON when available) followed by its configured channels.

        If a configuration is not present, an empty placeholder is printed for that
        section. Channels are displayed using the node's channel listing format.
        """
        prefs = ""
        if self.localConfig:
            prefs = messageToJson(self.localConfig, multiline=True)
        print(f"Preferences: {prefs}\n")
        prefs = ""
        if self.moduleConfig:
            prefs = messageToJson(self.moduleConfig, multiline=True)
        print(f"Module preferences: {prefs}\n")
        self.showChannels()

    def setChannels(self, channels: Sequence[channel_pb2.Channel]) -> None:
        """Set the node's channel list and normalize channel entries.

        Parameters
        ----------
        channels : collections.abc.Sequence[meshtastic.protobuf.channel_pb2.Channel]
            Sequence of channel protobufs to assign to this node. The assigned
            list will be normalized (indices fixed) and padded as needed to meet expected
            channel count.
        """
        self.channels = list(channels)
        self._fixup_channels()

    def requestChannels(self, startingIndex: int = 0) -> None:
        """Request channel definitions from the node, starting at the given channel index.

        When called with startingIndex 0, clears any cached channels and begins a fresh fetch into
        an internal partialChannels list. The method initiates the network request for the
        specified channel index.

        Parameters
        ----------
        startingIndex : int
            Zero-based channel index to start fetching from (typically 0-7). (Default value = 0)
        """
        logger.debug(f"requestChannels for nodeNum:{self.nodeNum}")
        # only initialize if we're starting out fresh
        if startingIndex == 0:
            self.channels = None
            # We keep our channels in a temp array until finished
            self.partialChannels = []
        self._request_channel(startingIndex)

    def onResponseRequestSettings(self, p: dict[str, Any]) -> None:
        """Process an admin response for a settings request and update the node's config objects.

        Parses the decoded response packet `p` to determine whether the request was acknowledged or
        rejected, marks the interface acknowledgment flags accordingly, and if the response contains
        `getConfigResponse` or `getModuleConfigResponse` copies the returned raw config into
        `self.localConfig` or `self.moduleConfig` respectively and logs the populated field.

        Parameters
        ----------
        p : dict[str, Any]
            Decoded response packet containing at least a `"decoded"` mapping with
            optional `"routing"` and `"admin"` entries. The `"admin"` entry is expected to
            contain either `getConfigResponse` or `getModuleConfigResponse` and accompanying
            `raw` bytes for the returned field.
        """
        logger.debug(f"onResponseRequestSetting() p:{p}")
        config_values = None
        if "routing" in p["decoded"]:
            if p["decoded"]["routing"]["errorReason"] != "NONE":
                logger.error(
                    f"Error on response: {p['decoded']['routing']['errorReason']}"
                )
                self.iface._acknowledgment.receivedNak = True
        else:
            self.iface._acknowledgment.receivedAck = True
            adminMessage = p["decoded"]["admin"]
            if "getConfigResponse" in adminMessage:
                oneof = "get_config_response"
                resp = adminMessage["getConfigResponse"]
                if not resp:
                    logger.warning("Received empty config response from node.")
                    return
                field = next(iter(resp.keys()))
                field_name = camel_to_snake(field)
                config_type = self.localConfig.DESCRIPTOR.fields_by_name.get(field_name)
                if config_type is None:
                    logger.warning(
                        "Ignoring unknown LocalConfig field in getConfigResponse: %s",
                        field_name,
                    )
                    return
                config_values = getattr(self.localConfig, config_type.name)
            elif "getModuleConfigResponse" in adminMessage:
                oneof = "get_module_config_response"
                resp = adminMessage["getModuleConfigResponse"]
                if not resp:
                    logger.warning("Received empty module config response from node.")
                    return
                field = next(iter(resp.keys()))
                field_name = camel_to_snake(field)
                config_type = self.moduleConfig.DESCRIPTOR.fields_by_name.get(
                    field_name
                )
                if config_type is None:
                    logger.warning(
                        "Ignoring unknown ModuleConfig field in getModuleConfigResponse: %s",
                        field_name,
                    )
                    return
                config_values = getattr(self.moduleConfig, config_type.name)
            else:
                logger.warning(
                    "Did not receive a valid response. Make sure to have a shared channel named 'admin'."
                )
                return
            if config_values is not None:
                raw_config = getattr(
                    getattr(adminMessage["raw"], oneof), camel_to_snake(field)
                )
                config_values.CopyFrom(raw_config)
                logger.info("%s:\n%s", camel_to_snake(field), config_values)

    def requestConfig(self, configType: int | Any) -> None:
        """Request a configuration subset or the full configuration from this node.

        If `configType` is an int it is treated as a config index. If it is a protobuf
        field descriptor, its `index` is used and the request targets `LocalConfig`
        when `containing_type.name == "LocalConfig"`, otherwise the module config is
        requested. For the local node the admin request is sent without a response
        handler; for a remote node this method registers a response handler and waits
        for an ACK/NAK before returning.

        Parameters
        ----------
        configType : int | Any
            Numeric config index or a
            protobuf field descriptor indicating which config field to fetch.
        """
        if self == self.iface.localNode:
            onResponse = None
        else:
            onResponse = self.onResponseRequestSettings
            logger.info(
                "Requesting current config from remote node (this can take a while)."
            )
        p = admin_pb2.AdminMessage()
        if isinstance(configType, int):
            p.get_config_request = configType  # type: ignore[assignment] # pyright: ignore[reportAttributeAccessIssue]

        else:
            msg_index = configType.index
            if configType.containing_type.name == "LocalConfig":
                p.get_config_request = admin_pb2.AdminMessage.ConfigType.Value(
                    f"{configType.name.upper()}_CONFIG"
                )
            else:
                p.get_module_config_request = (
                    msg_index  # pyright: ignore[reportAttributeAccessIssue]
                )

        self._send_admin(p, wantResponse=True, onResponse=onResponse)
        if onResponse:
            self.iface.waitForAckNak()

    def turnOffEncryptionOnPrimaryChannel(self) -> None:
        """Disable encryption on the primary channel and write the updated channel to the device.

        Raises
        ------
        MeshInterfaceError
            if channel data has not been loaded.
        """
        if self.channels is None:
            self._raise_interface_error("Error: No channels have been read")
        self.channels[0].settings.psk = fromPSK("none")
        logger.info("Writing modified channels to device")
        self.writeChannel(0)

    def waitForConfig(self, attribute: str = "channels") -> bool:
        """Wait until a given attribute on the node's localConfig is populated or the timeout elapses.

        Parameters
        ----------
        attribute : str
            Name of the attribute on `localConfig` to wait for (default: "channels").

        Returns
        -------
        bool
            True if the attribute was set before the timeout expired, False otherwise.
        """
        return self._timeout.waitForSet(self, attrs=("localConfig", attribute))

    def _raise_interface_error(self, message: str) -> NoReturn:
        """Raise a MeshInterface-style error with the provided message.

        Parameters
        ----------
        message : str
            The error message to use for the raised MeshInterfaceError.

        Raises
        ------
        MeshInterface.MeshInterfaceError
            Always raised with the provided message.
        """
        from meshtastic.mesh_interface import (  # pylint: disable=import-outside-toplevel
            MeshInterface,
        )

        raise MeshInterface.MeshInterfaceError(message)

    def writeConfig(self, config_name: str) -> None:
        """Write a single named subsection of the node's edited configuration to the device.

        Sends only the specified device or module configuration section from this Node's cached
        localConfig/moduleConfig to the target node. For remote nodes the send expects an
        acknowledgment (ACK/NAK); for the local node the message is sent without waiting for an ACK/NAK.

        Parameters
        ----------
        config_name : str
            Configuration section to write. Valid values:
            "device", "position", "power", "network", "display", "lora", "bluetooth",
            "security", "mqtt", "serial", "external_notification", "store_forward",
            "range_test", "telemetry", "canned_message", "audio", "remote_hardware",
            "neighbor_info", "detection_sensor", "ambient_lighting", "paxcounter".

        Raises
        ------
        MeshInterfaceError
            If `config_name` is not one of the supported names, or if
            localConfig/moduleConfig has not been loaded.
        """
        p = admin_pb2.AdminMessage()

        config_dispatch: dict[str, tuple[str, Any]] = {
            "device": ("set_config", self.localConfig.device),
            "position": ("set_config", self.localConfig.position),
            "power": ("set_config", self.localConfig.power),
            "network": ("set_config", self.localConfig.network),
            "display": ("set_config", self.localConfig.display),
            "lora": ("set_config", self.localConfig.lora),
            "bluetooth": ("set_config", self.localConfig.bluetooth),
            "security": ("set_config", self.localConfig.security),
            "mqtt": ("set_module_config", self.moduleConfig.mqtt),
            "serial": ("set_module_config", self.moduleConfig.serial),
            "external_notification": (
                "set_module_config",
                self.moduleConfig.external_notification,
            ),
            "store_forward": ("set_module_config", self.moduleConfig.store_forward),
            "range_test": ("set_module_config", self.moduleConfig.range_test),
            "telemetry": ("set_module_config", self.moduleConfig.telemetry),
            "canned_message": ("set_module_config", self.moduleConfig.canned_message),
            "audio": ("set_module_config", self.moduleConfig.audio),
            "remote_hardware": (
                "set_module_config",
                self.moduleConfig.remote_hardware,
            ),
            "neighbor_info": ("set_module_config", self.moduleConfig.neighbor_info),
            "detection_sensor": (
                "set_module_config",
                self.moduleConfig.detection_sensor,
            ),
            "ambient_lighting": (
                "set_module_config",
                self.moduleConfig.ambient_lighting,
            ),
            "paxcounter": ("set_module_config", self.moduleConfig.paxcounter),
            "traffic_management": (
                "set_module_config",
                self.moduleConfig.traffic_management,
            ),
        }
        config_entry = config_dispatch.get(config_name)
        if config_entry is None:
            self._raise_interface_error(
                f"Error: No valid config with name {config_name}"
            )
        if (
            len(self.localConfig.ListFields()) == 0
            and len(self.moduleConfig.ListFields()) == 0
        ):
            self._raise_interface_error(
                "Error: No localConfig has been read. "
                "Request config from the device before writing."
            )
        setter_name, source_config = config_entry
        config_setter = getattr(p, setter_name)
        getattr(config_setter, config_name).CopyFrom(source_config)

        logger.debug(f"Wrote: {config_name}")
        if self == self.iface.localNode:
            onResponse = None
        else:
            onResponse = self.onAckNak
        self._send_admin(p, onResponse=onResponse)

    def writeChannel(self, channelIndex: int, adminIndex: int = 0) -> None:
        """Write the channel at the given index to the device.

        Sends the specified channel configuration to the node and ensures an admin session key is present before sending.

        Parameters
        ----------
        channelIndex : int
            Index of the channel to write.
        adminIndex : int
            Admin channel index to use for sending. (Default value = 0)

        Raises
        ------
        MeshInterfaceError
            If channels have not been loaded (no channels to write).
        """
        if self.channels is None:
            self._raise_interface_error("Error: No channels have been read")
        self.ensureSessionKey()
        p = admin_pb2.AdminMessage()
        p.set_channel.CopyFrom(self.channels[channelIndex])
        self._send_admin(p, adminIndex=adminIndex)
        logger.debug(f"Wrote channel {channelIndex}")

    def getChannelByChannelIndex(self, channelIndex: int) -> channel_pb2.Channel | None:
        """Retrieve the channel at the given zero-based index from this node's channels.

        Parameters
        ----------
        channelIndex : int
            Zero-based channel index (typically 0-7).

        Returns
        -------
        channel_pb2.Channel | None
            The channel at the specified index, or None if channels are unset or the index is out of range.
        """
        ch = None
        if self.channels and 0 <= channelIndex < len(self.channels):
            ch = self.channels[channelIndex]
        return ch

    def deleteChannel(self, channelIndex: int) -> None:
        """Delete the channel at the given zero-based index and rewrite subsequent channels to normalize device channel state.

        Only channels with role SECONDARY or DISABLED may be removed; after
        removal, the channel list is normalized to the device channel count and
        affected channels are written back to the device. When operating on the local
        node, admin-channel indexing is adjusted so ongoing writes use the correct
        admin index.

        Parameters
        ----------
        channelIndex : int
            Zero-based index of the channel to delete.

        Raises
        ------
        MeshInterfaceError
            If channels have not been loaded.
        MeshInterfaceError
            If the channel at channelIndex is not Role.SECONDARY or Role.DISABLED.
        """
        if self.channels is None:
            self._raise_interface_error("Error: No channels have been read")
        ch = self.channels[channelIndex]
        if ch.role not in (
            channel_pb2.Channel.Role.SECONDARY,
            channel_pb2.Channel.Role.DISABLED,
        ):
            self._raise_interface_error(
                "Only SECONDARY or DISABLED channels can be deleted"
            )

        # we are careful here because if we move the "admin" channel the channelIndex we need to use
        # for sending admin channels will also change
        adminIndex = self.iface.localNode._get_admin_channel_index()

        self.channels.pop(channelIndex)
        self._fixup_channels()  # expand back to 8 channels

        index = channelIndex
        while index < 8:
            self.writeChannel(index, adminIndex=adminIndex)
            index += 1

            # if we are updating the local node, we might end up
            # *moving* the admin channel index as we are writing
            if (self.iface.localNode == self) and index >= adminIndex:
                # We've now passed the old location for admin index
                # (and written it), so we can start finding it by name again
                adminIndex = 0

    def getChannelByName(self, name: str) -> channel_pb2.Channel | None:
        """Find a channel whose settings.name exactly matches the provided name.

        Parameters
        ----------
        name : str
            The channel name to search for.

        Returns
        -------
        channel_pb2.Channel | None
            The matching channel object if found, `None` otherwise.
        """
        for c in self.channels or []:
            if c.settings and c.settings.name == name:
                return c
        return None

    def getDisabledChannel(self) -> channel_pb2.Channel | None:
        """Find the first channel whose role is DISABLED.

        Returns
        -------
        channel_pb2.Channel | None
            The first disabled channel if present, `None` otherwise.
        """
        if self.channels is None:
            return None
        for c in self.channels:
            if c.role == channel_pb2.Channel.Role.DISABLED:
                return c
        return None

    def _get_admin_channel_index(self) -> int:
        """Get the index of the channel named "admin", or 0 if no such channel exists.

        Returns
        -------
        int
            Index of the admin channel, or 0 if no channel with name "admin" is present.
        """
        for c in self.channels or []:
            if c.settings and c.settings.name.lower() == "admin":
                return c.index
        return 0

    def setOwner(
        self,
        long_name: str | None = None,
        short_name: str | None = None,
        is_licensed: bool = False,
        is_unmessagable: bool | None = None,
    ) -> mesh_pb2.MeshPacket | None:
        """Set the device owner fields (long and short names) and optional license/unmessagable flags for this node.

        Parameters
        ----------
        long_name : str | None
            Owner long name; leading/trailing whitespace is trimmed. If provided and empty after trimming, an error is raised. (Default value = None)
        short_name : str | None
            Owner short name; leading/trailing whitespace
            is trimmed and truncated to 4 characters if longer. If provided and empty
            after trimming, an error is raised. (Default value = None)
        is_licensed : bool
            If `long_name` is provided, set the owner's licensed flag. (Default value = False)
        is_unmessagable : bool | None
            If provided, set the owner's unmessagable flag. (Default value = None)

        Returns
        -------
        mesh_pb2.MeshPacket | None
            The sent Admin message packet if available, otherwise `None`.

        Raises
        ------
        MeshInterfaceError
            If `long_name` or `short_name` is provided but empty or whitespace-only after trimming.
        """
        logger.debug(f"in setOwner nodeNum:{self.nodeNum}")
        self.ensureSessionKey()
        p = admin_pb2.AdminMessage()

        nChars = 4
        if long_name is not None:
            long_name = long_name.strip()
            # Validate that long_name is not empty or whitespace-only
            if not long_name:
                self._raise_interface_error(EMPTY_LONG_NAME_MSG)
            if len(long_name) > MAX_LONG_NAME_LEN:
                long_name = long_name[:MAX_LONG_NAME_LEN]
                logger.warning(
                    f"Long name is longer than {MAX_LONG_NAME_LEN} characters, truncating to '{long_name}'"
                )
            p.set_owner.long_name = long_name
            p.set_owner.is_licensed = is_licensed
        if short_name is not None:
            short_name = short_name.strip()
            # Validate that short_name is not empty or whitespace-only
            if not short_name:
                self._raise_interface_error(EMPTY_SHORT_NAME_MSG)
            if len(short_name) > nChars:
                short_name = short_name[:nChars]
                logger.warning(
                    f"Short name is longer than {nChars} characters, truncating to '{short_name}'"
                )
            p.set_owner.short_name = short_name
        if is_unmessagable is not None:
            p.set_owner.is_unmessagable = is_unmessagable

        # Note: These debug lines are used in unit tests
        logger.debug(f"p.set_owner.long_name:{p.set_owner.long_name}:")
        logger.debug(f"p.set_owner.short_name:{p.set_owner.short_name}:")
        logger.debug(f"p.set_owner.is_licensed:{p.set_owner.is_licensed}:")
        logger.debug(f"p.set_owner.is_unmessagable:{p.set_owner.is_unmessagable}:")
        # If sending to a remote node, wait for ACK/NAK
        if self == self.iface.localNode:
            onResponse = None
        else:
            onResponse = self.onAckNak
        return self._send_admin(p, onResponse=onResponse)

    def getURL(self, includeAll: bool = True) -> str:
        """Build a sharable meshtastic URL encoding the node's primary channel and LoRa configuration.

        Includes the secondary channels in the URL when requested.

        Parameters
        ----------
        includeAll : bool
            If True, include secondary channels in addition to the primary channel. (Default value = True)

        Returns
        -------
        share_url : str
            A meshtastic.org URL containing the encoded channel set and LoRa configuration.
        """
        # Only keep the primary/secondary channels, assume primary is first
        channelSet = apponly_pb2.ChannelSet()
        if self.channels:
            for c in self.channels:
                if c.role == channel_pb2.Channel.Role.PRIMARY or (
                    includeAll and c.role == channel_pb2.Channel.Role.SECONDARY
                ):
                    channelSet.settings.append(c.settings)

        if len(self.localConfig.ListFields()) == 0:
            self.requestConfig(self.localConfig.DESCRIPTOR.fields_by_name.get("lora"))
        channelSet.lora_config.CopyFrom(self.localConfig.lora)
        some_bytes = channelSet.SerializeToString()
        s = base64.urlsafe_b64encode(some_bytes).decode("ascii")
        s = s.replace("=", "").replace("+", "-").replace("/", "_")
        return f"https://meshtastic.org/e/#{s}"

    def setURL(self, url: str, addOnly: bool = False) -> None:
        """Parse a Mesh URL and apply its channel and LoRa configuration to this node.

        If addOnly is False, replace the node's channel list with the channels encoded in the URL
        (first becomes PRIMARY, subsequent become SECONDARY) and write each channel to the device.
        If addOnly is True, add only channels from the URL whose names are not already present,
        placing each into the first available DISABLED channel and writing it.

        Parameters
        ----------
        url : str
            A Mesh share URL containing a base64-encoded ChannelSet (e.g., .../#<base64> or .../?add=true#<base64>).
        addOnly : bool
            If True, add channels without modifying existing ones; if False, replace channels with those from the URL. (Default value = False)

        Raises
        ------
        MeshInterfaceError
            If channels or configuration are not loaded, the URL is invalid or
            contains no settings, or no free channel slot is available when adding.
        """
        if self.channels is None:
            self._raise_interface_error("Config or channels not loaded")

        # URLs are of the form https://meshtastic.org/d/#{base64_channel_set}
        # Split on '/#' to find the base64 encoded channel settings
        if addOnly:
            splitURL = url.split("/?add=true#")
        else:
            splitURL = url.split("/#")
        if len(splitURL) == 1:
            self._raise_interface_error(f"Invalid URL '{url}'")
        b64 = splitURL[-1]
        if not b64:
            self._raise_interface_error(f"Invalid URL '{url}': no channel data found")

        # We normally strip padding to make for a shorter URL, but the python parser doesn't like
        # that.  So add back any missing padding
        # per https://stackoverflow.com/a/9807138
        missing_padding = len(b64) % 4
        if missing_padding:
            b64 += "=" * (4 - missing_padding)

        try:
            decodedURL = base64.urlsafe_b64decode(b64)
        except (binascii.Error, ValueError) as ex:
            self._raise_interface_error(f"Invalid URL '{url}': {ex}")
        channelSet = apponly_pb2.ChannelSet()
        try:
            channelSet.ParseFromString(decodedURL)
        except (DecodeError, ValueError) as ex:
            self._raise_interface_error(
                f"Unable to parse channel settings from URL '{url}': {ex}"
            )

        if len(channelSet.settings) == 0:
            self._raise_interface_error("There were no settings.")

        if addOnly:
            # Add new channels with names not already present
            # Don't change existing channels
            for chs in channelSet.settings:
                channelExists = self.getChannelByName(chs.name)
                if channelExists or chs.name == "":
                    logger.info(
                        f'Ignoring existing or empty channel "{chs.name}" from add URL'
                    )
                    continue
                ch = self.getDisabledChannel()
                if not ch:
                    self._raise_interface_error("No free channels were found")
                ch.settings.CopyFrom(chs)
                ch.role = channel_pb2.Channel.Role.SECONDARY
                logger.info(f"Adding new channel '{chs.name}' to device")
                self.writeChannel(ch.index)
        else:
            max_channels = len(self.channels)
            for i, chs in enumerate(channelSet.settings):
                if i >= max_channels:
                    logger.warning(
                        "URL contains more than %d channels; extra channels are ignored.",
                        max_channels,
                    )
                    break
                ch = channel_pb2.Channel()
                ch.role = (
                    channel_pb2.Channel.Role.PRIMARY
                    if i == 0
                    else channel_pb2.Channel.Role.SECONDARY
                )
                ch.index = i
                ch.settings.CopyFrom(chs)
                self.channels[ch.index] = ch
                logger.debug(f"Channel i:{i} ch:{ch}")
                self.writeChannel(ch.index)

        p = admin_pb2.AdminMessage()
        p.set_config.lora.CopyFrom(channelSet.lora_config)
        self.ensureSessionKey()
        self._send_admin(p)

    def onResponseRequestRingtone(self, p: dict[str, Any]) -> None:
        """Process an admin response containing a ringtone fragment and cache it on the Node.

        If the decoded response has no routing error and contains an admin.raw
        get_ringtone_response, stores that value in self.ringtonePart; if a routing
        error is present, the cached ringtone is not modified.

        Parameters
        ----------
        p : dict[str, Any]
            Decoded response packet from the interface. Expected to include
            a "decoded" dict with optional "routing" (containing "errorReason") and
            "admin" -> "raw" -> get_ringtone_response payload.
        """
        logger.debug("onResponseRequestRingtone() p:%s", p)
        errorFound = False
        if "routing" in p["decoded"]:
            if p["decoded"]["routing"]["errorReason"] != "NONE":
                errorFound = True
                logger.error(
                    "Error on response: %s", p["decoded"]["routing"]["errorReason"]
                )
        if errorFound is False:
            if "admin" in p["decoded"] and "raw" in p["decoded"]["admin"]:
                ringtone_part = p["decoded"]["admin"]["raw"].get_ringtone_response
                with self._ringtone_lock:
                    self.ringtonePart = ringtone_part
                logger.debug("self.ringtonePart:%s", ringtone_part)

    def _get_ringtone(self) -> str | None:
        """Retrieve the node's ringtone as a single concatenated string.

        This call will wait for a device response and may block until the node replies
        or the node's timeout elapses. If the External Notification module is excluded
        by firmware, or if no ringtone is available or the request times out, the
        method returns None.

        Returns
        -------
        str | None
            The complete ringtone string if available, `None` if the
            module is not present, the ringtone is unavailable, or the request
            timed out.
        """
        logger.debug("in get_ringtone()")
        if not self.module_available(mesh_pb2.EXTNOTIF_CONFIG):
            logger.warning(
                "External Notification module not present (excluded by firmware)"
            )
            return None

        with self._ringtone_lock:
            if self.ringtone:
                logger.debug("ringtone:%s", self.ringtone)
                return self.ringtone
            # Clear stale partial state before issuing a new request.
            self.ringtonePart = None

        response_event = threading.Event()

        def _on_ringtone_response(packet: dict[str, Any]) -> None:
            """Forward a ringtone response packet to the instance handler and signal completion.

            Calls self.onResponseRequestRingtone(packet) to process the response and
            ensures the threading Event response_event is set whether the handler
            succeeds or raises an exception, allowing any waiters to continue.

            Parameters
            ----------
            packet : dict[str, Any]
                Admin response packet containing ringtone data and routing information.
            """
            try:
                self.onResponseRequestRingtone(packet)
            finally:
                response_event.set()

        p1 = admin_pb2.AdminMessage()
        p1.get_ringtone_request = True
        request = self._send_admin(
            p1, wantResponse=True, onResponse=_on_ringtone_response
        )
        if request is None:
            logger.debug("Skipping ringtone wait because protocol send was not started")
            return None
        if not response_event.wait(timeout=self._timeout.expireTimeout):
            logger.warning("Timed out waiting for ringtone response")
            return None

        with self._ringtone_lock:
            # Another caller may have already populated the cache while we waited.
            if self.ringtone:
                logger.debug("ringtone:%s", self.ringtone)
                result = self.ringtone
            elif self.ringtonePart:
                self.ringtone = self.ringtonePart
                logger.debug("ringtone:%s", self.ringtone)
                result = self.ringtone
            else:
                result = None
        return result

    def _set_ringtone(self, ringtone: str) -> mesh_pb2.MeshPacket | None:
        """Set the node's ringtone.

        Validates that the External Notification module is available and that the ringtone length
        is 230 characters or fewer; ensures an admin session key, then sends one admin message.
        Returns None if the External Notification module is not available. For remote nodes the
        send waits for an ACK/NAK response.

        Parameters
        ----------
        ringtone : str
            The ringtone text to set.

        Returns
        -------
        mesh_pb2.MeshPacket | None
            The result of sending the AdminMessage for the first chunk, or `None` if the External Notification module is unavailable.

        Raises
        ------
        MeshInterfaceError
            If `ringtone` length exceeds 230 characters.
        """
        if not self.module_available(mesh_pb2.EXTNOTIF_CONFIG):
            logger.warning(
                "External Notification module not present (excluded by firmware)"
            )
            return None

        if len(ringtone) > 230:
            self._raise_interface_error("The ringtone must be 230 characters or fewer.")
        self.ensureSessionKey()
        p = admin_pb2.AdminMessage()
        p.set_ringtone_message = ringtone

        logger.debug("Setting ringtone '%s'", ringtone)
        # If sending to a remote node, wait for ACK/NAK
        if self == self.iface.localNode:
            onResponse = None
        else:
            onResponse = self.onAckNak
        send_result = self._send_admin(p, onResponse=onResponse)
        with self._ringtone_lock:
            # Invalidate cache after send so future reads refresh.
            self.ringtone = None
            self.ringtonePart = None
        return send_result

    def onResponseRequestCannedMessagePluginMessageMessages(
        self, p: dict[str, Any]
    ) -> None:
        """Handle the admin response for a canned-message plugin messages request.

        If the response indicates a routing error, prints the error. On a successful response,
        stores the `get_canned_message_module_messages_response` payload from the admin raw data
        into `self.cannedPluginMessageMessages`.

        Parameters
        ----------
        p : dict[str, Any]
            Decoded packet dictionary containing response fields, expected to include
            keys like `"decoded"`, `"decoded"]["routing"]`, and `"decoded"]["admin"]["raw"]`.
        """
        logger.debug("onResponseRequestCannedMessagePluginMessageMessages() p:%s", p)
        errorFound = False
        if "routing" in p["decoded"]:
            if p["decoded"]["routing"]["errorReason"] != "NONE":
                errorFound = True
                logger.error(
                    "Error on response: %s", p["decoded"]["routing"]["errorReason"]
                )
        if errorFound is False:
            if "admin" in p["decoded"] and "raw" in p["decoded"]["admin"]:
                canned_messages = p["decoded"]["admin"][
                    "raw"
                ].get_canned_message_module_messages_response
                with self._canned_message_lock:
                    self.cannedPluginMessageMessages = canned_messages
                logger.debug(
                    "self.cannedPluginMessageMessages:%s",
                    canned_messages,
                )

    def _get_canned_message(self) -> str | None:
        """Retrieve the device's canned message, requesting parts from the node if not already cached.

        If the canned-message module is excluded by firmware, returns None. When a
        request is made this call blocks until a response is received or the operation
        times out.

        Returns
        -------
        str | None
            str or None: The assembled canned message if available, or None if the module is unavailable or no response was received.
        """
        logger.debug("in get_canned_message()")
        if not self.module_available(mesh_pb2.CANNEDMSG_CONFIG):
            logger.warning("Canned Message module not present (excluded by firmware)")
            return None
        with self._canned_message_lock:
            if self.cannedPluginMessage:
                logger.debug("canned_plugin_message:%s", self.cannedPluginMessage)
                return self.cannedPluginMessage
            # Clear stale partial state before issuing a new request.
            self.cannedPluginMessageMessages = None

        response_event = threading.Event()

        def _on_canned_message_response(packet: dict[str, Any]) -> None:
            """Handle an incoming canned-message admin response and notify the waiting event.

            Forwards the received admin response packet to
            self.onResponseRequestCannedMessagePluginMessageMessages, then sets the
            response_event to signal completion. The event is set regardless of handler
            success to ensure waiters are released.

            Parameters
            ----------
            packet : dict[str, Any]
                The received admin response payload for the canned-message plugin.
            """
            try:
                self.onResponseRequestCannedMessagePluginMessageMessages(packet)
            finally:
                response_event.set()

        p1 = admin_pb2.AdminMessage()
        p1.get_canned_message_module_messages_request = True
        request = self._send_admin(
            p1,
            wantResponse=True,
            onResponse=_on_canned_message_response,
        )
        if request is None:
            logger.debug(
                "Skipping canned-message wait because protocol send was not started"
            )
            return None
        if not response_event.wait(timeout=self._timeout.expireTimeout):
            logger.warning("Timed out waiting for canned message response")
            return None

        with self._canned_message_lock:
            # Another caller may have already populated the cache while we waited.
            if self.cannedPluginMessage:
                logger.debug("canned_plugin_message:%s", self.cannedPluginMessage)
                result = self.cannedPluginMessage
            else:
                logger.debug(
                    "self.cannedPluginMessageMessages:%s",
                    self.cannedPluginMessageMessages,
                )
                if self.cannedPluginMessageMessages:
                    self.cannedPluginMessage = self.cannedPluginMessageMessages
                    logger.debug("canned_plugin_message:%s", self.cannedPluginMessage)
                    result = self.cannedPluginMessage
                else:
                    result = None
        return result

    def _set_canned_message(self, message: str) -> mesh_pb2.MeshPacket | None:
        """Set the device's canned message.

        If the canned-message module is not available on the device, the method logs a warning and
        returns None. If the provided message is longer than 200 characters, a MeshInterfaceError
        is raised. The message is sent with one admin request (waiting for an ACK/NAK when
        targeting a remote node).

        Parameters
        ----------
        message : str
            The canned message to set; must be 200 characters or fewer.

        Returns
        -------
        mesh_pb2.MeshPacket | None
            The result returned by _send_admin for the first chunk, or `None` if the canned-message module is unavailable.

        Raises
        ------
        MeshInterfaceError
            If `message` length is greater than 200 characters.
        """
        if not self.module_available(mesh_pb2.CANNEDMSG_CONFIG):
            logger.warning("Canned Message module not present (excluded by firmware)")
            return None

        if len(message) > 200:
            self._raise_interface_error(
                "The canned message must be 200 characters or fewer."
            )
        self.ensureSessionKey()
        p = admin_pb2.AdminMessage()
        p.set_canned_message_module_messages = message

        logger.debug("Setting canned message '%s'", message)
        # If sending to a remote node, wait for ACK/NAK
        if self == self.iface.localNode:
            onResponse = None
        else:
            onResponse = self.onAckNak
        send_result = self._send_admin(p, onResponse=onResponse)
        with self._canned_message_lock:
            # Invalidate cache after send so future reads refresh.
            self.cannedPluginMessage = None
            self.cannedPluginMessageMessages = None
        return send_result

    # COMPAT_STABLE_SHIM: alias for getRingtone
    def get_ringtone(self) -> str | None:
        """Compatibility wrapper that returns the node's ringtone.

        Canonical public method: getRingtone().

        Returns
        -------
        ringtone : str | None
            The ringtone string if available, or None if unavailable or unsupported.
        """
        return self.getRingtone()

    # COMPAT_STABLE_SHIM: alias for setRingtone
    def set_ringtone(self, ringtone: str) -> mesh_pb2.MeshPacket | None:
        """Set the device's ringtone.

        Backward-compatibility alias for setRingtone().

        Parameters
        ----------
        ringtone : str
            Ringtone payload to apply to the device.

        Returns
        -------
        mesh_pb2.MeshPacket | None
            The Admin MeshPacket sent for the request, or `None` if no packet was produced.
        """
        return self.setRingtone(ringtone)

    # COMPAT_STABLE_SHIM: alias for getCannedMessage
    def get_canned_message(self) -> str | None:
        """Return the device's canned message.

        Canonical public method: getCannedMessage().

        Returns
        -------
        str | None
            The canned message string if available, `None` otherwise.
        """
        return self.getCannedMessage()

    # COMPAT_STABLE_SHIM: alias for setCannedMessage
    def set_canned_message(self, message: str) -> mesh_pb2.MeshPacket | None:
        """Set the device's canned message using a backward-compatible snake_case wrapper.

        Backward-compatibility alias for setCannedMessage().

        Parameters
        ----------
        message : str
            The canned message text to set (maximum 200 characters).

        Returns
        -------
        mesh_pb2.MeshPacket | None
            The Admin MeshPacket that was sent, or `None` if no packet is produced.
        """
        return self.setCannedMessage(message)

    def getRingtone(self) -> str | None:
        """Get the node's ringtone.

        Returns
        -------
        str | None
            The ringtone data as a single concatenated string, or `None` if the ringtone is unavailable.
        """
        return self._get_ringtone()

    def setRingtone(self, ringtone: str) -> mesh_pb2.MeshPacket | None:
        """Set the node's ringtone.

        Parameters
        ----------
        ringtone : str
            Ringtone string to set (maximum 230 characters).

        Returns
        -------
        mesh_pb2.MeshPacket | None
            The Admin MeshPacket sent to set the ringtone,
            or `None` if the operation could not be completed (for example, the
            ringtone feature is unavailable or the request timed out).
        """
        return self._set_ringtone(ringtone)

    def getCannedMessage(self) -> str | None:
        """Retrieve the node's canned message.

        Returns
        -------
        str | None
            The canned message string if available, `None` otherwise.
        """
        return self._get_canned_message()

    def setCannedMessage(self, message: str) -> mesh_pb2.MeshPacket | None:
        """Set the node's canned message.

        Validates module availability and that `message` is at most 200 characters,
        ensures an admin session key, sends the AdminMessage to write the canned
        message, and invalidates any cached canned-message state.

        Parameters
        ----------
        message : str
            The canned message text to set (maximum 200 characters).

        Returns
        -------
        mesh_pb2.MeshPacket | None
            The sent MeshPacket if a packet was transmitted, `None` if no packet was sent.
        """
        return self._set_canned_message(message)

    def exitSimulator(self) -> mesh_pb2.MeshPacket | None:
        """Request the target simulator process to exit; has no effect on non-simulator nodes.

        Returns
        -------
        mesh_pb2.MeshPacket | None
            A MeshPacket for the sent admin request, or `None` if the admin message was not sent.
        """
        self.ensureSessionKey()
        p = admin_pb2.AdminMessage()
        p.exit_simulator = True
        logger.debug("in exitSimulator()")

        return self._send_admin(p)

    def reboot(self, secs: int = 10) -> mesh_pb2.MeshPacket | None:
        """Request the node to reboot after a delay.

        Parameters
        ----------
        secs : int
            Number of seconds to wait before rebooting. (Default value = 10)

        Returns
        -------
        mesh_pb2.MeshPacket | None
            The AdminMessage packet sent to the node, or `None` if no packet was sent.
        """
        self.ensureSessionKey()
        p = admin_pb2.AdminMessage()
        p.reboot_seconds = secs
        logger.info(f"Telling node to reboot in {secs} seconds")

        # If sending to a remote node, wait for ACK/NAK
        if self == self.iface.localNode:
            onResponse = None
        else:
            onResponse = self.onAckNak
        return self._send_admin(p, onResponse=onResponse)

    def beginSettingsTransaction(self) -> mesh_pb2.MeshPacket | None:
        """Request the node to open a settings edit transaction.

        Ensures an admin session key exists before sending the request and uses
        ACK/NAK handling for remote nodes while not waiting for a response from
        the local node.

        Returns
        -------
        mesh_pb2.MeshPacket | None
            The sent admin packet if available, or `None` otherwise.
        """
        self.ensureSessionKey()
        p = admin_pb2.AdminMessage()
        p.begin_edit_settings = True
        logger.info("Telling node to open a transaction to edit settings")

        # If sending to a remote node, wait for ACK/NAK
        if self == self.iface.localNode:
            onResponse = None
        else:
            onResponse = self.onAckNak
        return self._send_admin(p, onResponse=onResponse)

    def commitSettingsTransaction(self) -> mesh_pb2.MeshPacket | None:
        """Commit the node's open settings edit transaction.

        For remote nodes, waits for an ACK/NAK response; for the local node the commit is sent without waiting for a response.

        Returns
        -------
        mesh_pb2.MeshPacket | None
            The sent Admin `MeshPacket` when available, or `None`.
        """
        self.ensureSessionKey()
        p = admin_pb2.AdminMessage()
        p.commit_edit_settings = True
        logger.info("Telling node to commit open transaction for editing settings")

        # If sending to a remote node, wait for ACK/NAK
        if self == self.iface.localNode:
            onResponse = None
        else:
            onResponse = self.onAckNak
        return self._send_admin(p, onResponse=onResponse)

    def rebootOTA(self, secs: int = 10) -> mesh_pb2.MeshPacket | None:
        """Request the node to perform an OTA reboot after a given delay.

        Parameters
        ----------
        secs : int
            Seconds to wait before rebooting. (Default value = 10)

        Returns
        -------
        mesh_pb2.MeshPacket | None
            The sent Admin message packet, or `None` if no packet was produced.
        """
        self.ensureSessionKey()
        p = admin_pb2.AdminMessage()
        p.reboot_ota_seconds = secs
        logger.info(f"Telling node to reboot to OTA in {secs} seconds")

        # If sending to a remote node, wait for ACK/NAK
        if self == self.iface.localNode:
            onResponse = None
        else:
            onResponse = self.onAckNak
        return self._send_admin(p, onResponse=onResponse)

    def startOTA(
        self,
        mode: admin_pb2.OTAMode.ValueType,
        hash: bytes,
    ) -> mesh_pb2.MeshPacket | None:
        """Request OTA mode for local node firmware that supports ota_request.

        Parameters
        ----------
        mode : admin_pb2.OTAMode.ValueType
            OTA transport mode to use after reboot (for example, ``admin_pb2.OTA_WIFI``).
        hash : bytes
            Firmware hash bytes used by the node to validate OTA payload consistency.

        Returns
        -------
        mesh_pb2.MeshPacket | None
            The sent Admin message packet, or ``None`` if no packet was produced.

        Raises
        ------
        MeshInterfaceError
            If called for a non-local node.
        """
        if self != self.iface.localNode:
            self._raise_interface_error("startOTA only possible on local node")

        self.ensureSessionKey()
        p = admin_pb2.AdminMessage()
        p.ota_request.reboot_ota_mode = mode
        p.ota_request.ota_hash = hash
        return self._send_admin(p)

    def enterDFUMode(self) -> mesh_pb2.MeshPacket | None:
        """Request the node to enter DFU (NRF52) mode.

        Ensures an admin session key exists and sends an AdminMessage requesting DFU mode.
        When targeting a remote node, waits for an ACK/NAK response; local node sends without waiting.

        Returns
        -------
        mesh_pb2.MeshPacket | None
            The sent Admin message packet, or `None` if no packet was sent.
        """
        self.ensureSessionKey()
        p = admin_pb2.AdminMessage()
        p.enter_dfu_mode_request = True
        logger.info("Telling node to enable DFU mode")

        # If sending to a remote node, wait for ACK/NAK
        if self == self.iface.localNode:
            onResponse = None
        else:
            onResponse = self.onAckNak
        return self._send_admin(p, onResponse=onResponse)

    def shutdown(self, secs: int = 10) -> mesh_pb2.MeshPacket | None:
        """Request the node to shut down after a given number of seconds.

        Parameters
        ----------
        secs : int
            Number of seconds until the node shuts down. (Default value = 10)

        Returns
        -------
        mesh_pb2.MeshPacket | None
            The AdminMessage packet that was sent, or `None` if no packet was sent.
        """
        self.ensureSessionKey()
        p = admin_pb2.AdminMessage()
        p.shutdown_seconds = secs
        logger.info(f"Telling node to shutdown in {secs} seconds")

        # If sending to a remote node, wait for ACK/NAK
        if self == self.iface.localNode:
            onResponse = None
        else:
            onResponse = self.onAckNak
        return self._send_admin(p, onResponse=onResponse)

    def getMetadata(self) -> None:
        """Request the node's device metadata and wait for an acknowledgement.

        Sends a metadata request to the node and blocks until an ACK or NAK is received; the
        received metadata is processed asynchronously when the response arrives.
        """
        p = admin_pb2.AdminMessage()
        p.get_device_metadata_request = True
        logger.info("Requesting device metadata")

        self._send_admin(p, wantResponse=True, onResponse=self.onRequestGetMetadata)
        self.iface.waitForAckNak()

    def factoryReset(self, full: bool = False) -> mesh_pb2.MeshPacket | None:
        """Request a factory reset on the node.

        Parameters
        ----------
        full : bool
            If True, perform a full device factory reset; if False, reset configuration only. (Default value = False)

        Returns
        -------
        mesh_pb2.MeshPacket | None
            The sent admin packet if sending succeeded, or None otherwise.
        """
        self.ensureSessionKey()
        p = admin_pb2.AdminMessage()
        if full:
            p.factory_reset_device = True
            logger.info("Telling node to factory reset (full device reset)")
        else:
            p.factory_reset_config = True
            logger.info("Telling node to factory reset (config reset)")

        # If sending to a remote node, wait for ACK/NAK
        if self == self.iface.localNode:
            onResponse = None
        else:
            onResponse = self.onAckNak
        return self._send_admin(p, onResponse=onResponse)

    def removeNode(self, nodeId: int | str) -> mesh_pb2.MeshPacket | None:
        """Request removal of the mesh node identified by nodeId.

        Converts nodeId to a numeric node number and sends a remove-by-node-number
        admin request to the device. For remote targets, the request uses ACK/NAK
        handling; for the local node, no response callback is used.

        Parameters
        ----------
        nodeId : int | str
            Node number or a string convertible to a node number.

        Returns
        -------
        mesh_pb2.MeshPacket | None
            The admin packet returned by the send operation if available, `None` otherwise.
        """
        self.ensureSessionKey()
        nodeId = toNodeNum(nodeId)

        p = admin_pb2.AdminMessage()
        p.remove_by_nodenum = nodeId

        if self == self.iface.localNode:
            onResponse = None
        else:
            onResponse = self.onAckNak
        return self._send_admin(p, onResponse=onResponse)

    def setFavorite(self, nodeId: int | str) -> mesh_pb2.MeshPacket | None:
        """Mark a node as a favorite in the target device's NodeDB.

        Parameters
        ----------
        nodeId : int | str
            Node identifier (numeric or numeric string); will be converted to a node number.

        Returns
        -------
        mesh_pb2.MeshPacket | None
            The response packet if one was received, `None` otherwise.
        """
        self.ensureSessionKey()
        nodeId = toNodeNum(nodeId)

        p = admin_pb2.AdminMessage()
        p.set_favorite_node = nodeId

        if self == self.iface.localNode:
            onResponse = None
        else:
            onResponse = self.onAckNak
        return self._send_admin(p, onResponse=onResponse)

    def removeFavorite(self, nodeId: int | str) -> mesh_pb2.MeshPacket | None:
        """Unmark a node as a favorite in the device's NodeDB.

        Parameters
        ----------
        nodeId : int | str
            Numeric node identifier or a string that can be converted to one.

        Returns
        -------
        mesh_pb2.MeshPacket | None
            The Admin packet sent to the device, or `None` if no packet was sent.
        """
        self.ensureSessionKey()
        nodeId = toNodeNum(nodeId)

        p = admin_pb2.AdminMessage()
        p.remove_favorite_node = nodeId

        if self == self.iface.localNode:
            onResponse = None
        else:
            onResponse = self.onAckNak
        return self._send_admin(p, onResponse=onResponse)

    def setIgnored(self, nodeId: int | str) -> mesh_pb2.MeshPacket | None:
        """Mark a node in the device NodeDB as ignored.

        Parameters
        ----------
        nodeId : int | str
            Node number or string convertible to a node number.

        Returns
        -------
        mesh_pb2.MeshPacket | None
            The AdminMessage/packet sent to request the change, or `None` if no packet was sent.
        """
        self.ensureSessionKey()
        nodeId = toNodeNum(nodeId)

        p = admin_pb2.AdminMessage()
        p.set_ignored_node = nodeId

        if self == self.iface.localNode:
            onResponse = None
        else:
            onResponse = self.onAckNak
        return self._send_admin(p, onResponse=onResponse)

    def removeIgnored(self, nodeId: int | str) -> mesh_pb2.MeshPacket | None:
        """Unmark a node as ignored in the device's NodeDB.

        Parameters
        ----------
        nodeId : int | str
            Node identifier (integer or numeric string). It will be converted to a numeric node number.

        Returns
        -------
        mesh_pb2.MeshPacket | None
            `mesh_pb2.MeshPacket` if an AdminMessage was sent, `None` otherwise.
        """
        self.ensureSessionKey()
        nodeId = toNodeNum(nodeId)

        p = admin_pb2.AdminMessage()
        p.remove_ignored_node = nodeId

        if self == self.iface.localNode:
            onResponse = None
        else:
            onResponse = self.onAckNak
        return self._send_admin(p, onResponse=onResponse)

    def resetNodeDb(self) -> mesh_pb2.MeshPacket | None:
        """Request that the node clear its stored NodeDB (node database).

        Ensures an admin session key exists before sending. For remote targets, this
        waits for an ACK/NAK response; for the local node, it does not wait.

        Returns
        -------
        mesh_pb2.MeshPacket | None
            The AdminMessage packet sent, or `None` if no packet was sent.
        """
        self.ensureSessionKey()
        p = admin_pb2.AdminMessage()
        p.nodedb_reset = True
        logger.info("Telling node to reset the NodeDB")

        # If sending to a remote node, wait for ACK/NAK
        if self == self.iface.localNode:
            onResponse = None
        else:
            onResponse = self.onAckNak
        return self._send_admin(p, onResponse=onResponse)

    def setFixedPosition(
        self, lat: int | float, lon: int | float, alt: int
    ) -> mesh_pb2.MeshPacket | None:
        """Set the node's fixed position and enable the fixed-position setting on the device.

        Parameters
        ----------
        lat : int | float
            Latitude specified either as an integer in 1e-7 degrees units or as a float in decimal degrees.
        lon : int | float
            Longitude specified either as an integer in 1e-7 degrees units or as a float in decimal degrees.
        alt : int
            Altitude in meters (0 leaves altitude unset).

        Returns
        -------
        mesh_packet : mesh_pb2.MeshPacket | None
            The result from sending the AdminMessage, or `None` if no packet was sent.
        """
        self.ensureSessionKey()

        p = mesh_pb2.Position()
        if isinstance(lat, float) and lat != 0.0:
            p.latitude_i = int(lat * 1e7)
        elif isinstance(lat, int) and lat != 0:
            p.latitude_i = lat

        if isinstance(lon, float) and lon != 0.0:
            p.longitude_i = int(lon * 1e7)
        elif isinstance(lon, int) and lon != 0:
            p.longitude_i = lon

        if alt != 0:
            p.altitude = alt

        a = admin_pb2.AdminMessage()
        a.set_fixed_position.CopyFrom(p)

        if self == self.iface.localNode:
            onResponse = None
        else:
            onResponse = self.onAckNak
        return self._send_admin(a, onResponse=onResponse)

    def removeFixedPosition(self) -> mesh_pb2.MeshPacket | None:
        """Clear the node's fixed position setting.

        Sends an AdminMessage requesting removal of the node's fixed position; remote nodes will use ACK/NAK handling.

        Returns
        -------
        mesh_pb2.MeshPacket | None
            The sent AdminMessage (`mesh_pb2.MeshPacket`) if a packet was transmitted, or `None` if sending was skipped.
        """
        self.ensureSessionKey()
        p = admin_pb2.AdminMessage()
        p.remove_fixed_position = True
        logger.info("Telling node to remove fixed position")

        if self == self.iface.localNode:
            onResponse = None
        else:
            onResponse = self.onAckNak
        return self._send_admin(p, onResponse=onResponse)

    def setTime(self, timeSec: int = 0) -> mesh_pb2.MeshPacket | None:
        """Set the node's clock to the specified Unix timestamp.

        If `timeSec` is 0, the system's current time is used. The call sends an
        AdminMessage to set the node time; for remote nodes, the function waits for
        an ACK/NAK response.

        Parameters
        ----------
        timeSec : int
            Unix timestamp in seconds to set on the node; pass 0 to use the current system time. (Default value = 0)

        Returns
        -------
        mesh_pb2.MeshPacket | None
            mesh_pb2.MeshPacket or None: The sent AdminMessage packet when available, or `None` if no packet is produced.
        """
        self.ensureSessionKey()
        if timeSec == 0:
            timeSec = int(time.time())
        p = admin_pb2.AdminMessage()
        p.set_time_only = timeSec
        logger.info(f"Setting node time to {timeSec}")

        if self == self.iface.localNode:
            onResponse = None
        else:
            onResponse = self.onAckNak
        return self._send_admin(p, onResponse=onResponse)

    def _fixup_channels(self) -> None:
        """Normalize the node's channel list by assigning sequential index values and ensuring the list contains the expected number of channels.

        If `channels` is None this is a no-op. Otherwise this method sets each channel's `index`
        field to its position in the list (starting at 0) and then appends disabled channels as
        needed so the channel list reaches the required length.
        """
        channels = self.channels
        if channels is None:
            return

        if len(channels) > 8:
            logger.warning(
                "Truncating channel list from %d to 8 entries", len(channels)
            )
            del channels[8:]

        # Add extra disabled channels as needed
        # This is needed because the protobufs will have index **missing** if the channel number is zero
        for index, ch in enumerate(channels):
            ch.index = index  # fixup indexes

        self._fill_channels()

    def _fill_channels(self) -> None:
        """Ensure the node has exactly eight channels by appending DISABLED channels as needed.

        If `self.channels` is None this is a no-op. Appends new Channel objects with
        role `DISABLED` and sequential `index` values until the list length reaches 8.
        """
        channels = self.channels
        if channels is None:
            return

        # Add extra disabled channels as needed
        index = len(channels)
        while index < 8:
            ch = channel_pb2.Channel()
            ch.role = channel_pb2.Channel.Role.DISABLED
            ch.index = index
            channels.append(ch)
            index += 1

    def onRequestGetMetadata(self, p: dict[str, Any]) -> None:
        """Handle an incoming device metadata response packet and display the parsed metadata.

        Parses the decoded packet, updates the interface acknowledgment state (ACK/NAK), may retry
        the metadata request when notified by the routing layer, and logs the device metadata
        fields (firmware_version, device_state_version, role, position_flags, hw_model, hasPKC,
        and excluded_modules) when available.

        Parameters
        ----------
        p : dict[str, Any]
            Decoded packet containing at minimum a 'decoded' key with routing and
            admin/raw get_device_metadata_response fields.
        """
        logger.debug(f"onRequestGetMetadata() p:{p}")

        decoded = p["decoded"]

        if decoded["portnum"] == portnums_pb2.PortNum.Name(
            portnums_pb2.PortNum.ROUTING_APP
        ):
            if decoded["routing"]["errorReason"] != "NONE":
                logger.warning(
                    "Metadata request failed, error reason: %s",
                    decoded["routing"]["errorReason"],
                )
                self.iface._acknowledgment.receivedNak = True
                self._timeout.expireTime = time.time()  # Do not wait any longer
                return  # Don't try to parse this routing message
            logger.debug("Retrying metadata request.")
            self.getMetadata()
            return

        if "routing" in decoded and decoded["routing"]["errorReason"] != "NONE":
            logger.error("Error on response: %s", decoded["routing"]["errorReason"])
            self.iface._acknowledgment.receivedNak = True
            return

        self.iface._acknowledgment.receivedAck = True
        c = decoded["admin"]["raw"].get_device_metadata_response
        self._timeout.reset()  # We made forward progress
        logger.debug("Received metadata %s", stripnl(c))
        logger.info("\nfirmware_version: %s", c.firmware_version)
        logger.info("device_state_version: %s", c.device_state_version)
        if c.role in config_pb2.Config.DeviceConfig.Role.values():
            logger.info("role: %s", config_pb2.Config.DeviceConfig.Role.Name(c.role))
        else:
            logger.info("role: %s", c.role)
        logger.info("position_flags: %s", self.position_flags_list(c.position_flags))
        if c.hw_model in mesh_pb2.HardwareModel.values():
            logger.info("hw_model: %s", mesh_pb2.HardwareModel.Name(c.hw_model))
        else:
            logger.info("hw_model: %s", c.hw_model)
        logger.info("hasPKC: %s", c.hasPKC)
        if c.excluded_modules > 0:
            logger.info(
                "excluded_modules: %s",
                self.excluded_modules_list(c.excluded_modules),
            )

    def onResponseRequestChannel(self, p: dict[str, Any]) -> None:
        """Process a response packet for a previously requested channel and update the Node's channel state.

        If the packet is a routing message with an error, expire the request timeout and retry the
        last requested channel. If the packet contains an admin get_channel_response, append that
        channel to the node's partial channel list, reset the request timeout, and either continue
        requesting the next channel or, when the final channel is received, replace the node's
        channels with the collected channels and normalize them.

        Parameters
        ----------
        p : dict[str, Any]
            Decoded packet dictionary from the interface. Expected to contain either
            - a routing message with 'routing.errorReason', or
            - an admin message with 'admin.raw.get_channel_response' (a Channel protobuf-like object with an `index` field).
        """
        logger.debug(f"onResponseRequestChannel() p:{p}")

        if p["decoded"]["portnum"] == portnums_pb2.PortNum.Name(
            portnums_pb2.PortNum.ROUTING_APP
        ):
            if p["decoded"]["routing"]["errorReason"] != "NONE":
                logger.warning(
                    f"Channel request failed, error reason: {p['decoded']['routing']['errorReason']}"
                )
                self._timeout.expireTime = time.time()  # Do not wait any longer
                return  # Don't try to parse this routing message
            lastTried = 0
            if len(self.partialChannels) > 0:
                lastTried = self.partialChannels[-1].index
            logger.debug("Retrying previous channel request.")
            self._request_channel(lastTried)
            return

        c = p["decoded"]["admin"]["raw"].get_channel_response
        self.partialChannels.append(c)
        self._timeout.reset()  # We made forward progress
        logger.debug(f"Received channel {stripnl(c)}")
        index = c.index

        if index >= 8 - 1:
            logger.debug("Finished downloading channels")

            self.channels = self.partialChannels
            self._fixup_channels()
        else:
            self._request_channel(index + 1)

    def onAckNak(self, p: dict[str, Any]) -> None:
        """Handle an incoming ACK/NAK admin response and update interface acknowledgment state.

        Inspect the routing error reason in the parsed packet `p` and:
        - If the errorReason is not "NONE", log a NAK message and set
          iface._acknowledgment.receivedNak to True.
        - If the errorReason is "NONE" and the packet originates from the local node, log an
          implicit-ACK message and set iface._acknowledgment.receivedImplAck to True.
        - Otherwise log a normal ACK message and set iface._acknowledgment.receivedAck to True.

        Parameters
        ----------
        p : dict[str, Any]
            Parsed packet dictionary expected to contain:
            - p["decoded"]["routing"]["errorReason"]: routing error reason string.
            - p["from"]: numeric origin node identifier (string or int convertible).
        """
        decoded = p.get("decoded", {})
        routing = decoded.get("routing")
        if not isinstance(routing, dict):
            logger.warning("Received ACK/NAK response without routing details: %s", p)
            return
        error_reason = routing.get("errorReason", "NONE")
        if error_reason != "NONE":
            logger.warning(
                "Received a NAK, error reason: %s",
                error_reason,
            )
            self.iface._acknowledgment.receivedNak = True
        else:
            from_value = p.get("from")
            if from_value is None:
                logger.warning("Received ACK/NAK response without sender: %s", p)
                return
            try:
                from_num = int(from_value)
            except (TypeError, ValueError):
                logger.warning("Received ACK/NAK response with invalid sender: %s", p)
                return
            if from_num == self.iface.localNode.nodeNum:
                logger.info(
                    "Received an implicit ACK. Packet will likely arrive, but cannot be guaranteed."
                )
                self.iface._acknowledgment.receivedImplAck = True
            else:
                logger.info("Received an ACK.")
                self.iface._acknowledgment.receivedAck = True

    def _request_channel(self, channelNum: int) -> mesh_pb2.MeshPacket | None:
        """Request settings for a single channel from this node.

        Sends an admin request for the channel at the given zero-based index and registers the response handler.

        Parameters
        ----------
        channelNum : int
            Zero-based index of the channel to request.

        Returns
        -------
        mesh_pb2.MeshPacket | None
            The AdminMessage packet sent to the interface, or `None` if sending was skipped (e.g., protocol disabled).
        """
        p = admin_pb2.AdminMessage()
        p.get_channel_request = channelNum + 1

        # Show progress message for super slow operations
        if self != self.iface.localNode:
            logger.info(
                f"Requesting channel {channelNum} info from remote node (this could take a while)"
            )
        else:
            logger.debug(f"Requesting channel {channelNum}")

        return self._send_admin(
            p, wantResponse=True, onResponse=self.onResponseRequestChannel
        )

    # pylint: disable=R1710
    def _send_admin(
        self,
        p: admin_pb2.AdminMessage,
        wantResponse: bool = False,
        onResponse: Callable[[dict[str, Any]], Any] | None = None,
        adminIndex: int = 0,
    ) -> mesh_pb2.MeshPacket | None:
        """Send an AdminMessage to this Node's admin channel.

        Parameters
        ----------
        p : admin_pb2.AdminMessage
            AdminMessage to send; a session passkey may be attached.
        wantResponse : bool
            Request a response from the recipient when True. (Default value = False)
        onResponse : Callable[[dict[str, Any]], Any] | None
            Optional callback invoked with the received response packet. (Default value = None)
        adminIndex : int
            Channel index to use for the admin message; when 0 the node's configured admin channel is used. (Default value = 0)

        Returns
        -------
        mesh_pb2.MeshPacket | None
            The MeshPacket returned by the send operation,
            or `None` if sending was skipped because protocol use is disabled.
        """

        if self.noProto:
            logger.warning(
                "Not sending packet because protocol use is disabled by noProto"
            )
            return None
        if (
            adminIndex == 0
        ):  # unless a special channel index was used, we want to use the admin index
            adminIndex = self.iface.localNode._get_admin_channel_index()
        logger.debug(f"adminIndex:{adminIndex}")
        node_info = self.iface._get_or_create_by_num(self.nodeNum)
        passkey = node_info.get("adminSessionPassKey")
        if isinstance(passkey, bytes):
            p.session_passkey = passkey
        return self.iface.sendData(
            p,
            self.nodeNum,
            portNum=portnums_pb2.PortNum.ADMIN_APP,
            wantAck=True,
            wantResponse=wantResponse,
            onResponse=onResponse,
            channelIndex=adminIndex,
            pkiEncrypted=True,
        )

    def ensureSessionKey(self) -> None:
        """Ensure an admin session key exists for this node, requesting one if missing.

        If protocol use is disabled (`noProto`), no action is taken. Otherwise, if the node has no
        `adminSessionPassKey` recorded, a session-key request is sent.
        """
        if self.noProto:
            logger.warning(
                "Not ensuring session key, because protocol use is disabled by noProto"
            )
        else:
            if (
                self.iface._get_or_create_by_num(self.nodeNum).get(
                    "adminSessionPassKey"
                )
                is None
            ):
                self.requestConfig(admin_pb2.AdminMessage.SESSIONKEY_CONFIG)

    def _get_channels_with_hash(self) -> list[dict[str, Any]]:
        """Return a list of channel descriptors containing index, role, name, and an optional hash.

        Returns
        -------
        list[dict[str, Any]]
            A list of dictionaries, each with keys:
            - "index" (int): The channel's zero-based index.
            - "role" (str): The channel role name.
            - "name" (str): The channel settings name, or an empty string if missing.
            - "hash" (int or None): Computed channel hash when both name and PSK are present, otherwise None.
        """
        result: list[dict[str, Any]] = []
        if self.channels:
            for c in self.channels:
                settings = getattr(c, "settings", None)
                name = getattr(settings, "name", "")
                psk = getattr(settings, "psk", b"")
                has_name = bool(name)
                has_psk = bool(psk)
                hash_val = (
                    generate_channel_hash(name, psk) if has_name and has_psk else None
                )
                result.append(
                    {
                        "index": c.index,
                        "role": channel_pb2.Channel.Role.Name(c.role),
                        "name": name if has_name else "",
                        "hash": hash_val,
                    }
                )
        return result

    # COMPAT_STABLE_SHIM: alias for getChannelsWithHash
    def get_channels_with_hash(self) -> list[dict[str, Any]]:
        """Get channel entries with computed per-channel hashes.

        Each entry is a dict containing:
        - `index` (int): zero-based channel index.
        - `role` (str): channel role name.
        - `name` (str): channel settings name, or an empty string if unset.
        - `hash` (int | None): computed channel hash when both `name` and PSK are present, otherwise `None`.

        Returns
        -------
        list[dict[str, Any]]
            The list of channel entries described above.
        """
        return self._get_channels_with_hash()

    def getChannelsWithHash(self) -> list[dict[str, Any]]:
        """Compatibility wrapper that returns channel entries including computed per-channel hashes.

        Returns
        -------
        list[dict[str, Any]]
            A list of dictionaries, each with keys 'index', 'role', 'name', and 'hash'
            where 'hash' is the computed channel hash when both name and PSK are
            present, or `None` otherwise.
        """
        return self._get_channels_with_hash()
